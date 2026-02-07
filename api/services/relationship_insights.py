"""
Relationship Insights Service for LifeOS CRM.

Extracts and stores relationship insights from therapy notes, Omi recordings,
and other personal sources using Claude Sonnet. Focused on couples therapy
insights for tracking relationship health and growth.

Key features:
- Reads vault notes from therapy path
- Parses date from note TITLE (yyyymmdd format) for recency
- Uses Claude Sonnet for insight extraction
- Confirm/dismiss functionality like person_facts
- Recency bias: last 3 months weighted 3x
"""
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any

from config.settings import settings
from api.utils.datetime_utils import make_aware as _make_aware
from api.utils.db_paths import get_crm_db_path

logger = logging.getLogger(__name__)

# Insight categories with their icons
INSIGHT_CATEGORIES = {
    # Actionable commitments by person
    "for_me": "🎯",  # Things partner asked for, or things user committed to
    "for_partner": "💜",  # Things user asked for, or things partner committed to
    # Analysis categories
    "growth_patterns": "📈",  # How the relationship has improved
    "recurring_themes": "🔄",  # Topics that come up repeatedly
    "relationship_strengths": "💪",  # Positive patterns to reinforce
    # AI-generated novel suggestions
    "ai_suggestions": "💡",  # Novel ideas from AI that weren't explicitly discussed
}

# Therapy notes path - relative to vault_path
# Configure via LIFEOS_THERAPY_PATH or defaults to Personal/Self-Improvement/Therapy and coaching
THERAPY_VAULT_SUBPATH = os.environ.get(
    "LIFEOS_THERAPY_PATH",
    "Personal/Self-Improvement/Therapy and coaching"
)

def _get_therapy_path() -> str:
    """Get the full therapy notes path from vault + subpath."""
    return str(settings.vault_path / THERAPY_VAULT_SUBPATH)

THERAPY_VAULT_PATH = _get_therapy_path()

# Therapist configurations - can be customized via environment or config
# Names are used for display, aliases are used for matching in notes
# Configure via LIFEOS_*_THERAPIST and LIFEOS_*_THERAPIST_ALIASES (comma-separated)
def _parse_aliases(env_var: str) -> list:
    """Parse comma-separated aliases from env var."""
    val = os.environ.get(env_var, "")
    if val:
        return [a.strip().lower() for a in val.split(",") if a.strip()]
    return []

THERAPISTS = {
    "couples": {
        "name": os.environ.get("LIFEOS_COUPLES_THERAPIST", ""),
        "aliases": _parse_aliases("LIFEOS_COUPLES_THERAPIST_ALIASES"),
        "context": os.environ.get("LIFEOS_COUPLES_THERAPY_CONTEXT", "couples therapy"),
    },
    "personal": {
        "name": os.environ.get("LIFEOS_PERSONAL_THERAPIST", ""),
        "aliases": _parse_aliases("LIFEOS_PERSONAL_THERAPIST_ALIASES"),
        "context": os.environ.get("LIFEOS_PERSONAL_THERAPY_CONTEXT", "personal therapy"),
    },
}

# Legacy constant for backwards compatibility
COUPLES_THERAPIST = THERAPISTS["couples"]["name"]

# Model for insight generation
INSIGHTS_MODEL = "claude-opus-4-5-20251101"


@dataclass
class RelationshipInsight:
    """
    A relationship insight extracted from therapy notes.

    Insights are extracted from couples therapy notes and stored for display
    in the relationship dashboard. Each insight can be confirmed to persist.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    person_id: str = ""  # Partner's ID (from config/family_members.json)
    category: str = ""  # focus_areas, growth_patterns, recurring_themes, action_items, relationship_strengths
    text: str = ""  # The insight text
    source_title: str = ""  # Note title for attribution
    source_link: Optional[str] = None  # Obsidian link to source note
    source_date: Optional[datetime] = None  # Date parsed from note title
    confirmed: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "id": self.id,
            "person_id": self.person_id,
            "category": self.category,
            "text": self.text,
            "source_title": self.source_title,
            "source_link": self.source_link,
            "source_date": self.source_date.isoformat() if self.source_date else None,
            "confirmed": self.confirmed,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "category_icon": INSIGHT_CATEGORIES.get(self.category, "📝"),
        }

    @classmethod
    def from_row(cls, row: tuple) -> "RelationshipInsight":
        """Create RelationshipInsight from SQLite row.

        Column order:
        0: id, 1: person_id, 2: category, 3: text, 4: source_title,
        5: source_link, 6: source_date, 7: confirmed, 8: created_at
        """
        return cls(
            id=row[0],
            person_id=row[1],
            category=row[2],
            text=row[3],
            source_title=row[4],
            source_link=row[5],
            source_date=_make_aware(datetime.fromisoformat(row[6])) if row[6] else None,
            confirmed=bool(row[7]),
            created_at=_make_aware(datetime.fromisoformat(row[8])) if row[8] else datetime.now(timezone.utc),
        )


class RelationshipInsightStore:
    """
    SQLite-backed storage for relationship insights.
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize insight store."""
        self.db_path = db_path or get_crm_db_path()
        self._init_db()

    def _init_db(self):
        """Create the relationship_insights table if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS relationship_insights (
                    id TEXT PRIMARY KEY,
                    person_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source_title TEXT,
                    source_link TEXT,
                    source_date TEXT,
                    confirmed INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Index for person queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationship_insights_person
                ON relationship_insights(person_id)
            """)

            # Index for confirmed queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_relationship_insights_confirmed
                ON relationship_insights(confirmed)
            """)

            conn.commit()
            logger.info(f"Initialized relationship_insights table in {self.db_path}")
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        return sqlite3.connect(self.db_path)

    def get_all(self, person_id: str) -> list[RelationshipInsight]:
        """Get all insights for a person."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT * FROM relationship_insights
                WHERE person_id = ?
                ORDER BY confirmed DESC, created_at DESC
            """, (person_id,))
            return [RelationshipInsight.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_confirmed(self, person_id: str) -> list[RelationshipInsight]:
        """Get only confirmed insights for a person."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT * FROM relationship_insights
                WHERE person_id = ? AND confirmed = 1
                ORDER BY category, created_at DESC
            """, (person_id,))
            return [RelationshipInsight.from_row(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def add(self, insight: RelationshipInsight) -> RelationshipInsight:
        """Add a new insight."""
        conn = self._get_connection()
        try:
            conn.execute("""
                INSERT INTO relationship_insights
                (id, person_id, category, text, source_title, source_link, source_date, confirmed, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                insight.id,
                insight.person_id,
                insight.category,
                insight.text,
                insight.source_title,
                insight.source_link,
                insight.source_date.isoformat() if insight.source_date else None,
                1 if insight.confirmed else 0,
                insight.created_at.isoformat() if insight.created_at else None,
            ))
            conn.commit()
            return insight
        finally:
            conn.close()

    def confirm(self, insight_id: str) -> bool:
        """Mark an insight as confirmed."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                UPDATE relationship_insights
                SET confirmed = 1
                WHERE id = ?
            """, (insight_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete(self, insight_id: str) -> bool:
        """Delete an insight."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM relationship_insights WHERE id = ?", (insight_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_unconfirmed(self, person_id: str, category: Optional[str] = None) -> int:
        """Delete unconfirmed insights for a person, optionally filtered by category."""
        conn = self._get_connection()
        try:
            if category:
                cursor = conn.execute(
                    "DELETE FROM relationship_insights WHERE person_id = ? AND confirmed = 0 AND category = ?",
                    (person_id, category)
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM relationship_insights WHERE person_id = ? AND confirmed = 0",
                    (person_id,)
                )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def get_last_generated(self, person_id: str) -> Optional[datetime]:
        """Get the timestamp of the most recently generated insight."""
        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT MAX(created_at) FROM relationship_insights
                WHERE person_id = ?
            """, (person_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return _make_aware(datetime.fromisoformat(row[0]))
            return None
        finally:
            conn.close()


class RelationshipInsightGenerator:
    """
    Generates relationship insights from therapy notes using Claude.

    Reads vault notes from the therapy path, filters to couples therapy notes,
    and uses Claude Sonnet to extract actionable insights.
    """

    def __init__(self, store: Optional[RelationshipInsightStore] = None):
        """Initialize generator."""
        self.store = store or get_relationship_insight_store()
        self._client: Any = None

    @property
    def client(self):
        """Lazy-load the Anthropic client."""
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def _parse_date_from_title(self, title: str) -> Optional[datetime]:
        """
        Parse date from note title in yyyymmdd format.

        Examples:
        - "Couples Therapy Erica Turner 20230115" -> 2023-01-15
        - "Amy Morgan therapy 20251230" -> 2025-12-30
        """
        # Look for 8-digit date at start of title
        match = re.match(r'^(\d{8})\s', title)
        if match:
            date_str = match.group(1)
            try:
                return datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        # Also try yyyymmdd anywhere in title
        match = re.search(r'(\d{8})', title)
        if match:
            date_str = match.group(1)
            try:
                return datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        return None

    def _identify_therapist(self, title: str, content: str) -> tuple[str, str]:
        """
        Identify which therapist a note is associated with based on title/content.

        Returns tuple of (therapist_type, context_string).
        therapist_type is 'couples', 'personal', or 'unknown'.
        """
        title_lower = title.lower()
        content_lower = content.lower()[:500]  # Only check first 500 chars of content

        for therapist_type, config in THERAPISTS.items():
            for alias in config["aliases"]:
                if alias in title_lower:
                    return therapist_type, config["context"]

        # Fallback: check content if not found in title
        for therapist_type, config in THERAPISTS.items():
            for alias in config["aliases"]:
                if alias in content_lower:
                    return therapist_type, config["context"]

        # Check for generic therapy keywords
        if "couples therapy" in title_lower or "couples therapy" in content_lower:
            return "couples", THERAPISTS["couples"]["context"]

        return "unknown", "therapy session"

    def _read_vault_notes(self) -> list[dict]:
        """
        Read therapy notes from vault.

        Returns list of dicts with: title, content, path, date, therapist_type, therapist_context, is_couples_therapy
        """
        notes = []
        therapy_path = Path(THERAPY_VAULT_PATH)

        if not therapy_path.exists():
            logger.warning(f"Therapy vault path does not exist: {therapy_path}")
            return notes

        # Walk through all markdown files
        for md_file in therapy_path.rglob("*.md"):
            try:
                title = md_file.stem  # Filename without extension
                content = md_file.read_text(encoding='utf-8')

                # Parse date from title
                note_date = self._parse_date_from_title(title)

                # Identify therapist from title (primary) or content (fallback)
                therapist_type, therapist_context = self._identify_therapist(title, content)
                is_couples = therapist_type == "couples"

                # Create obsidian link
                obsidian_link = f"obsidian://open?vault=Notes%202025&file={md_file.relative_to(therapy_path.parent.parent).as_posix()}"

                notes.append({
                    "title": title,
                    "content": content,
                    "path": str(md_file),
                    "date": note_date,
                    "therapist_type": therapist_type,
                    "therapist_context": therapist_context,
                    "is_couples_therapy": is_couples,
                    "obsidian_link": obsidian_link,
                })
            except Exception as e:
                logger.error(f"Error reading note {md_file}: {e}")

        return notes

    def _weight_notes_by_recency(self, notes: list[dict]) -> list[dict]:
        """
        Weight notes by recency. Notes from last 3 months get 3x weight.

        Returns a potentially expanded list with recent notes repeated.
        """
        now = datetime.now(timezone.utc)
        three_months_ago = now - timedelta(days=90)

        weighted_notes = []
        for note in notes:
            note_date = note.get("date")
            if note_date and note_date > three_months_ago:
                # Recent note - add 3 times
                weighted_notes.extend([note] * 3)
            else:
                weighted_notes.append(note)

        return weighted_notes

    def generate(self, person_id: str, category: Optional[str] = None) -> list[RelationshipInsight]:
        """
        Generate new insights for a relationship.

        If category is specified, only generates for that specific category.
        Otherwise generates for all categories.

        1. Get existing confirmed insights (to avoid duplicates)
        2. Delete unconfirmed insights (for target category or all)
        3. Read and filter vault notes
        4. Use Claude to extract new insights
        5. Save new insights
        """
        # Get all existing insights
        all_existing = self.store.get_all(person_id)
        confirmed = [i for i in all_existing if i.confirmed]
        confirmed_texts = {i.text.lower().strip() for i in confirmed}
        confirmed_themes = set()
        for i in confirmed:
            words = re.findall(r'\b\w+\b', i.text.lower())
            for j in range(len(words) - 1):
                confirmed_themes.add(" ".join(words[j:j+2]))

        # Delete unconfirmed insights (only for target category if specified)
        deleted_count = self.store.delete_unconfirmed(person_id, category=category)
        logger.info(f"Deleted {deleted_count} unconfirmed insights for {person_id}" +
                   (f" (category: {category})" if category else ""))

        # Read vault notes
        all_notes = self._read_vault_notes()
        logger.info(f"Found {len(all_notes)} total therapy notes")

        # Filter to couples therapy notes
        couples_notes = [n for n in all_notes if n.get("is_couples_therapy")]
        logger.info(f"Found {len(couples_notes)} couples therapy notes")

        if not couples_notes:
            logger.warning("No couples therapy notes found")
            return all_existing if not category else [i for i in all_existing if i.category == category]

        # Weight by recency
        weighted_notes = self._weight_notes_by_recency(couples_notes)

        # Build prompt with note content
        notes_text = self._format_notes_for_prompt(weighted_notes[:50])

        # Build exclusion text (only for the target category if specified)
        exclusion_text = ""
        if confirmed_themes:
            if category:
                category_confirmed = [i for i in confirmed if i.category == category]
                cat_themes = set()
                for i in category_confirmed:
                    words = re.findall(r'\b\w+\b', i.text.lower())
                    for j in range(len(words) - 1):
                        cat_themes.add(" ".join(words[j:j+2]))
                if cat_themes:
                    exclusion_text = f"\n\nAVOID generating insights about these already-confirmed topics: {', '.join(list(cat_themes)[:20])}"
            else:
                exclusion_text = f"\n\nAVOID generating insights about these already-confirmed topics: {', '.join(list(confirmed_themes)[:20])}"

        # Build prompt (category-specific or all)
        prompt = self._build_generation_prompt(notes_text, exclusion_text, category=category)

        # Call Claude
        try:
            response = self.client.messages.create(
                model=INSIGHTS_MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}]
            )

            response_text = response.content[0].text
            new_insights = self._parse_response(response_text, person_id, couples_notes, target_category=category)

            # Filter out duplicates
            unique_insights = []
            for insight in new_insights:
                if insight.text.lower().strip() not in confirmed_texts:
                    unique_insights.append(insight)

            # Save new insights
            for insight in unique_insights:
                self.store.add(insight)

            logger.info(f"Generated {len(unique_insights)} new insights for {person_id}" +
                       (f" (category: {category})" if category else ""))

            # Return updated insights
            if category:
                # Return all insights for this category (confirmed + new)
                return [i for i in confirmed if i.category == category] + unique_insights
            else:
                return confirmed + unique_insights

        except Exception as e:
            logger.error(f"Failed to generate insights: {e}")
            if category:
                return [i for i in confirmed if i.category == category]
            return confirmed

    def _format_notes_for_prompt(self, notes: list[dict]) -> str:
        """Format notes for the prompt, deduplicating weighted entries."""
        seen_paths = set()
        lines = []

        for note in notes:
            path = note.get("path", "")
            if path in seen_paths:
                continue
            seen_paths.add(path)

            title = note.get("title", "Untitled")
            content = note.get("content", "")
            date = note.get("date")
            date_str = date.strftime("%Y-%m-%d") if date else "unknown date"
            therapist_context = note.get("therapist_context", "therapy session")

            # Truncate content if too long
            if len(content) > 3000:
                content = content[:3000] + "..."

            lines.append(f"=== {title} ({date_str}) [{therapist_context}] ===\n{content}\n")

        return "\n".join(lines)

    def _build_generation_prompt(self, notes_text: str, exclusion_text: str, category: Optional[str] = None) -> str:
        """Build the Claude prompt for insight generation, optionally for a specific category."""
        from config.settings import settings

        user_name = settings.user_name if settings.user_name else "User"
        partner_name = settings.partner_name if settings.partner_name else "Partner"

        # Category-specific prompts
        category_prompts = {
            "for_me": f"""Focus ONLY on extracting insights for the "for_me" category.

This category is about things {user_name} should work on:
- Requests {partner_name} has made ("{partner_name} asked {user_name} to...")
- Commitments {user_name} made ("{user_name} said he would...")
- Therapist recommendations for {user_name} ("Therapist suggested {user_name}...")

Be very specific about what was actually asked or promised. Quote or closely paraphrase.
Extract 4-6 actionable insights.
Include source_title for each insight.""",

            "for_partner": f"""Focus ONLY on extracting insights for the "for_partner" category.

This category is about things {partner_name} should work on:
- Requests {user_name} has made ("{user_name} asked {partner_name} to...")
- Commitments {partner_name} made ("{partner_name} said they would...")
- Therapist recommendations for {partner_name} ("Therapist suggested {partner_name}...")

Be very specific about what was actually asked or promised. Quote or closely paraphrase.
Extract 4-6 actionable insights.
Include source_title for each insight.""",

            "ai_suggestions": """Focus ONLY on extracting insights for the "ai_suggestions" category.

This is YOUR role as an AI therapist - provide novel therapeutic suggestions based on the patterns you observe in these notes. These should be things that weren't explicitly discussed in therapy but could help.

Think like a wise couples therapist offering fresh perspective. Be bold, specific, and creative.
DO NOT just rephrase things already discussed - offer genuinely new ideas.
Extract 4-6 novel suggestions.
Set source_title to null for all insights (these are your original ideas).""",

            "growth_patterns": """Focus ONLY on extracting insights for the "growth_patterns" category.

This category is about concrete improvements observed over time:
- Specific examples of positive change
- Progress on issues that were challenging before
- Skills or dynamics that have strengthened

Reference specific situations from the notes to illustrate growth.
Extract 4-6 insights.
Include source_title for each insight.""",

            "recurring_themes": f"""Focus ONLY on extracting insights for the "recurring_themes" category.

This category is about topics that come up repeatedly in sessions:
- Patterns to be aware of
- Issues that resurface
- Underlying dynamics that keep appearing

Help {user_name} recognize these patterns so they can be mindful of them.
Extract 4-6 insights.
Include source_title for each insight.""",

            "relationship_strengths": """Focus ONLY on extracting insights for the "relationship_strengths" category.

This category is about positive dynamics to reinforce and celebrate:
- What's working well in the relationship
- Strengths identified by the therapist
- Positive patterns that should be maintained

Highlight the good stuff that should be recognized and protected.
Extract 4-6 insights.
Include source_title for each insight.""",
        }

        if category and category in category_prompts:
            # Category-specific prompt
            return f"""Analyze these couples therapy notes and extract relationship insights.

You are helping {user_name} track relationship growth from therapy sessions with {partner_name}.

{category_prompts[category]}
{exclusion_text}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "insights": [
    {{
      "category": "{category}",
      "text": "Specific insight text here",
      "source_title": {"\"20230115 Couples Therapy\"" if category != "ai_suggestions" else "null"}
    }}
  ]
}}

THERAPY NOTES:
{notes_text}"""

        # Full prompt for all categories
        return f"""Analyze these couples therapy notes and extract relationship insights.

You are helping {user_name} track relationship growth and commitments from therapy - both individual therapy and couples therapy sessions with partner {partner_name}. Extract specific, actionable insights organized by who needs to act on them.

CATEGORIES TO EXTRACT:

**COMMITMENTS (most important - be very specific about what was asked/promised):**
- for_me: Things {user_name} should work on. Include:
  - Requests {partner_name} has made ("{partner_name} asked {user_name} to...")
  - Commitments {user_name} made ("{user_name} said they would...")
  - Therapist recommendations for {user_name} ("Therapist suggested {user_name}...")

- for_partner: Things {partner_name} should work on. Include:
  - Requests {user_name} has made ("{user_name} asked {partner_name} to...")
  - Commitments {partner_name} made ("{partner_name} said they would...")
  - Therapist recommendations for {partner_name} ("Therapist suggested {partner_name}...")

**PATTERNS:**
- growth_patterns: Concrete improvements observed over time (with examples)
- recurring_themes: Topics that come up repeatedly - patterns to be aware of
- relationship_strengths: Positive dynamics to reinforce and celebrate

**AI SUGGESTIONS (separate category):**
- ai_suggestions: YOUR novel therapeutic suggestions based on the patterns you observe - things that weren't explicitly discussed but could help. Think like a therapist offering fresh perspective. Be bold and specific.

RULES:
- Be specific and actionable, not generic advice
- For for_me/for_partner: Quote or closely paraphrase what was actually said/requested
- Reference specific situations from the notes
- Keep each insight to 1-2 sentences
- Extract 3-5 insights per category
- Include source_title for all categories EXCEPT ai_suggestions (those have no source)
- For ai_suggestions: Make them genuinely novel - don't just rephrase things already discussed
{exclusion_text}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "insights": [
    {{
      "category": "for_me",
      "text": "{partner_name} asked for more proactive communication about plans - let them know schedule changes before they happen",
      "source_title": "20230115 Couples Therapy"
    }},
    {{
      "category": "for_partner",
      "text": "{user_name} asked for space to decompress after work before diving into heavy topics",
      "source_title": "20230115 Couples Therapy"
    }},
    {{
      "category": "ai_suggestions",
      "text": "Consider a weekly 'state of us' check-in where you each share one appreciation and one growth area - structured but low-pressure",
      "source_title": null
    }}
  ]
}}

THERAPY NOTES:
{notes_text}"""

    def _parse_response(self, response_text: str, person_id: str, notes: list[dict], target_category: Optional[str] = None) -> list[RelationshipInsight]:
        """Parse Claude response into RelationshipInsight objects."""
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
            insights_data = data.get("insights", [])

            # Build lookup for notes by title
            notes_by_title = {}
            for note in notes:
                title = note.get("title", "")
                notes_by_title[title.lower()] = note

            insights = []
            for item in insights_data:
                item_category = item.get("category", "")
                text = item.get("text", "")
                source_title = item.get("source_title", "")

                if not item_category or not text:
                    continue

                if item_category not in INSIGHT_CATEGORIES:
                    logger.warning(f"Unknown category: {item_category}")
                    continue

                # Skip if filtering by category and this doesn't match
                if target_category and item_category != target_category:
                    continue

                # Find matching note for source info
                source_link = None
                source_date = None
                if source_title:
                    note = notes_by_title.get(source_title.lower())
                    if note:
                        source_link = note.get("obsidian_link")
                        source_date = note.get("date")

                insight = RelationshipInsight(
                    person_id=person_id,
                    category=item_category,
                    text=text,
                    source_title=source_title,
                    source_link=source_link,
                    source_date=source_date,
                    confirmed=False,
                )
                insights.append(insight)

            return insights

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse insights JSON: {e}")
            logger.debug(f"Response was: {response_text[:500]}")
            return []


# Singleton instances
_insight_store: Optional[RelationshipInsightStore] = None
_insight_generator: Optional[RelationshipInsightGenerator] = None


def get_relationship_insight_store(db_path: Optional[str] = None) -> RelationshipInsightStore:
    """Get or create the singleton RelationshipInsightStore."""
    global _insight_store
    if _insight_store is None:
        _insight_store = RelationshipInsightStore(db_path)
    return _insight_store


def get_relationship_insight_generator() -> RelationshipInsightGenerator:
    """Get or create the singleton RelationshipInsightGenerator."""
    global _insight_generator
    if _insight_generator is None:
        _insight_generator = RelationshipInsightGenerator()
    return _insight_generator
