"""
Briefings service for LifeOS.

Generates stakeholder briefings by aggregating:
- People metadata from EntityResolver
- Vault notes mentioning the person
- Action items involving the person
- Calendar meetings with the person
- Interaction history
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

from api.services.people import resolve_person_name, PEOPLE_DICTIONARY
from api.services.hybrid_search import HybridSearch
from api.services.task_manager import TaskManager, get_task_manager
from api.services.synthesizer import get_synthesizer
from api.services.entity_resolver import EntityResolver, get_entity_resolver
from api.services.interaction_store import InteractionStore, get_interaction_store

# iMessage imports
try:
    from api.services.imessage import IMessageStore, get_imessage_store
    HAS_IMESSAGE = True
except ImportError:
    HAS_IMESSAGE = False

logger = logging.getLogger(__name__)


@dataclass
class BriefingContext:
    """Context gathered for a stakeholder briefing."""
    person_name: str
    resolved_name: str
    email: Optional[str] = None
    company: Optional[str] = None
    position: Optional[str] = None
    category: str = "unknown"  # work, personal, family
    linkedin_url: Optional[str] = None  # v2: LinkedIn profile

    # Interaction metrics
    meeting_count: int = 0
    email_count: int = 0
    mention_count: int = 0
    last_interaction: Optional[datetime] = None

    # Content
    related_notes: list[dict] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)
    recent_context: list[str] = field(default_factory=list)

    # v2: Interaction history (formatted markdown)
    interaction_history: str = ""

    # iMessage history (formatted markdown)
    imessage_history: str = ""

    # Sources
    sources: list[str] = field(default_factory=list)

    # v2: Entity ID for linking
    entity_id: Optional[str] = None

    # v3: CRM enrichment
    person_facts: list[dict] = field(default_factory=list)  # Extracted facts
    aliases: list[str] = field(default_factory=list)        # Known aliases
    relationship_strength: float = 0.0                       # 0-100 scale
    tags: list[str] = field(default_factory=list)           # User-defined tags
    notes: str = ""                                          # User notes on person


BRIEFING_PROMPT = """You are LifeOS, preparing a stakeholder briefing for Nathan.

Generate a concise, actionable briefing about {person_name} based on the context below.

## Person Metadata
- Name: {resolved_name}
- Aliases: {aliases_text}
- Email: {email}
- Company: {company}
- Position: {position}
- Category: {category}
- Relationship Strength: {relationship_strength}/100
- Tags: {tags_text}
- LinkedIn: {linkedin_url}
- Meetings (90 days): {meeting_count}
- Emails (90 days): {email_count}
- Note mentions: {mention_count}
- Last interaction: {last_interaction}

## User Notes
{notes_text}

## Known Facts About This Person
{person_facts_text}

## Interaction History
{interaction_history}

## Recent Messages (iMessage/SMS)
{imessage_history}

## Related Notes
{related_notes_text}

## Action Items
{action_items_text}

---

Generate a briefing in this exact format:

## {resolved_name} — Briefing

**Role/Relationship:** [Infer from notes and metadata - be specific]
**Last Interaction:** [Date and brief context if available]
**Interaction Frequency:** {meeting_count} meetings in past 90 days
{linkedin_line}

### Interaction Timeline
[If interaction history is available, summarize recent touchpoints: emails, meetings, note mentions]
[If not available, omit this section]

### Recent Context
- [2-4 bullet points of key recent information from notes]

### Open Items
- [Any action items involving this person]
- [Decisions pending with them]
- [If none, say "No open items found"]

### Relationship Notes
- [Any personal context from notes: preferences, communication style, shared history]
- [If none found, omit this section]

### Suggested Topics
- [2-3 topics to discuss based on open items and recent context]

---
Sources: [list source files]

Keep it concise and actionable. Focus on what Nathan needs to know for his next interaction."""


class BriefingsService:
    """Service for generating stakeholder briefings."""

    def __init__(
        self,
        hybrid_search: Optional[HybridSearch] = None,
        task_manager: Optional[TaskManager] = None,
        entity_resolver: Optional[EntityResolver] = None,
        interaction_store: Optional[InteractionStore] = None,
        imessage_store: Optional["IMessageStore"] = None,
    ):
        """Initialize briefings service."""
        self._hybrid_search = hybrid_search
        self._task_manager = task_manager
        self._entity_resolver = entity_resolver
        self._interaction_store = interaction_store
        self._imessage_store = imessage_store

    @property
    def hybrid_search(self) -> HybridSearch:
        """Lazy-load hybrid search."""
        if self._hybrid_search is None:
            self._hybrid_search = HybridSearch()
        return self._hybrid_search

    @property
    def task_manager(self) -> TaskManager:
        """Lazy-load task manager."""
        if self._task_manager is None:
            self._task_manager = get_task_manager()
        return self._task_manager

    @property
    def entity_resolver(self) -> EntityResolver:
        """Lazy-load entity resolver."""
        if self._entity_resolver is None:
            self._entity_resolver = get_entity_resolver()
        return self._entity_resolver

    @property
    def interaction_store(self) -> InteractionStore:
        """Lazy-load interaction store."""
        if self._interaction_store is None:
            self._interaction_store = get_interaction_store()
        return self._interaction_store

    @property
    def imessage_store(self) -> Optional["IMessageStore"]:
        """Lazy-load iMessage store."""
        if self._imessage_store is None and HAS_IMESSAGE:
            try:
                self._imessage_store = get_imessage_store()
            except Exception as e:
                logger.debug(f"IMessageStore not available: {e}")
        return self._imessage_store

    def gather_context(self, person_name: str, email: Optional[str] = None) -> Optional[BriefingContext]:
        """
        Gather all context about a person.

        Args:
            person_name: Name to look up (will be resolved)
            email: Optional email for better resolution (v2)

        Returns:
            BriefingContext with all gathered data, or None if person unknown
        """
        # Resolve the person name using v1 system
        resolved = resolve_person_name(person_name)
        if not resolved:
            resolved = person_name.title()

        # Initialize context
        context = BriefingContext(
            person_name=person_name,
            resolved_name=resolved,
        )

        # Resolve person using EntityResolver
        entity = None
        try:
            result = self.entity_resolver.resolve(name=resolved, email=email)
            if result:
                entity = result.entity
                context.entity_id = entity.id
                context.resolved_name = entity.display_name or entity.canonical_name
                context.email = entity.emails[0] if entity.emails else None
                context.company = entity.company
                context.position = entity.position
                context.category = entity.category
                context.linkedin_url = entity.linkedin_url
                context.meeting_count = entity.meeting_count
                context.email_count = entity.email_count
                context.mention_count = entity.mention_count
                context.last_interaction = entity.last_seen
                context.sources.extend(entity.sources)

                # v3: Fetch CRM enrichment data
                context.aliases = entity.aliases or []
                context.tags = entity.tags or []
                context.notes = entity.notes or ""
                context.relationship_strength = entity.relationship_strength or 0.0

                logger.debug(f"Resolved {person_name} via EntityResolver: {entity.canonical_name}")
        except Exception as e:
            logger.warning(f"Could not resolve person: {e}")

        # v3: Get PersonFacts (extracted facts about this person)
        if context.entity_id:
            try:
                from api.services.person_facts import get_person_fact_store
                fact_store = get_person_fact_store()
                facts = fact_store.get_for_person(context.entity_id)
                context.person_facts = [
                    {
                        "category": f.category,
                        "key": f.key,
                        "value": f.value,
                        "confidence": f.confidence,
                        "confirmed": f.confirmed_by_user,
                    }
                    for f in facts if f.confidence >= 0.6
                ]
                logger.debug(f"Loaded {len(context.person_facts)} facts for {person_name}")
            except Exception as e:
                logger.warning(f"Could not get person facts: {e}")

        # Get interaction history from v2 InteractionStore (if available and entity found)
        if self.interaction_store and context.entity_id:
            try:
                context.interaction_history = self.interaction_store.format_interaction_history(
                    context.entity_id, days_back=90, limit=20
                )
            except Exception as e:
                logger.warning(f"Could not get interaction history: {e}")

        # Get iMessage history (if available and entity found)
        if self.imessage_store and context.entity_id:
            try:
                messages = self.imessage_store.get_messages_for_entity(
                    context.entity_id, limit=15
                )
                if messages:
                    context.imessage_history = self._format_imessage_history(messages)
                    if "iMessage" not in context.sources:
                        context.sources.append("iMessage")
            except Exception as e:
                logger.warning(f"Could not get iMessage history: {e}")

        # Search vault for mentions using hybrid search (vector + BM25)
        # Search by person name - ChromaDB doesn't support filtering on JSON array fields
        # so we rely on semantic + keyword search with the person name
        try:
            chunks = self.hybrid_search.search(
                query=resolved,
                top_k=15
            )

            for chunk in chunks:
                context.related_notes.append({
                    "file_name": chunk.get("metadata", {}).get("file_name", "Unknown"),
                    "file_path": chunk.get("metadata", {}).get("file_path", ""),
                    "content": chunk.get("content", "")[:500],  # Truncate for prompt
                    "score": chunk.get("score", 0),
                })
                file_name = chunk.get("metadata", {}).get("file_name", "")
                if file_name and file_name not in context.sources:
                    context.sources.append(file_name)
        except Exception as e:
            logger.warning(f"Could not search vault: {e}")

        # Get action items involving person
        try:
            tasks = self.task_manager.list_tasks(query=resolved, status="todo")
            for t in tasks[:10]:
                context.action_items.append({
                    "task": t.description,
                    "owner": None,
                    "completed": t.status == "done",
                    "due_date": t.due_date,
                    "source_file": t.source_file,
                })
        except Exception as e:
            logger.warning(f"Could not get action items: {e}")

        return context

    def _format_imessage_history(self, messages: list) -> str:
        """
        Format iMessage history for the briefing prompt.

        Args:
            messages: List of IMessageRecord objects

        Returns:
            Formatted markdown string
        """
        if not messages:
            return "_No recent messages._"

        lines = []
        for msg in messages[:15]:  # Limit to 15 most recent
            direction = "→" if msg.is_from_me else "←"
            date_str = msg.timestamp.strftime("%Y-%m-%d %H:%M")
            # Truncate long messages
            text = msg.text[:200] + "..." if len(msg.text) > 200 else msg.text
            # Escape any markdown in the message
            text = text.replace("\n", " ").strip()
            lines.append(f"- **{date_str}** {direction} {text}")

        return "\n".join(lines)

    async def generate_briefing(self, person_name: str, email: Optional[str] = None) -> dict:
        """
        Generate a stakeholder briefing.

        Args:
            person_name: Name of person to brief on
            email: Optional email for better resolution (v2)

        Returns:
            Dict with briefing content and metadata
        """
        # Gather context
        context = self.gather_context(person_name, email=email)

        if not context:
            return {
                "status": "not_found",
                "message": f"I don't have any notes about {person_name}.",
                "person_name": person_name,
            }

        # Check if we have any data
        if not context.related_notes and not context.action_items and not context.email:
            return {
                "status": "limited",
                "message": f"I have limited information about {context.resolved_name}. They appear in my records but I don't have detailed notes.",
                "person_name": context.resolved_name,
                "sources": context.sources,
            }

        # Format related notes for prompt
        related_notes_text = "\n\n".join([
            f"**{note['file_name']}:**\n{note['content']}"
            for note in context.related_notes[:10]  # Limit for prompt size
        ]) or "No related notes found."

        # Format action items for prompt
        action_items_text = "\n".join([
            f"- [{('x' if item['completed'] else ' ')}] {item['task']} (Owner: {item['owner'] or 'Unassigned'})"
            for item in context.action_items
        ]) or "No action items found."

        # Format interaction history
        interaction_history = context.interaction_history or "_No interaction history available._"

        # Format iMessage history
        imessage_history = context.imessage_history or "_No recent messages._"

        # Format LinkedIn line for output
        linkedin_line = ""
        if context.linkedin_url:
            linkedin_line = f"**LinkedIn:** [{context.resolved_name}]({context.linkedin_url})"

        # v3: Format CRM enrichment fields
        aliases_text = ", ".join(context.aliases) if context.aliases else "None known"
        tags_text = ", ".join(context.tags) if context.tags else "None"
        notes_text = context.notes if context.notes else "_No notes._"

        # Format facts by category
        if context.person_facts:
            facts_by_cat = {}
            for fact in context.person_facts:
                cat = fact["category"]
                facts_by_cat.setdefault(cat, []).append(f"- {fact['value']}")
            person_facts_text = "\n".join(
                f"**{cat.title()}:**\n" + "\n".join(items)
                for cat, items in facts_by_cat.items()
            )
        else:
            person_facts_text = "_No facts extracted yet._"

        # Build prompt
        prompt = BRIEFING_PROMPT.format(
            person_name=context.person_name,
            resolved_name=context.resolved_name,
            aliases_text=aliases_text,
            email=context.email or "Unknown",
            company=context.company or "Unknown",
            position=context.position or "Unknown",
            category=context.category,
            relationship_strength=context.relationship_strength,
            tags_text=tags_text,
            linkedin_url=context.linkedin_url or "N/A",
            meeting_count=context.meeting_count,
            email_count=context.email_count,
            mention_count=context.mention_count,
            last_interaction=context.last_interaction.strftime("%Y-%m-%d") if context.last_interaction else "Unknown",
            notes_text=notes_text,
            person_facts_text=person_facts_text,
            interaction_history=interaction_history,
            imessage_history=imessage_history,
            related_notes_text=related_notes_text,
            action_items_text=action_items_text,
            linkedin_line=linkedin_line,
        )

        # Generate briefing with Claude
        try:
            synthesizer = get_synthesizer()
            briefing_content = await synthesizer.get_response(prompt, max_tokens=2048)

            return {
                "status": "success",
                "briefing": briefing_content,
                "person_name": context.resolved_name,
                "metadata": {
                    "email": context.email,
                    "company": context.company,
                    "position": context.position,
                    "linkedin_url": context.linkedin_url,
                    "meeting_count": context.meeting_count,
                    "email_count": context.email_count,
                    "mention_count": context.mention_count,
                    "last_interaction": context.last_interaction.isoformat() if context.last_interaction else None,
                    "entity_id": context.entity_id,
                    # v3: CRM enrichment
                    "relationship_strength": context.relationship_strength,
                    "aliases": context.aliases,
                    "tags": context.tags,
                    "facts_count": len(context.person_facts),
                },
                "sources": context.sources,
                "action_items_count": len(context.action_items),
                "notes_count": len(context.related_notes),
            }
        except Exception as e:
            logger.error(f"Failed to generate briefing: {e}")
            return {
                "status": "error",
                "message": f"Failed to generate briefing: {str(e)}",
                "person_name": context.resolved_name,
            }


# Singleton
_briefings_service: Optional[BriefingsService] = None


def get_briefings_service() -> BriefingsService:
    """Get or create briefings service singleton."""
    global _briefings_service
    if _briefings_service is None:
        _briefings_service = BriefingsService()
    return _briefings_service
