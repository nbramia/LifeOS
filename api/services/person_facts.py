"""
Person Facts Service for LifeOS CRM.

Extracts and stores interesting facts about contacts using a multi-stage LLM pipeline.
Facts are stored in SQLite and can be displayed in the CRM UI.

Pipeline Architecture (v2):
- Stage 1: Filter interactions using local Ollama (fast, cheap)
- Stage 2: Extract candidate facts using Claude (accurate, expensive)
- Stage 3: Validate facts and assign confidence using Ollama (local)

Key improvements over v1:
- Focus on MEMORABLE facts (pet names, hobbies) not obvious ones (job titles)
- Calibrated confidence based on evidence strength, not LLM self-assessment
- Message context windows for better extraction from conversations
- Significant cost reduction via local Ollama for filtering/validation
"""
import asyncio
import json
import logging
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

        # Extract facts with Claude
        extracted_facts = self._extract_facts_claude(
            person_id, person_name, enriched_interactions, interaction_lookup, use_model
        )
        logger.info(f"Extracted {len(extracted_facts)} facts for {person_name}")

        # Generate relationship summaries for people with sufficient interactions
        if len(interactions) >= 10:
            try:
                summaries = self._generate_relationship_summaries(
                    person_id, person_name, sampled_interactions, use_model
                )
                extracted_facts.extend(summaries)
            except Exception as e:
                logger.error(f"Failed to generate summaries for {person_name}: {e}")

        # Deduplicate and save facts
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

        extracted_facts = self._extract_facts_claude(
            person_id, person_name, enriched_interactions, interaction_lookup, use_model
        )

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

    def _enrich_with_context(self, interactions: list) -> list:
        """
        Enrich message-based interactions with conversation context.

        For iMessage, WhatsApp, and Slack interactions, fetches surrounding
        messages to provide better context for fact extraction.

        NOTE: Disabled for speed - context enrichment requires N database queries
        where N = number of message-based interactions, which is too slow for
        people with many iMessage/WhatsApp/Slack interactions.
        """
        # Skip context enrichment for now - too slow
        # TODO: Batch context queries for better performance
        return interactions

    def _extract_facts_claude(
        self,
        person_id: str,
        person_name: str,
        interactions: list[dict],
        interaction_lookup: dict,
        model: str,
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

        Returns:
            List of PersonFact objects with calibrated confidence scores
        """
        all_facts = []
        batches = self._create_batches(interactions, self.MAX_INTERACTIONS_PER_BATCH)
        logger.info(f"Processing {len(interactions)} interactions in {len(batches)} batch(es) for {person_name}")

        for batch_idx, batch in enumerate(batches):
            logger.info(f"Batch {batch_idx + 1}/{len(batches)}: {len(batch)} interactions")
            interaction_text = self._format_interactions(batch, person_name)
            prompt = self._build_extraction_prompt(person_name, interaction_text)

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

    def _build_extraction_prompt(self, person_name: str, interaction_text: str) -> str:
        """Build the extraction prompt focused on memorable facts with confidence."""
        return f"""Extract MEMORABLE personal details about {person_name} from these interactions.

YOU ARE A RECALL ASSISTANT, not a biography builder. Extract facts that help remember
personal details about {person_name} - things you couldn't find on LinkedIn or in a quick search.

PRIORITIZE (high value for recall):
- Pet names ("my dog Max", "our cat Luna")
- Hobby specifics ("I've been learning pottery", "training for a triathlon")
- Family member names ("my sister Emma", "my son Jake")
- Personal preferences ("I can't stand cilantro", "I'm a morning person")
- Personal anecdotes ("We went to Costa Rica last year")
- Health/medical mentions ("I have my infusion next week", "my allergies")
- Interests and passions ("I'm obsessed with Formula 1")

SKIP (low value, findable elsewhere):
- Current job title (LinkedIn has this)
- Company name (LinkedIn has this)
- Generic professional info
- Meeting logistics
- Routine scheduling details

CRITICAL - ENTITY ATTRIBUTION:
These are conversations BETWEEN the user and {person_name}.
- If the user says "my daughter" → This is the user's family, NOT {person_name}'s. DO NOT extract.
- If {person_name} says "my daughter Emma" → This IS {person_name}'s daughter. EXTRACT IT.
- If they discuss a third person "Sarah got a new job" → About Sarah, not {person_name}. DO NOT extract.

CONFIDENCE SCORING (based on evidence strength):
- 0.9: Direct quote where {person_name} states the fact explicitly ("My dog's name is Max")
- 0.8: Clear statement in context, minor interpretation needed
- 0.7: Reasonably certain but some ambiguity in context
- 0.6: Likely true based on context but not explicitly stated
- 0.5: Inference from indirect evidence

Return ONLY valid JSON (no markdown, no explanation):
{{
  "facts": [
    {{
      "category": "family",
      "key": "dog_name",
      "value": "Max",
      "confidence": 0.9,
      "quote": "I need to take Max to the vet tomorrow",
      "source_id": "abc123"
    }}
  ]
}}

Categories: family, preferences, background, interests, dates, work, topics, travel

Interactions:
{interaction_text}"""

    def _parse_extraction_response(
        self,
        response_text: str,
        person_id: str,
        interaction_lookup: dict
    ) -> list[PersonFact]:
        """Parse Claude response into PersonFact objects."""
        try:
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
            facts_data = data.get("facts", [])

            facts = []
            for f in facts_data:
                source_id = f.get("source_id")
                fact = PersonFact(
                    person_id=person_id,
                    category=f.get("category", ""),
                    key=f.get("key", ""),
                    value=str(f.get("value", "")),
                    confidence=f.get("confidence", 0.7),
                    source_interaction_id=source_id,
                    source_quote=f.get("quote"),
                    source_link=interaction_lookup.get(source_id, {}).get("source_link"),
                )
                facts.append(fact)

            return facts

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse extraction JSON: {e}")
            return []

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
            sampled.extend(source_interactions[:allocation])

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
                line += f"\n    Content: {snippet[:500]}"

            lines.append(line)

        return "\n\n".join(lines)

    def _build_extraction_prompt(self, person_name: str, interaction_text: str) -> str:
        """Build the strict LLM prompt for fact extraction."""
        # Uses LIFEOS_USER_NAME from settings (see config/settings.py)
        user = settings.user_name
        user_upper = user.upper()
        person_upper = person_name.upper()
        return f"""Analyze these interactions and extract ONLY facts about {person_name} (the contact person).

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
      "key": "spouse_name",
      "value": "Sarah",
      "quote": "my wife Sarah and I went hiking",
      "source_id": "abc123",
      "confidence": 0.95
    }}
  ]
}}

Categories and example keys:
- family: spouse_name, children_count, child_names, parent_names, sibling_names, pet_name
- preferences: food_preference, communication_style, meeting_preference, schedule_preference
- background: hometown, alma_mater, previous_companies, nationality, languages, medication
- interests: hobby, sport, music_taste, book_genre, favorite_team, creative_pursuits
- dates: birthday, anniversary, started_job, important_dates
- work: current_role, company, expertise, projects, team_size, reports_to
- topics: frequent_discussion_topics, concerns, goals, current_focus
- travel: visited_countries, planned_trips, favorite_destination, travel_style

EXTRACTION RULES:
- Only include facts with clear textual evidence about {person_name} specifically
- The "quote" field MUST show this fact belongs to {person_name}
- The "source_id" field should match the ID shown in the interaction (e.g., "ID:abc123")
- Values should be specific (not "sister" but "sister named Jane")
- Use lowercase snake_case for keys
- Reject vague facts without specific names or details
- Reject any fact about the user or third parties mentioned in conversation

Example of GOOD extraction (from [{person_name.upper()} SENT] messages):
- [{person_name.upper()} SENT]: "I'm taking my daughter Emma to soccer practice"
- Fact: {{"category": "family", "key": "daughter_name", "value": "Emma", "quote": "I'm taking my daughter Emma", "confidence": 0.95}}

Example of BAD extraction (DO NOT do this):
- [{user_upper} SENT]: "I'm in Minnesota" <- The user said this, not {person_name}!
- BAD: {{"category": "travel", "key": "location", "value": "Minnesota"}} <- WRONG! This is about the user!

- [{user_upper} SENT]: "I need to pick up my daughter" <- The user said this about their own family
- BAD: {{"category": "family", "key": "child_name", "value": "..."}} <- WRONG! This is the user's child, not {person_name}'s!

- They discuss "Sarah got a new job in Boston"
- BAD: {{"category": "work", "key": "employer", "value": "Boston company"}} <- WRONG! This is about Sarah, not {person_name}!

Interactions:
{interaction_text}"""

    def _parse_facts_response(
        self, response_text: str, person_id: str, interactions: list, interaction_lookup: dict
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
                key = fact_data.get("key", "")
                value = fact_data.get("value", "")
                quote = fact_data.get("quote", "")
                source_id = fact_data.get("source_id", "")
                confidence = float(fact_data.get("confidence", 0.5))

                # Validate category
                if category not in FACT_CATEGORIES:
                    logger.warning(f"Unknown category: {category}")
                    continue

                # Validate required fields
                if not key or not value:
                    continue

                # Require quote for high-confidence facts
                if confidence >= 0.8 and not quote:
                    logger.warning(f"Rejecting high-confidence fact without quote: {key}")
                    confidence = 0.6  # Downgrade confidence

                # Clamp confidence
                confidence = max(0.0, min(1.0, confidence))

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

                # If no source found, use first interaction as fallback
                if not source_interaction_id and interactions:
                    source_interaction_id = interactions[0].get("id")
                    source_link = interactions[0].get("source_link")

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
