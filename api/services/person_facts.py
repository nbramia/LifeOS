"""
Person Facts Service for LifeOS CRM.

Extracts and stores interesting facts about contacts using a multi-stage LLM pipeline.
Facts are stored in SQLite and can be displayed in the CRM UI.

Pipeline Architecture (v3):
- Stage 1: Extract candidate facts using Claude (natural sentence values, no keys)
- Stage 2: Batched validation + dedup using single Ollama call (quality, attribution, dedup)

Key design decisions:
- No LLM-generated keys: content-hash keys auto-generated for DB dedup
- Values are natural sentences displayed directly (no key-value stitching)
- Single batched Ollama call replaces N per-fact calls (faster, cheaper)
- Semantic dedup: Ollama compares candidates against existing facts
- Fallback: rule-based validation when Ollama unavailable
"""
import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any

from config.settings import settings
from api.utils.datetime_utils import make_aware as _make_aware
from api.utils.db_paths import get_crm_db_path

logger = logging.getLogger(__name__)

# Fact categories with their icons
FACT_CATEGORIES = {
    "family": "👨‍👩‍👧",
    "preferences": "⚙️",
    "background": "🏠",
    "interests": "🎯",
    "dates": "📅",
    "work": "💼",
    "topics": "💬",
    "travel": "✈️",
    "summary": "📊",  # Relationship summaries
}


@dataclass
class PersonFact:
    """
    A single fact about a person.

    Facts are extracted from interactions and stored for quick reference.
    Each fact must have a source quote as evidence.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    person_id: str = ""
    category: str = ""  # family, preferences, background, interests, dates, work, topics, travel, summary
    key: str = ""  # e.g., "spouse_name", "birthday", "hometown"
    value: str = ""  # The actual fact value
    confidence: float = 0.5  # 0.0-1.0
    source_interaction_id: Optional[str] = None  # For attribution
    source_quote: Optional[str] = None  # Verbatim quote proving this fact
    source_link: Optional[str] = None  # Deep link to source (Gmail, Calendar, Obsidian)
    extracted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confirmed_by_user: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "id": self.id,
            "person_id": self.person_id,
            "category": self.category,
            "key": self.key,
            "value": self.value,
            "confidence": self.confidence,
            "source_interaction_id": self.source_interaction_id,
            "source_quote": self.source_quote,
            "source_link": self.source_link,
            "extracted_at": self.extracted_at.isoformat() if self.extracted_at else None,
            "confirmed_by_user": self.confirmed_by_user,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "category_icon": FACT_CATEGORIES.get(self.category, "📄"),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PersonFact":
        """Create PersonFact from dict."""
        # Handle datetime parsing
        if data.get("extracted_at") and isinstance(data["extracted_at"], str):
            data["extracted_at"] = _make_aware(datetime.fromisoformat(data["extracted_at"]))
        if data.get("created_at") and isinstance(data["created_at"], str):
            data["created_at"] = _make_aware(datetime.fromisoformat(data["created_at"]))
        # Remove icon field if present (it's computed)
        data.pop("category_icon", None)
        return cls(**data)

    @classmethod
    def from_row(cls, row: tuple) -> "PersonFact":
        """Create PersonFact from SQLite row.

        Column order (after migration):
        0: id, 1: person_id, 2: category, 3: key, 4: value, 5: confidence,
        6: source_interaction_id, 7: extracted_at, 8: confirmed_by_user,
        9: created_at, 10: source_quote, 11: source_link
        """
        # Handle both old schema (10 columns) and new schema (12 columns)
        source_quote = row[10] if len(row) > 10 else None
        source_link = row[11] if len(row) > 11 else None

        return cls(
            id=row[0],
            person_id=row[1],
            category=row[2],
            key=row[3],
            value=row[4],
            confidence=row[5] or 0.5,
            source_interaction_id=row[6],
            source_quote=source_quote,
            source_link=source_link,
            extracted_at=_make_aware(datetime.fromisoformat(row[7])) if row[7] else datetime.now(timezone.utc),
            confirmed_by_user=bool(row[8]),
            created_at=_make_aware(datetime.fromisoformat(row[9])) if row[9] else datetime.now(timezone.utc),
        )


class PersonFactStore:
    """
    SQLite-backed storage for person facts.
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize fact store."""
        self.db_path = db_path or get_crm_db_path()
        self._init_db()

    def _init_db(self):
        """Create the person_facts table if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS person_facts (
                    id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5,
                    source_interaction_id TEXT,
                    source_quote TEXT,
                    source_link TEXT,
                    extracted_at TEXT,
                    confirmed_by_user INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(person_id, category, key)
                )
            """)

            # Index for efficient person queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_person_facts_person
                ON person_facts(person_id)
            """)

            # Index for category queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_person_facts_category
                ON person_facts(category)
            """)

            # Migrate existing table: add source_quote and source_link columns if missing
            try:
                conn.execute("ALTER TABLE person_facts ADD COLUMN source_quote TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            try:
                conn.execute("ALTER TABLE person_facts ADD COLUMN source_link TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # v3: Junction table for dual-association of relationship facts
            # Allows a fact to be associated with multiple people (e.g., "Taylor is Nathan's wife"
            # can appear on both Nathan's and Taylor's profile)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS person_fact_associations (
                    fact_id TEXT NOT NULL,
                    person_id TEXT NOT NULL,
                    is_primary BOOLEAN DEFAULT 0,
                    PRIMARY KEY (fact_id, person_id)
                )
            """)

            # Index for efficient lookups by person
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_fact_associations_person
                ON person_fact_associations(person_id)
            """)

            conn.commit()
            logger.info(f"Initialized person_facts table in {self.db_path}")
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        return sqlite3.connect(self.db_path)

    def add(self, fact: PersonFact) -> PersonFact:
        """Add a new fact."""
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT INTO person_facts
                (id, person_id, category, key, value, confidence, source_interaction_id,
                 source_quote, source_link, extracted_at, confirmed_by_user, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fact.id,
                fact.person_id,
                fact.category,
                fact.key,
                fact.value,
                fact.confidence,
                fact.source_interaction_id,
                fact.source_quote,
                fact.source_link,
                fact.extracted_at.isoformat() if fact.extracted_at else None,
                1 if fact.confirmed_by_user else 0,
                fact.created_at.isoformat() if fact.created_at else None,
            ))
            conn.commit()
            return fact
        finally:
            conn.close()

    def upsert(self, fact: PersonFact) -> PersonFact:
        """
        Insert or update a fact.

        If a fact with the same (person_id, category, key) exists:
        - Update if new confidence is higher or fact is user-confirmed
        - Otherwise keep existing
        """
        conn = self._get_connection()
        try:
            # Check for existing fact
            cursor = conn.execute("""
                SELECT id, confidence, confirmed_by_user FROM person_facts
                WHERE person_id = ? AND category = ? AND key = ?
            """, (fact.person_id, fact.category, fact.key))
            existing = cursor.fetchone()

            if existing:
                existing_id, existing_conf, existing_confirmed = existing
                # Only update if: new confidence is higher OR new fact is confirmed
                # AND existing is not already confirmed (user confirmation is sticky)
                if existing_confirmed:
                    logger.debug(f"Skipping update for confirmed fact: {fact.key}")
                    fact.id = existing_id
                    return fact

                if fact.confidence >= existing_conf or fact.confirmed_by_user:
                    conn.execute("""
                        UPDATE person_facts
                        SET value = ?, confidence = ?, source_interaction_id = ?,
                            source_quote = ?, source_link = ?,
                            extracted_at = ?, confirmed_by_user = ?
                        WHERE id = ?
                    """, (
                        fact.value,
                        fact.confidence,
                        fact.source_interaction_id,
                        fact.source_quote,
                        fact.source_link,
                        fact.extracted_at.isoformat() if fact.extracted_at else None,
                        1 if fact.confirmed_by_user else 0,
                        existing_id,
                    ))
                    fact.id = existing_id
                    conn.commit()
                else:
                    logger.debug(f"Skipping lower-confidence fact: {fact.key}")
                    fact.id = existing_id
            else:
                # Insert new fact
                conn.execute("""
                    INSERT INTO person_facts
                    (id, person_id, category, key, value, confidence, source_interaction_id,
                     source_quote, source_link, extracted_at, confirmed_by_user, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    fact.id,
                    fact.person_id,
                    fact.category,
                    fact.key,
                    fact.value,
                    fact.confidence,
                    fact.source_interaction_id,
                    fact.source_quote,
                    fact.source_link,
                    fact.extracted_at.isoformat() if fact.extracted_at else None,
                    1 if fact.confirmed_by_user else 0,
                    fact.created_at.isoformat() if fact.created_at else None,
                ))
                conn.commit()

            return fact
        finally:
            conn.close()

    def get_by_id(self, fact_id: str) -> Optional[PersonFact]:
        """Get fact by ID."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM person_facts WHERE id = ?", (fact_id,)
            )
            row = cursor.fetchone()
            if row:
                return PersonFact.from_row(row)
            return None
        finally:
            conn.close()

    def get_for_person(self, person_id: str, include_shared: bool = True) -> list[PersonFact]:
        """
        Get all facts for a person, including shared relationship facts.

        Args:
            person_id: The person's ID
            include_shared: If True, include facts associated via person_fact_associations

        Returns:
            List of PersonFact objects, sorted by category and key
        """
        conn = self._get_connection()
        try:
            if include_shared:
                # Include facts owned by this person OR associated via junction table
                cursor = conn.execute("""
                    SELECT DISTINCT f.* FROM person_facts f
                    LEFT JOIN person_fact_associations a ON f.id = a.fact_id
                    WHERE f.person_id = ? OR a.person_id = ?
                    ORDER BY f.category, f.key
                """, (person_id, person_id))
            else:
                # Only facts directly owned by this person
                cursor = conn.execute("""
                    SELECT * FROM person_facts
                    WHERE person_id = ?
                    ORDER BY category, key
                """, (person_id,))
            return [PersonFact.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def update(self, fact: PersonFact) -> PersonFact:
        """Update an existing fact."""
        conn = self._get_connection()
        try:
            conn.execute("""
                UPDATE person_facts
                SET category = ?, key = ?, value = ?, confidence = ?,
                    source_interaction_id = ?, confirmed_by_user = ?
                WHERE id = ?
            """, (
                fact.category,
                fact.key,
                fact.value,
                fact.confidence,
                fact.source_interaction_id,
                1 if fact.confirmed_by_user else 0,
                fact.id,
            ))
            conn.commit()
            return fact
        finally:
            conn.close()

    def confirm(self, fact_id: str) -> bool:
        """Mark a fact as confirmed by user."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                UPDATE person_facts
                SET confirmed_by_user = 1
                WHERE id = ?
            """, (fact_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete(self, fact_id: str) -> bool:
        """Delete a fact by ID."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM person_facts WHERE id = ?", (fact_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_for_person(self, person_id: str) -> int:
        """Delete all facts for a person."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM person_facts WHERE person_id = ?", (person_id,)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def add_association(self, fact_id: str, person_id: str, is_primary: bool = False) -> bool:
        """
        Associate a fact with an additional person.

        This enables relationship facts to appear on both people's profiles.
        For example, "Taylor is Nathan's wife" can be associated with both
        Taylor's profile (where it's the primary fact) and Nathan's profile.

        Args:
            fact_id: The fact to associate
            person_id: The person to associate it with
            is_primary: Whether this person is the primary subject of the fact

        Returns:
            True if association was created, False if it already existed
        """
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO person_fact_associations
                (fact_id, person_id, is_primary)
                VALUES (?, ?, ?)
            """, (fact_id, person_id, 1 if is_primary else 0))
            conn.commit()
            return True
        except Exception as e:
            logger.warning(f"Failed to add fact association: {e}")
            return False
        finally:
            conn.close()

    def get_associations(self, fact_id: str) -> list[dict]:
        """Get all person associations for a fact."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT person_id, is_primary FROM person_fact_associations
                WHERE fact_id = ?
            """, (fact_id,))
            return [
                {"person_id": row[0], "is_primary": bool(row[1])}
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def remove_association(self, fact_id: str, person_id: str) -> bool:
        """Remove an association between a fact and a person."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                DELETE FROM person_fact_associations
                WHERE fact_id = ? AND person_id = ?
            """, (fact_id, person_id))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


class PersonFactExtractor:
    """
    Extracts facts from interactions using Claude.

    Simple pipeline:
    1. Strategic sampling for large interaction sets
    2. Enrich message-based interactions with conversation context
    3. Single Claude call to extract facts with calibrated confidence
    4. Save facts

    Key features:
    - Focus on MEMORABLE facts, not obvious professional info
    - Confidence calibrated based on evidence strength in prompt
    - Message context windows for conversation-based sources
    - Supports both Sonnet (more accurate) and Haiku (cheaper/faster)
    """

    # Sampling configuration - tuned for ~20-30 second extraction
    # Target: 1 extraction batch + 1 summary = 2 Claude calls = ~20-25 seconds
    MAX_INTERACTIONS_PER_BATCH = 300  # Single batch for most people

    # High-value sources get priority allocation in budget distribution
    PRIORITY_SOURCES = {"calendar", "vault", "granola"}
    PRIORITY_BONUS = 1.5  # Priority sources get 50% more of their share

    # Model options
    # Use non-dated aliases for durability (always gets latest version)
    MODEL_SONNET = "claude-sonnet-4-5"
    MODEL_HAIKU = "claude-haiku-4-5"
    DEFAULT_MODEL = MODEL_HAIKU  # Default to Haiku for auto-extraction

    def __init__(self, fact_store: Optional[PersonFactStore] = None):
        """Initialize extractor."""
        self.fact_store = fact_store or get_person_fact_store()
        self._client: Any = None

    @property
    def client(self):
        """Lazy-load the Anthropic client."""
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def _generate_fact_key(self, category: str, value: str) -> str:
        """Generate a deterministic dedup key from category + normalized value."""
        normalized = re.sub(r'[^\w\s]', '', value.lower())
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return hashlib.sha256(f"{category}:{normalized}".encode()).hexdigest()[:12]

    async def extract_facts_async(
        self,
        person_id: str,
        person_name: str,
        interactions: list,
        model: Optional[str] = None,
    ) -> list[PersonFact]:
        """
        Extract facts from a person's interactions using Claude.

        Simple pipeline:
        1. Strategic sampling for large interaction sets
        2. Enrich message-based interactions with conversation context
        3. Single Claude call to extract facts with confidence
        4. Save facts

        Args:
            person_id: The person's ID
            person_name: The person's name
            interactions: List of interaction records
            model: Claude model to use (default: Sonnet, can use Haiku for cheaper/faster)

        Returns:
            List of extracted PersonFact objects
        """
        use_model = model or self.DEFAULT_MODEL
        logger.info(f"Using model {use_model} for {person_name}")

        if not interactions:
            logger.info(f"No interactions to extract facts from for {person_name}")
            return []

        # Strategic sampling for large interaction sets
        sampled_interactions = self._sample_interactions(interactions)
        logger.info(
            f"Sampling {len(sampled_interactions)} from {len(interactions)} interactions for {person_name}"
        )

        # Build interaction lookup for source attribution
        interaction_lookup = {i.get("id"): i for i in sampled_interactions if i.get("id")}

        # Enrich with conversation context (for message-based sources)
        enriched_interactions = self._enrich_with_context(sampled_interactions)

        # Fetch existing facts so Claude can avoid re-extracting known topics
        existing_facts = self.fact_store.get_for_person(person_id)
        existing_non_summary = [f for f in existing_facts if f.category != "summary"]

        # Extract facts with Claude
        extracted_facts = self._extract_facts_claude(
            person_id, person_name, enriched_interactions, interaction_lookup,
            use_model, existing_facts=existing_non_summary,
        )
        logger.info(f"Extracted {len(extracted_facts)} facts for {person_name}")

        # Combined validation + dedup (single batched Ollama call)
        if extracted_facts:
            extracted_facts = await self._validate_and_dedup_ollama(
                extracted_facts, existing_non_summary, person_name
            )

        # Programmatic word-overlap dedup as safety net (catches what Ollama misses)
        if extracted_facts:
            extracted_facts = self._word_overlap_dedup(extracted_facts, existing_non_summary)

        # Generate relationship summaries for people with sufficient interactions
        if len(interactions) >= 10:
            try:
                summaries = self._generate_relationship_summaries(
                    person_id, person_name, sampled_interactions, use_model
                )
                extracted_facts.extend(summaries)
            except Exception as e:
                logger.error(f"Failed to generate summaries for {person_name}: {e}")

        # Delete old non-confirmed, non-summary facts before saving new ones.
        # This clears legacy cruft (old key formats, terse values) on re-extraction.
        for old_fact in existing_non_summary:
            if not old_fact.confirmed_by_user:
                self.fact_store.delete(old_fact.id)
        if existing_non_summary:
            confirmed_kept = sum(1 for f in existing_non_summary if f.confirmed_by_user)
            deleted = len(existing_non_summary) - confirmed_kept
            logger.info(f"Cleared {deleted} old facts ({confirmed_kept} confirmed kept)")

        # Save new facts
        saved_facts = []
        seen_keys = set()
        for fact in extracted_facts:
            key = (fact.category, fact.key)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            saved_fact = self.fact_store.upsert(fact)
            saved_facts.append(saved_fact)

        logger.info(f"Saved {len(saved_facts)} facts for {person_name}")
        return saved_facts

    def extract_facts(
        self,
        person_id: str,
        person_name: str,
        interactions: list,
        model: Optional[str] = None,
    ) -> list[PersonFact]:
        """
        Extract facts from a person's interactions (sync wrapper).

        Args:
            person_id: The person's ID
            person_name: The person's name
            interactions: List of interaction records
            model: Claude model to use (default: Sonnet)

        Returns:
            List of extracted PersonFact objects
        """
        # Check if we're already in an async context
        try:
            asyncio.get_running_loop()
            # We're in an async context - run sync version
            logger.warning("extract_facts called from async context - use extract_facts_async instead")
            return self._extract_facts_sync(person_id, person_name, interactions, model)
        except RuntimeError:
            # No running event loop - safe to use asyncio.run
            return asyncio.run(self.extract_facts_async(person_id, person_name, interactions, model))

    def _extract_facts_sync(
        self,
        person_id: str,
        person_name: str,
        interactions: list,
        model: Optional[str] = None,
    ) -> list[PersonFact]:
        """Synchronous extraction for use within async contexts."""
        use_model = model or self.DEFAULT_MODEL

        if not interactions:
            return []

        sampled_interactions = self._sample_interactions(interactions)
        interaction_lookup = {i.get("id"): i for i in sampled_interactions if i.get("id")}
        enriched_interactions = self._enrich_with_context(sampled_interactions)

        # Fetch existing facts for dedup
        existing_facts = self.fact_store.get_for_person(person_id)
        existing_non_summary = [f for f in existing_facts if f.category != "summary"]

        extracted_facts = self._extract_facts_claude(
            person_id, person_name, enriched_interactions, interaction_lookup,
            use_model, existing_facts=existing_non_summary,
        )

        # Fallback validation (no Ollama in sync path)
        if extracted_facts:
            extracted_facts = self._fallback_validate(extracted_facts, existing_non_summary)

        # Programmatic word-overlap dedup
        if extracted_facts:
            extracted_facts = self._word_overlap_dedup(extracted_facts, existing_non_summary)

        if len(interactions) >= 10:
            try:
                summaries = self._generate_relationship_summaries(
                    person_id, person_name, sampled_interactions, use_model
                )
                extracted_facts.extend(summaries)
            except Exception as e:
                logger.error(f"Failed to generate summaries for {person_name}: {e}")

        saved_facts = []
        seen_keys = set()
        for fact in extracted_facts:
            key = (fact.category, fact.key)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            saved_fact = self.fact_store.upsert(fact)
            saved_facts.append(saved_fact)

        return saved_facts

    # Source types that benefit from conversation context
    MESSAGE_SOURCES = {"imessage", "whatsapp", "slack"}

    def _enrich_with_context(self, interactions: list) -> list:
        """
        Enrich message-based interactions with conversation context.

        For iMessage, WhatsApp, and Slack interactions, fetches surrounding
        messages to provide better context for fact extraction.

        Groups nearby messages into threads (same source_type, within 1 hour)
        and fetches context once per thread to minimize DB queries.
        """
        try:
            from api.services.interaction_store import get_interaction_store
            store = get_interaction_store()
        except Exception as e:
            logger.warning(f"Could not load interaction store for context enrichment: {e}")
            return interactions

        # Separate message-based and non-message interactions
        message_interactions = [
            i for i in interactions
            if i.get("source_type") in self.MESSAGE_SOURCES and i.get("id")
        ]

        if not message_interactions:
            return interactions

        # Group into threads: same source_type, within 1 hour of each other
        threads = self._group_into_threads(message_interactions)
        logger.info(f"Grouped {len(message_interactions)} messages into {len(threads)} threads for context")

        # Fetch context once per thread (using the first message in each thread)
        context_cache = {}  # interaction_id -> context_text
        for thread in threads:
            representative_id = thread[0].get("id")
            try:
                context_interactions = store.get_conversation_context(
                    representative_id, window=3
                )
                if len(context_interactions) > 1:
                    # Build context text from surrounding messages
                    context_lines = []
                    for ctx in context_interactions:
                        title = ctx.title or ""
                        context_lines.append(f"  [{ctx.source_type}] {title}")
                    context_text = "\n".join(context_lines)

                    # Apply context to all messages in this thread
                    for msg in thread:
                        context_cache[msg.get("id")] = context_text
            except Exception as e:
                logger.debug(f"Context fetch failed for {representative_id}: {e}")

        if context_cache:
            logger.info(f"Enriched {len(context_cache)} messages with conversation context")

        # Attach context to interactions
        enriched = []
        for interaction in interactions:
            iid = interaction.get("id")
            if iid and iid in context_cache:
                interaction = dict(interaction)  # Don't mutate original
                interaction["thread_context"] = context_cache[iid]
            enriched.append(interaction)

        return enriched

    def _group_into_threads(self, messages: list) -> list[list]:
        """Group messages into conversation threads by source proximity."""
        if not messages:
            return []

        # Sort by source_type then timestamp
        sorted_msgs = sorted(
            messages,
            key=lambda x: (x.get("source_type", ""), x.get("timestamp", ""))
        )

        threads = []
        current_thread = [sorted_msgs[0]]

        for msg in sorted_msgs[1:]:
            prev = current_thread[-1]
            # Same thread if same source_type and within 1 hour
            if msg.get("source_type") == prev.get("source_type"):
                try:
                    prev_ts = datetime.fromisoformat(prev.get("timestamp", ""))
                    msg_ts = datetime.fromisoformat(msg.get("timestamp", ""))
                    if abs((msg_ts - prev_ts).total_seconds()) <= 3600:
                        current_thread.append(msg)
                        continue
                except (ValueError, TypeError):
                    pass

            # Start new thread
            threads.append(current_thread)
            current_thread = [msg]

        threads.append(current_thread)
        return threads

    def _extract_facts_claude(
        self,
        person_id: str,
        person_name: str,
        interactions: list[dict],
        interaction_lookup: dict,
        model: str,
        existing_facts: Optional[list[PersonFact]] = None,
    ) -> list[PersonFact]:
        """
        Extract facts using Claude with calibrated confidence.

        Focuses on MEMORABLE personal details that help with recall,
        not obvious professional information.

        Args:
            person_id: The person's ID
            person_name: The person's name
            interactions: Interactions to extract facts from
            interaction_lookup: Dict mapping interaction IDs to full records
            model: Claude model to use
            existing_facts: Already-stored facts to avoid re-extracting

        Returns:
            List of PersonFact objects with calibrated confidence scores
        """
        all_facts = []
        batches = self._create_batches(interactions, self.MAX_INTERACTIONS_PER_BATCH)
        logger.info(f"Processing {len(interactions)} interactions in {len(batches)} batch(es) for {person_name}")

        for batch_idx, batch in enumerate(batches):
            logger.info(f"Batch {batch_idx + 1}/{len(batches)}: {len(batch)} interactions")
            interaction_text = self._format_interactions(batch, person_name)
            prompt = self._build_extraction_prompt(
                person_name, interaction_text, existing_facts=existing_facts
            )

            try:
                response = self.client.messages.create(
                    model=model,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}]
                )

                response_text = response.content[0].text
                facts = self._parse_extraction_response(
                    response_text, person_id, interaction_lookup
                )
                all_facts.extend(facts)

            except Exception as e:
                logger.error(f"Fact extraction failed for batch {batch_idx + 1}: {e}")

        return all_facts

    def _sample_interactions(self, interactions: list) -> list:
        """
        Intelligently sample interactions with dynamic budget distribution.

        The budget (300 interactions) is distributed across source types based on
        how many sources the person has:
        - 1 source: gets all 300
        - 3 sources: ~100 each (with priority bonus for calendar/vault/granola)
        - 10 sources: ~30 each (with priority bonuses)

        This ensures someone who only uses iMessage gets more iMessages sampled
        than someone who uses 7 different channels.
        """
        if len(interactions) <= self.MAX_INTERACTIONS_PER_BATCH:
            return interactions

        # Sort by timestamp (most recent first)
        sorted_interactions = sorted(
            interactions,
            key=lambda x: x.get("timestamp", ""),
            reverse=True
        )

        # Group by source type
        by_source: dict[str, list] = {}
        for interaction in sorted_interactions:
            source_type = interaction.get("source_type", "other")
            if source_type not in by_source:
                by_source[source_type] = []
            by_source[source_type].append(interaction)

        # Calculate budget per source (with priority bonuses)
        num_sources = len(by_source)
        priority_count = sum(1 for s in by_source if s in self.PRIORITY_SOURCES)
        non_priority_count = num_sources - priority_count

        # Total "shares" = non_priority + (priority * bonus)
        total_shares = non_priority_count + (priority_count * self.PRIORITY_BONUS)
        budget_per_share = self.MAX_INTERACTIONS_PER_BATCH / total_shares if total_shares > 0 else self.MAX_INTERACTIONS_PER_BATCH

        # Sample from each source type
        sampled = []
        for source_type, source_interactions in by_source.items():
            is_priority = source_type in self.PRIORITY_SOURCES
            allocation = int(budget_per_share * (self.PRIORITY_BONUS if is_priority else 1.0))
            sampled.extend(self._sample_with_temporal_diversity(source_interactions, allocation))

        # Final cap and sort by timestamp
        if len(sampled) > self.MAX_INTERACTIONS_PER_BATCH:
            sampled = sampled[:self.MAX_INTERACTIONS_PER_BATCH]
        sampled.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        # Log breakdown
        breakdown = []
        for k, v in by_source.items():
            is_priority = k in self.PRIORITY_SOURCES
            allocation = int(budget_per_share * (self.PRIORITY_BONUS if is_priority else 1.0))
            breakdown.append(f"{k}:{min(len(v), allocation)}/{len(v)}")
        logger.info(f"Sampled {len(sampled)} from {len(interactions)} ({', '.join(breakdown)})")

        return sampled

    def _sample_with_temporal_diversity(self, interactions: list, budget: int) -> list:
        """
        Sample interactions with temporal diversity across time buckets.

        Distributes budget across time periods to avoid missing old facts:
        - 50% from the most recent year
        - 30% from 1-3 years ago
        - 20% from 3+ years ago

        Unused budget from empty buckets is redistributed to non-empty ones.
        Interactions must already be sorted most-recent-first.
        """
        if len(interactions) <= budget:
            return interactions

        now = datetime.now(timezone.utc)

        # Bucket interactions by age
        recent = []      # < 1 year
        mid = []         # 1-3 years
        old = []         # 3+ years

        for interaction in interactions:
            ts = interaction.get("timestamp", "")
            if not ts:
                recent.append(interaction)
                continue
            try:
                dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_days = (now - dt).days
                if age_days < 365:
                    recent.append(interaction)
                elif age_days < 365 * 3:
                    mid.append(interaction)
                else:
                    old.append(interaction)
            except (ValueError, TypeError):
                recent.append(interaction)

        # Initial allocations
        allocations = {
            "recent": int(budget * 0.5),
            "mid": int(budget * 0.3),
            "old": budget - int(budget * 0.5) - int(budget * 0.3),  # remainder
        }
        buckets = {"recent": recent, "mid": mid, "old": old}

        # Redistribute unused budget
        unused = 0
        for name in ["recent", "mid", "old"]:
            available = len(buckets[name])
            if available < allocations[name]:
                unused += allocations[name] - available
                allocations[name] = available

        # Distribute unused to buckets that have more items
        if unused > 0:
            for name in ["recent", "mid", "old"]:
                can_add = len(buckets[name]) - allocations[name]
                if can_add > 0:
                    give = min(can_add, unused)
                    allocations[name] += give
                    unused -= give
                    if unused == 0:
                        break

        result = []
        for name in ["recent", "mid", "old"]:
            result.extend(buckets[name][:allocations[name]])

        return result

    def _create_batches(self, interactions: list, batch_size: int) -> list[list]:
        """Split interactions into batches for processing."""
        return [
            interactions[i:i + batch_size]
            for i in range(0, len(interactions), batch_size)
        ]

    def _format_interactions(self, interactions: list, person_name: str = "") -> str:
        """Format interactions for the prompt with clear sender attribution."""
        # Uses LIFEOS_USER_NAME from settings (see config/settings.py)
        user_upper = settings.user_name.upper()
        lines = []
        for i, interaction in enumerate(interactions, 1):
            source_type = interaction.get("source_type", "unknown")
            title = interaction.get("title", "Untitled")
            snippet = interaction.get("snippet", "")
            timestamp = interaction.get("timestamp", "")
            interaction_id = interaction.get("id", "")

            # For message-based sources, make sender crystal clear
            # Title format from sync: "→ message" (sent) or "← message" (received)
            sender_prefix = ""
            if source_type in ("imessage", "whatsapp", "slack", "phone"):
                if title.startswith("→"):
                    sender_prefix = f"[{user_upper} SENT]: "
                    title = title[1:].strip()  # Remove arrow
                elif title.startswith("←"):
                    sender_prefix = f"[{person_name.upper()} SENT]: "
                    title = title[1:].strip()  # Remove arrow

            line = f"[{i}] ID:{interaction_id} [{source_type}] {timestamp}"
            if sender_prefix:
                line += f"\n    {sender_prefix}{title}"
            else:
                line += f": {title}"

            if snippet and not sender_prefix:
                # Only add snippet if we didn't already include the full message
                line += f"\n    Content: {snippet[:800]}"

            # Include thread context if available
            thread_context = interaction.get("thread_context")
            if thread_context:
                line += f"\n    Thread context:\n{thread_context}"

            lines.append(line)

        return "\n\n".join(lines)

    async def _validate_and_dedup_ollama(
        self,
        new_facts: list[PersonFact],
        existing_facts: list[PersonFact],
        person_name: str,
    ) -> list[PersonFact]:
        """
        Validate and deduplicate facts in a single batched Ollama call.

        Sends all candidates + existing facts to Ollama, which returns
        keep/reject/update decisions for each candidate. Falls back to
        rule-based validation if Ollama is unavailable.
        """
        from api.services.ollama_client import OllamaClient

        client = OllamaClient()

        if not await client.is_available_async():
            logger.warning("Ollama unavailable — using fallback validation")
            return self._fallback_validate(new_facts, existing_facts)

        # Build existing facts list for the prompt
        existing_lines = []
        for i, fact in enumerate(existing_facts):
            confirmed = " [USER-CONFIRMED]" if fact.confirmed_by_user else ""
            existing_lines.append(f"[E{i}] {fact.category}: {fact.value}{confirmed}")

        # Build candidate facts list
        candidate_lines = []
        for i, fact in enumerate(new_facts):
            quote_part = f' (quote: "{fact.source_quote}")' if fact.source_quote else ""
            candidate_lines.append(f"[C{i}] {fact.category}: {fact.value}{quote_part}")

        existing_block = "\n".join(existing_lines) if existing_lines else "(none)"
        candidate_block = "\n".join(candidate_lines)

        prompt = f"""Validate candidate facts about {person_name}. Check for duplicates against BOTH existing facts AND other candidates.

EXISTING FACTS (already stored):
{existing_block}

CANDIDATE FACTS (to validate):
{candidate_block}

For each candidate [C0], [C1], etc., return a JSON array with one decision per candidate:

{{
  "decisions": [
    {{
      "candidate": 0,
      "action": "keep",
      "evidence_strength": 4,
      "reason": "New unique fact with strong evidence"
    }},
    {{
      "candidate": 1,
      "action": "reject",
      "reason": "Universal fact — everyone has a mother"
    }},
    {{
      "candidate": 2,
      "action": "update",
      "updates_existing": 0,
      "evidence_strength": 4,
      "reason": "More detailed version of E0"
    }},
    {{
      "candidate": 3,
      "action": "merge",
      "merge_into_candidate": 0,
      "reason": "Same topic as C0 — backpacking"
    }}
  ]
}}

ACTIONS:
- "keep": New, unique, specific fact. Assign evidence_strength 1-5.
- "reject": Universal/obvious, wrong person, unsupported by quote, too vague, or fewer than 5 words.
- "update": Same core topic as an EXISTING fact but with more/better detail. Specify "updates_existing" index. If the existing fact is marked [USER-CONFIRMED], use "keep" instead (don't overwrite confirmed facts).
- "merge": Same core topic as another CANDIDATE in this batch. Keep the most detailed version, merge the rest into it via "merge_into_candidate" index. Only the target candidate survives.

CRITICAL — DEDUPLICATION:
Two facts are duplicates if they describe the SAME core topic about this person, even if worded differently. Examples of duplicates:
- "Goes backpacking" and "Interested in backpacking and signed up for a trip" → SAME TOPIC (backpacking)
- "Has a daughter named Emma" and "Daughter Emma plays soccer" → SAME TOPIC (daughter Emma)
- "Works at Google" and "Software engineer at Google" → SAME TOPIC (works at Google)
When in doubt, merge/update. It is much worse to keep duplicates than to accidentally merge two slightly different facts.

Check EVERY candidate against ALL existing facts AND all other candidates for overlap. Be aggressive about dedup.

- evidence_strength: 1=weak inference, 2=implied, 3=stated once, 4=clearly stated, 5=explicitly confirmed multiple times.

Return ONLY valid JSON."""

        try:
            result = await client.generate_json(
                prompt=prompt,
                max_tokens=2048,
                timeout=30,
            )

            decisions = result.get("decisions", [])
            # Index decisions by candidate number
            decision_map = {}
            for d in decisions:
                idx = d.get("candidate")
                if idx is not None:
                    decision_map[idx] = d

            # First pass: identify which candidates are merged away
            merged_away = set()
            for d in decisions:
                if d.get("action") == "merge":
                    merged_away.add(d.get("candidate"))

            validated = []
            kept = 0
            rejected = 0
            merged = 0

            confidence_map = {1: 0.5, 2: 0.6, 3: 0.7, 4: 0.8, 5: 0.9}

            for i, fact in enumerate(new_facts):
                # Skip candidates that were merged into another candidate
                if i in merged_away:
                    merged += 1
                    continue

                decision = decision_map.get(i)

                if not decision:
                    # Not mentioned in response → reject (safe default)
                    rejected += 1
                    continue

                action = decision.get("action", "reject")
                evidence_strength = int(decision.get("evidence_strength", 3))
                fact.confidence = confidence_map.get(
                    max(1, min(5, evidence_strength)), 0.7
                )

                if action == "keep":
                    kept += 1
                    validated.append(fact)

                elif action == "update":
                    updates_idx = decision.get("updates_existing")
                    if updates_idx is not None and 0 <= updates_idx < len(existing_facts):
                        existing = existing_facts[updates_idx]
                        if existing.confirmed_by_user:
                            # Don't overwrite confirmed facts — keep as new instead
                            kept += 1
                            validated.append(fact)
                        else:
                            # Use existing fact's key so upsert overwrites it
                            fact.key = existing.key
                            kept += 1
                            validated.append(fact)
                    else:
                        # Invalid update target — keep as new
                        kept += 1
                        validated.append(fact)

                else:  # reject or unrecognized
                    rejected += 1

            logger.info(
                f"Validation+dedup: {kept} kept, {rejected} rejected, {merged} merged"
            )
            return validated

        except Exception as e:
            logger.error(f"Ollama batch validation failed: {e}")
            return self._fallback_validate(new_facts, existing_facts)

    def _fallback_validate(
        self, new_facts: list[PersonFact], existing_facts: list[PersonFact]
    ) -> list[PersonFact]:
        """Rule-based validation when Ollama is unavailable."""
        # Build set of normalized existing values for dedup
        existing_normalized = set()
        for fact in existing_facts:
            normalized = re.sub(r'[^\w\s]', '', fact.value.lower())
            normalized = re.sub(r'\s+', ' ', normalized).strip()
            existing_normalized.add(f"{fact.category}:{normalized}")

        # Universal patterns to reject
        universal_pattern = re.compile(
            r'^has (a )?(mother|father|parents|family|siblings?|brother|sister)$',
            re.IGNORECASE,
        )

        validated = []
        for fact in new_facts:
            value = fact.value.strip()

            # Reject short values
            if len(value.split()) < 4:
                logger.debug(f"Fallback rejected (too short): {value}")
                continue

            # Reject boolean values
            if value.lower() in ("true", "false", "yes", "no"):
                logger.debug(f"Fallback rejected (boolean): {value}")
                continue

            # Reject universal patterns
            if universal_pattern.match(value):
                logger.debug(f"Fallback rejected (universal): {value}")
                continue

            # Skip exact normalized-value matches against existing facts
            normalized = re.sub(r'[^\w\s]', '', value.lower())
            normalized = re.sub(r'\s+', ' ', normalized).strip()
            norm_key = f"{fact.category}:{normalized}"
            if norm_key in existing_normalized:
                logger.debug(f"Fallback rejected (duplicate): {value}")
                continue

            # Cap confidence
            fact.confidence = min(fact.confidence, 0.7)
            validated.append(fact)

        logger.info(f"Fallback validation: {len(validated)}/{len(new_facts)} kept")
        return validated

    # Expanded stopwords: common verbs, adverbs, prepositions — so content words
    # focus on actual topic nouns (names, places, activities, objects).
    _STOPWORDS = {
        # articles/pronouns/prepositions
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "has", "have", "had", "do", "does", "did", "at", "in", "on", "to",
        "for", "of", "with", "and", "or", "but", "not", "that", "this",
        "it", "its", "by", "from", "as", "into", "about", "who", "their",
        "they", "them", "she", "he", "her", "his", "we", "our", "my", "me",
        "you", "your", "i", "up", "out", "so", "if", "no", "yes", "also",
        "very", "just", "more", "most", "some", "any", "all", "each", "every",
        "both", "few", "many", "much", "own", "other", "than", "then", "when",
        "where", "how", "what", "which", "there", "here", "after", "before",
        # common verbs/adverbs (not meaningful for topic matching)
        "goes", "went", "going", "get", "gets", "got", "getting",
        "works", "working", "worked", "uses", "using", "used",
        "likes", "liked", "enjoys", "enjoyed", "loves", "loved",
        "interested", "participates", "attends", "attended", "attending",
        "lives", "lived", "living", "plays", "played", "playing",
        "signed", "recently", "regularly", "currently", "often", "sometimes",
        "always", "never", "really", "quite", "pretty", "especially",
        "named", "called", "known",
    }

    @classmethod
    def _get_content_words(cls, text: str) -> set[str]:
        """Extract meaningful content words from text (lowercase, no stopwords)."""
        words = set(re.sub(r'[^\w\s]', '', text.lower()).split())
        return words - cls._STOPWORDS

    def _word_overlap_dedup(
        self, candidates: list[PersonFact], existing_facts: list[PersonFact]
    ) -> list[PersonFact]:
        """
        Remove candidates whose content words heavily overlap with existing facts.

        Uses Jaccard-like word overlap within the same category. A candidate is
        dropped if its content words overlap >= 60% with any existing fact in the
        same category. Among candidates themselves, keeps the longest (most detailed).
        """
        # Build existing word sets by category
        existing_by_cat: dict[str, list[tuple[set[str], PersonFact]]] = {}
        for fact in existing_facts:
            words = self._get_content_words(fact.value)
            if fact.category not in existing_by_cat:
                existing_by_cat[fact.category] = []
            existing_by_cat[fact.category].append((words, fact))

        # First pass: remove candidates that duplicate existing facts
        survivors = []
        for candidate in candidates:
            cand_words = self._get_content_words(candidate.value)
            if not cand_words:
                survivors.append(candidate)
                continue

            is_dup = False
            for existing_words, existing_fact in existing_by_cat.get(candidate.category, []):
                if not existing_words:
                    continue
                overlap = len(cand_words & existing_words)
                smaller = min(len(cand_words), len(existing_words))
                if smaller > 0 and overlap / smaller >= 0.6:
                    logger.debug(
                        f"Word-overlap dedup: '{candidate.value[:50]}' "
                        f"overlaps with existing '{existing_fact.value[:50]}'"
                    )
                    is_dup = True
                    break
            if not is_dup:
                survivors.append(candidate)

        # Second pass: dedup candidates against each other (keep longest)
        # Sort longest first so the most detailed version wins
        survivors.sort(key=lambda f: len(f.value), reverse=True)
        final = []
        final_word_sets: list[tuple[set[str], PersonFact]] = []

        for candidate in survivors:
            cand_words = self._get_content_words(candidate.value)
            is_dup = False
            for kept_words, kept_fact in final_word_sets:
                if candidate.category != kept_fact.category:
                    continue
                if not cand_words or not kept_words:
                    continue
                overlap = len(cand_words & kept_words)
                smaller = min(len(cand_words), len(kept_words))
                if smaller > 0 and overlap / smaller >= 0.6:
                    logger.debug(
                        f"Word-overlap dedup (intra): '{candidate.value[:50]}' "
                        f"overlaps with kept '{kept_fact.value[:50]}'"
                    )
                    is_dup = True
                    break
            if not is_dup:
                final.append(candidate)
                final_word_sets.append((cand_words, candidate))

        removed = len(candidates) - len(final)
        if removed:
            logger.info(f"Word-overlap dedup: removed {removed}, kept {len(final)}")
        return final

    def _build_extraction_prompt(
        self, person_name: str, interaction_text: str,
        existing_facts: Optional[list[PersonFact]] = None,
    ) -> str:
        """Build the strict LLM prompt for fact extraction."""
        # Uses LIFEOS_USER_NAME from settings (see config/settings.py)
        user = settings.user_name
        user_upper = user.upper()
        person_upper = person_name.upper()

        # Build existing facts block to avoid re-extraction
        existing_block = ""
        if existing_facts:
            lines = []
            for f in existing_facts:
                lines.append(f"- [{f.category}] {f.value}")
            existing_block = f"""
ALREADY KNOWN FACTS (do NOT re-extract these or variations of them):
{chr(10).join(lines)}

Focus on finding NEW facts not covered above. If an interaction confirms an existing fact, skip it.
"""

        return f"""Analyze these interactions and extract ONLY facts about {person_name} (the contact person).
{existing_block}
CONTEXT: These messages are between {user} (the user) and {person_name}.

CRITICAL - MESSAGE SENDER LABELS:
Messages are labeled with who sent them:
- [{user_upper} SENT]: {user} (the user) wrote this message. ANY fact stated here is about {user_upper}, NOT {person_name}. NEVER extract these as facts about {person_name}.
- [{person_upper} SENT]: {person_name} wrote this message. Facts stated here ARE about {person_name}. These can be extracted.

EXAMPLES OF CORRECT ATTRIBUTION:
- [{user_upper} SENT]: "I'm in Minnesota" → {user} is in Minnesota. DO NOT extract as fact about {person_name}.
- [{person_upper} SENT]: "I'm in Minnesota" → {person_name} is in Minnesota. Extract this fact.
- [{user_upper} SENT]: "I imagine you're in Minnesota" → {user} thinks {person_name} might be there. NOT a confirmed fact.
- [{person_upper} SENT]: "Just landed in Denver" → {person_name} is in Denver. Extract this fact.

CRITICAL - ENTITY ATTRIBUTION:
- If {user} says "my daughter" → This is {user}'s daughter, NOT {person_name}'s. DO NOT extract.
- If {user} says "my wife" or "my partner" → This is {user}'s partner. Only extract if {person_name} IS that partner.
- If {person_name} says "my daughter Emma" → This IS {person_name}'s daughter. Extract it.
- If they discuss a third person "Sarah got a new job" → This is about Sarah, NOT {person_name}. DO NOT extract.

CRITICAL RULES:
1. CHECK THE SENDER LABEL before extracting any fact from a message
2. ONLY extract facts that {person_name} states about themselves
3. Each fact MUST have a verbatim quote as evidence
4. The quote must come from a message {person_name} sent (labeled [{person_upper} SENT])
5. If unsure who a fact applies to, DO NOT extract it

Return ONLY valid JSON with this structure (no markdown, no explanation):
{{
  "facts": [
    {{
      "category": "family",
      "value": "Married to Sarah, they went hiking together",
      "quote": "my wife Sarah and I went hiking",
      "source_id": "abc123"
    }}
  ]
}}

VALUE QUALITY RULES:
- The "value" field is displayed directly to the user. Write it as a readable phrase or sentence.
- MUST contain at least one specific detail (a name, place, date, or quantifiable detail).
- NEVER use boolean values ("true", "false", "yes", "no").
- NEVER use bare names ("Emma") or fragments ("monthly onsites").
- GOOD: "Has a daughter named Emma who plays soccer"
- GOOD: "Allergic to shellfish"
- GOOD: "Grew up in Portland, Oregon"
- BAD: "Emma" (bare name — not a sentence)
- BAD: "true" (boolean — write what the fact actually is)
- BAD: "monthly West Coast onsites" (fragment — write "Travels to the West Coast monthly for onsites")

EXTRACT GENEROUSLY — aim for 10-20 facts per person. It's better to extract too many (the validation step will filter) than to miss interesting details. Extract every specific, personal detail you can find.

NEVER EXTRACT these (obvious/universal — not useful):
- Having parents, siblings, or family in general (unless you know NAMES or specifics)
- Having a job (unless you know the specific role/company)
- Living somewhere (unless you know WHERE)
- Liking food/music/travel in general (unless you know WHAT specifically)
- Any fact that would be true of most people (e.g., "has a mother", "eats food", "uses a phone")

VALUE TIERS — extract in priority order:
Tier 1 (extract first — high recall value):
- Pet names, children's names and details
- Specific hobbies, health mentions, strong preferences

Tier 2 (useful context):
- Hometown, spouse name, birthday, school/alma_mater
- Siblings by name, parents by name, anniversary dates

Tier 3 (only if nothing better available):
- Job title, company, generic interests
- If you have 10+ Tier 1/2 facts, skip Tier 3 entirely

Categories: family, preferences, background, interests, dates, work, topics, travel

EXTRACTION RULES:
- Only include facts with clear textual evidence about {person_name} specifically
- The "quote" field MUST show this fact belongs to {person_name}
- The "source_id" field should match the ID shown in the interaction (e.g., "ID:abc123")
- Reject vague facts without specific names or details
- Reject any fact about the user or third parties mentioned in conversation

GOOD examples:
- [{person_name.upper()} SENT]: "I'm taking my daughter Emma to soccer practice"
- {{"category": "family", "value": "Has a daughter named Emma who plays soccer", "quote": "I'm taking my daughter Emma to soccer practice", "source_id": "abc123"}}

- [{person_name.upper()} SENT]: "I can't eat shellfish, I'm allergic"
- {{"category": "preferences", "value": "Allergic to shellfish", "quote": "I can't eat shellfish, I'm allergic", "source_id": "abc456"}}

BAD examples (DO NOT do this):
- {{"value": "true"}} <- USELESS boolean. Write what the fact actually is.
- {{"value": "Emma"}} <- Terse. Write "Has a daughter named Emma" instead.
- {{"value": "monthly West Coast onsites"}} <- Fragment. Write "Travels to the West Coast monthly for onsites" instead.
- [{user_upper} SENT]: "I'm in Minnesota" — {{"value": "Lives in Minnesota"}} <- WRONG! The user said this, not {person_name}!
- [{user_upper} SENT]: "I need to pick up my daughter" — {{"value": "Has a daughter"}} <- WRONG! This is the user's child!

Interactions:
{interaction_text}"""

    def _parse_extraction_response(
        self, response_text: str, person_id: str, interaction_lookup: dict
    ) -> list[PersonFact]:
        """Parse the LLM response into PersonFact objects with source attribution."""
        facts = []

        # Try to extract JSON from response
        try:
            # Handle potential markdown code blocks
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()

            data = json.loads(response_text)

            if "facts" not in data:
                logger.warning("No 'facts' key in response")
                return facts

            for fact_data in data["facts"]:
                category = fact_data.get("category", "").lower()
                value = fact_data.get("value", "")
                quote = fact_data.get("quote", "")
                source_id = fact_data.get("source_id", "")

                # Validate category
                if category not in FACT_CATEGORIES:
                    logger.warning(f"Unknown category: {category}")
                    continue

                # Validate required fields
                if not value:
                    continue

                # Auto-generate key from category + value
                key = self._generate_fact_key(category, value)

                # Placeholder confidence — real confidence assigned by Ollama validation
                confidence = 0.7

                # Find source interaction for link
                source_link = None
                source_interaction_id = None
                if source_id:
                    interaction = interaction_lookup.get(source_id)
                    if interaction:
                        source_link = interaction.get("source_link")
                        source_interaction_id = source_id
                    else:
                        # Try to find by partial match
                        for int_id, interaction in interaction_lookup.items():
                            if source_id in int_id or int_id in source_id:
                                source_link = interaction.get("source_link")
                                source_interaction_id = int_id
                                break

                # If no source found, use first interaction from lookup as fallback
                if not source_interaction_id and interaction_lookup:
                    fallback_id = next(iter(interaction_lookup))
                    source_interaction_id = fallback_id
                    source_link = interaction_lookup[fallback_id].get("source_link")

                fact = PersonFact(
                    person_id=person_id,
                    category=category,
                    key=key,
                    value=str(value),
                    confidence=confidence,
                    source_interaction_id=source_interaction_id,
                    source_quote=quote if quote else None,
                    source_link=source_link,
                )
                facts.append(fact)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            logger.debug(f"Response was: {response_text[:500]}")
        except Exception as e:
            logger.error(f"Error parsing facts: {e}")

        return facts

    def _generate_relationship_summaries(
        self, person_id: str, person_name: str, interactions: list, model: str
    ) -> list[PersonFact]:
        """
        Generate relationship summary facts.

        Creates high-level insights about:
        - Relationship trajectory
        - Key themes
        - Major events
        - Communication style
        """
        # Format a condensed view of interactions for summary
        summary_text = self._format_interactions_for_summary(interactions)

        prompt = f"""Analyze these interactions with {person_name} and provide relationship insights.

Return ONLY valid JSON with this structure (no markdown, no explanation):
{{
  "summaries": [
    {{
      "key": "relationship_trajectory",
      "value": "Started as professional contact, evolved to close friend over 2 years",
      "evidence": "First interaction was a work meeting in 2022, recent interactions include personal topics"
    }},
    {{
      "key": "key_themes",
      "value": "Technology, hiking, family updates",
      "evidence": "Recurring mentions of tech projects, outdoor activities, and family events"
    }},
    {{
      "key": "major_events",
      "value": "Collaborated on Project X, attended their wedding",
      "evidence": "Multiple references to working together on Project X, invitation to wedding in 2023"
    }},
    {{
      "key": "communication_style",
      "value": "Informal, emoji-heavy, quick responses",
      "evidence": "Most messages are casual in tone with frequent emoji usage"
    }}
  ]
}}

Summary keys to generate:
- relationship_trajectory: How the relationship has evolved over time
- key_themes: Recurring topics in conversations (3-5 themes)
- major_events: Important shared experiences or milestones
- communication_style: How you typically interact

Rules:
- Base summaries on patterns across multiple interactions
- Keep values concise but informative (10-30 words)
- The evidence field should describe what interactions support this summary
- Only include summaries you can support with evidence

Interactions:
{summary_text}"""

        try:
            response = self.client.messages.create(
                model=model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}]
            )

            response_text = response.content[0].text

            # Handle markdown code blocks
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()

            data = json.loads(response_text)
            summaries = []

            for summary_data in data.get("summaries", []):
                key = summary_data.get("key", "")
                value = summary_data.get("value", "")
                evidence = summary_data.get("evidence", "")

                if not key or not value:
                    continue

                fact = PersonFact(
                    person_id=person_id,
                    category="summary",
                    key=key,
                    value=value,
                    confidence=0.8,  # Summaries are synthesized, so moderate confidence
                    source_quote=evidence,
                    source_interaction_id=interactions[0].get("id") if interactions else None,
                    source_link=interactions[0].get("source_link") if interactions else None,
                )
                summaries.append(fact)

            return summaries

        except Exception as e:
            logger.error(f"Failed to generate summaries for {person_name}: {e}")
            return []

    def _format_interactions_for_summary(self, interactions: list) -> str:
        """Format interactions in a condensed way for summary generation."""
        lines = []

        # Group by year/month for temporal context
        by_period: dict[str, list] = {}
        for interaction in interactions:
            timestamp = interaction.get("timestamp", "")
            if timestamp:
                period = timestamp[:7]  # YYYY-MM
            else:
                period = "unknown"

            if period not in by_period:
                by_period[period] = []
            by_period[period].append(interaction)

        # Format grouped interactions
        for period in sorted(by_period.keys(), reverse=True):
            period_interactions = by_period[period]
            lines.append(f"\n--- {period} ({len(period_interactions)} interactions) ---")

            for interaction in period_interactions[:20]:  # Limit per period
                source_type = interaction.get("source_type", "")
                title = interaction.get("title", "")
                snippet = interaction.get("snippet", "")[:200] if interaction.get("snippet") else ""

                lines.append(f"[{source_type}] {title}")
                if snippet:
                    lines.append(f"  {snippet}")

        return "\n".join(lines[:200])  # Limit total lines


# Singleton instances
_fact_store: Optional[PersonFactStore] = None
_fact_extractor: Optional[PersonFactExtractor] = None


def get_person_fact_store(db_path: Optional[str] = None) -> PersonFactStore:
    """Get or create the singleton PersonFactStore."""
    global _fact_store
    if _fact_store is None:
        _fact_store = PersonFactStore(db_path)
    return _fact_store


def get_person_fact_extractor() -> PersonFactExtractor:
    """Get or create the singleton PersonFactExtractor."""
    global _fact_extractor
    if _fact_extractor is None:
        _fact_extractor = PersonFactExtractor()
    return _fact_extractor
