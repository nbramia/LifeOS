"""
Chat helper functions for query processing and message formatting.

Pure functions extracted from chat.py to reduce complexity and improve
testability. These handle query parsing, date extraction, and message formatting.
"""
import dataclasses
import logging
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# Imperative follow-ups: "look in my email", "check my calendar", "search drive"
_IMPERATIVE_FOLLOWUP_RE = re.compile(
    r"^(?:look|check|search|find|try|scan)\b.*"
    r"\b(?:email|gmail|calendar|drive|vault|slack|messages?|notes?|"
    r"for it|for that|again)\b"
)


class ReminderIntentType(Enum):
    """Types of reminder-related intents."""
    CREATE = "create"
    EDIT = "edit"
    LIST = "list"
    DELETE = "delete"


class TaskIntentType(Enum):
    """Types of task-related intents."""
    CREATE = "create"
    EDIT = "edit"
    LIST = "list"
    COMPLETE = "complete"
    DELETE = "delete"


@dataclasses.dataclass
class ActionIntent:
    """Unified intent classification result."""
    category: str  # "compose", "task", "reminder", "ambiguous_task_reminder"
    sub_type: Optional[str] = None  # CREATE/EDIT/LIST/COMPLETE/DELETE value string


# Common words to filter out of search queries
STOP_WORDS = {
    'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
    'ought', 'used', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by',
    'from', 'up', 'about', 'into', 'over', 'after', 'beneath', 'under',
    'above', 'and', 'but', 'or', 'nor', 'so', 'yet', 'both', 'either',
    'neither', 'not', 'only', 'own', 'same', 'than', 'too', 'very',
    'just', 'also', 'now', 'here', 'there', 'when', 'where', 'why',
    'how', 'all', 'each', 'every', 'both', 'few', 'more', 'most',
    'other', 'some', 'such', 'no', 'any', 'i', 'me', 'my', 'myself',
    'we', 'our', 'ours', 'ourselves', 'you', 'your', 'yours', 'yourself',
    'he', 'him', 'his', 'himself', 'she', 'her', 'hers', 'herself',
    'it', 'its', 'itself', 'they', 'them', 'their', 'theirs', 'themselves',
    'what', 'which', 'who', 'whom', 'this', 'that', 'these', 'those',
    'am', 'been', 'being', 'if', 'then', 'else', 'review', 'tell',
    'show', 'find', 'get', 'give', 'look', 'help', 'please', 'thanks',
    'summarize', 'summary', 'reference', 'notes', 'note', 'recent',
    'previous', 'likely', 'talk', 'meeting', 'meetings', 'agenda',
    'agendas', 'doc', 'document', 'google', 'file', 'files',
    'later', 'today', 'tomorrow', 'week', 'month', 'year'
}

# Month name to number mapping
MONTH_MAP = {
    'january': 1, 'jan': 1, 'february': 2, 'feb': 2,
    'march': 3, 'mar': 3, 'april': 4, 'apr': 4,
    'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
    'august': 8, 'aug': 8, 'september': 9, 'sep': 9,
    'october': 10, 'oct': 10, 'november': 11, 'nov': 11,
    'december': 12, 'dec': 12
}


def extract_search_keywords(query: str) -> list[str]:
    """
    Extract meaningful search keywords from a natural language query.

    Removes common words and extracts proper nouns and key terms.

    Args:
        query: Natural language search query

    Returns:
        List of up to 5 keywords suitable for search
    """
    # Extract words, keeping proper nouns (capitalized words)
    words = re.findall(r'\b[A-Za-z]+\b', query)
    keywords = []

    for word in words:
        # Keep capitalized words (likely names) regardless of stop words
        if word[0].isupper() and len(word) > 1:
            keywords.append(word)
        # Keep non-stop words that are at least 3 chars
        elif word.lower() not in STOP_WORDS and len(word) >= 3:
            keywords.append(word)

    # Deduplicate while preserving order
    seen = set()
    unique_keywords = []
    for kw in keywords:
        if kw.lower() not in seen:
            seen.add(kw.lower())
            unique_keywords.append(kw)

    return unique_keywords[:5]  # Limit to top 5 keywords


def expand_followup_query(query: str, conversation_history: list) -> str:
    """
    Expand a follow-up query with context from conversation history.

    Detects short queries with pronouns (our, their, they, them, he, she, it)
    or implicit references and expands them with context from previous messages.

    Args:
        query: The current user query
        conversation_history: List of previous messages in the conversation

    Returns:
        Expanded query with context, or original query if not a follow-up
    """
    if not conversation_history:
        return query

    query_lower = query.lower().strip()

    # Follow-up indicators: short query with pronouns or implicit references
    followup_patterns = [
        "what about", "how about", "and ", "but ",
        "their ", "they ", "them ", "he ", "she ", "it ",
        "our ", "his ", "her ", "its ",
        "the same", "more about", "anything else",
        "what else", "tell me more",
    ]

    is_followup = (
        len(query.split()) < 10 and  # Short query
        (
            any(pattern in query_lower for pattern in followup_patterns)
            or bool(_IMPERATIVE_FOLLOWUP_RE.search(query_lower))
        )
    )

    if not is_followup:
        return query

    # Find the most recent user question that mentions a person or topic
    for msg in reversed(conversation_history):
        if msg.role == "user":
            # Check for person-related queries
            person_patterns = [
                r"(?:with|about|from|to)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
                r"interactions?\s+with\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
            ]

            for pattern in person_patterns:
                match = re.search(pattern, msg.content, re.IGNORECASE)
                if match:
                    person_name = match.group(1).strip()
                    # Avoid matching common words
                    if person_name.lower() not in {'the', 'a', 'an', 'my', 'our', 'their'}:
                        # Expand the query with the person name
                        expanded = f"{query} (regarding {person_name})"
                        return expanded

            # If no person found but previous query exists, reference it
            if len(msg.content) < 200:  # Don't include very long queries
                expanded = f"{query} [Context: previous question was about '{msg.content[:100]}']"
                return expanded
            break

    return query


def detect_compose_intent(query: str) -> bool:
    """
    Detect if the query is asking to compose/draft an email.

    Args:
        query: User query text

    Returns:
        True if the query indicates email composition intent
    """
    query_lower = query.lower()

    # Compose intent patterns
    compose_patterns = [
        "draft an email",
        "draft email",
        "draft a message",
        "compose an email",
        "compose email",
        "write an email",
        "write email",
        "send an email",  # We'll create a draft, not send
        "send email",
        "email to ",  # "email to John about..."
        "write to ",  # "write to Sarah about..."
        "draft to ",
    ]

    return any(pattern in query_lower for pattern in compose_patterns)


def detect_reminder_intent(query: str) -> bool:
    """
    Detect if the query is asking to create a reminder or scheduled notification.

    Args:
        query: User query text

    Returns:
        True if the query indicates reminder creation intent
    """
    query_lower = query.lower()

    # Reminder intent patterns
    reminder_patterns = [
        "remind me",
        "set a reminder",
        "set up a reminder",
        "create a reminder",
        "schedule a reminder",
        "send me a reminder",
        "notify me",
        "alert me",
        "ping me",
        "message me every",
        "text me every",
        "send me a message every",
        "daily reminder",
        "weekly reminder",
        "reminder to",
        "reminder that",
    ]

    return any(pattern in query_lower for pattern in reminder_patterns)


def extract_date_context(query: str) -> Optional[str]:
    """
    Extract date references from query and convert to YYYY-MM-DD format.

    Supports: today, yesterday, this week, specific dates like "January 7"

    Args:
        query: User query text

    Returns:
        Date string in YYYY-MM-DD format, or None if no date found
    """
    query_lower = query.lower()
    today = datetime.now()

    if "today" in query_lower:
        return today.strftime("%Y-%m-%d")
    elif "yesterday" in query_lower:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    elif "this week" in query_lower:
        # Return start of week (Monday)
        start_of_week = today - timedelta(days=today.weekday())
        return start_of_week.strftime("%Y-%m-%d")

    # Check for explicit date patterns like "January 7" or "Jan 7"
    month_pattern = r'(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})'
    match = re.search(month_pattern, query_lower)
    if match:
        month_str, day = match.groups()
        month = MONTH_MAP.get(month_str)
        if month:
            year = today.year
            # If the date is in the future, assume last year
            try:
                date = datetime(year, month, int(day))
                if date > today:
                    date = datetime(year - 1, month, int(day))
                return date.strftime("%Y-%m-%d")
            except ValueError:
                pass

    return None


def extract_message_date_range(query: str) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Extract date range for message queries.

    Supports: last month, last week, in December, this month, past N days/weeks/months,
              lately, recently

    Args:
        query: User query text

    Returns:
        Tuple of (start_date, end_date), or (None, None) if no date range found
    """
    query_lower = query.lower()
    today = datetime.now()

    # "lately" - default to last 30 days
    if "lately" in query_lower:
        start = today - timedelta(days=30)
        return (start, today)

    # "recently" or "recent" - default to last 14 days
    if "recently" in query_lower or "recent " in query_lower:
        start = today - timedelta(days=14)
        return (start, today)

    # "last month" or "past month"
    if "last month" in query_lower or "past month" in query_lower:
        # First day of last month
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        return (last_month_start, last_month_end)

    # "this month"
    if "this month" in query_lower:
        first_of_month = today.replace(day=1, hour=0, minute=0, second=0)
        return (first_of_month, today)

    # "last week" or "past week"
    if "last week" in query_lower or "past week" in query_lower:
        start = today - timedelta(days=7)
        return (start, today)

    # "past N days/weeks/months"
    past_pattern = r'(?:past|last)\s+(\d+)\s+(day|days|week|weeks|month|months)'
    match = re.search(past_pattern, query_lower)
    if match:
        num, unit = match.groups()
        num = int(num)
        if 'day' in unit:
            start = today - timedelta(days=num)
        elif 'week' in unit:
            start = today - timedelta(weeks=num)
        elif 'month' in unit:
            start = today - timedelta(days=num * 30)  # Approximate
        return (start, today)

    # "in December", "in January", etc.
    month_pattern = r'\bin\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b'
    match = re.search(month_pattern, query_lower)
    if match:
        month_str = match.group(1)
        month = MONTH_MAP.get(month_str)
        if month:
            year = today.year
            # If month is in the future, use last year
            if month > today.month:
                year -= 1
            start = datetime(year, month, 1)
            # End of month
            if month == 12:
                end = datetime(year + 1, 1, 1) - timedelta(days=1)
            else:
                end = datetime(year, month + 1, 1) - timedelta(days=1)
            return (start, end)

    return (None, None)


def extract_message_search_terms(query: str, person_name: str) -> Optional[str]:
    """
    Extract search terms for message content from query.

    Looks for patterns like "about X", "regarding X", "mentioning X"

    Args:
        query: User query text
        person_name: Name of person to exclude from search terms

    Returns:
        Search term string, or None if no topic found
    """
    query_lower = query.lower()
    person_lower = person_name.lower()

    # Remove the person's name to focus on topic
    query_without_person = query_lower.replace(person_lower, "").strip()

    # Temporal words that shouldn't be search terms
    temporal_words = {'lately', 'recently', 'today', 'yesterday', 'tomorrow',
                      'last', 'this', 'next', 'week', 'month', 'year', 'now'}

    # Patterns that indicate topic search
    patterns = [
        r'about\s+(.+?)(?:\s+with|\s+in|\s+from|\s*\??\s*$)',
        r'regarding\s+(.+?)(?:\s+with|\s+in|\s+from|\s*\??\s*$)',
        r'mentioning\s+(.+?)(?:\s+with|\s+in|\s+from|\s*\??\s*$)',
        r'discussed?\s+(.+?)(?:\s+with|\s+in|\s+from|\s*\??\s*$)',
        r'talked?\s+about\s+(.+?)(?:\s+with|\s+in|\s+from|\s*\??\s*$)',
        r'talking\s+about\s+(.+?)(?:\s+with|\s+in|\s+from|\s*\??\s*$)',
    ]

    for pattern in patterns:
        match = re.search(pattern, query_without_person)
        if match:
            term = match.group(1).strip()
            # Clean up common words and punctuation
            term = re.sub(r'\b(the|a|an|our|my|her|his|their)\b', '', term).strip()
            term = term.rstrip('?').strip()
            # Filter out temporal words
            if term.lower() in temporal_words:
                return None
            if len(term) > 2:
                return term

    return None


def format_messages_for_synthesis(messages: list, include_sources: bool) -> str:
    """
    Format conversation messages for synthesis prompt.

    Args:
        messages: List of Message objects with role and content
        include_sources: Whether to include source references

    Returns:
        Formatted string for synthesis
    """
    parts = []
    for msg in messages:
        prefix = "User" if msg.role == "user" else "Assistant"
        parts.append(f"{prefix}: {msg.content}")
        if include_sources and msg.sources:
            sources_str = ", ".join(s.get("file_name", "unknown") for s in msg.sources)
            parts.append(f"[Sources: {sources_str}]")
    return "\n\n".join(parts)


def format_raw_qa_section(messages: list) -> str:
    """
    Format raw Q&A section to append to note.

    Args:
        messages: List of Message objects with role and content

    Returns:
        Markdown-formatted conversation section
    """
    parts = ["", "---", "", "## Original Conversation", ""]
    for msg in messages:
        prefix = "**User:**" if msg.role == "user" else "**Assistant:**"
        parts.append(prefix)
        parts.append(msg.content)
        parts.append("")
    return "\n".join(parts)


def classify_reminder_intent(query: str) -> Optional[ReminderIntentType]:
    """
    Classify the type of reminder intent in a query.

    Args:
        query: User query text

    Returns:
        ReminderIntentType or None if not reminder-related
    """
    # Check each intent type in order of specificity
    if detect_reminder_delete_intent(query):
        return ReminderIntentType.DELETE
    if detect_reminder_edit_intent(query):
        return ReminderIntentType.EDIT
    if detect_reminder_list_intent(query):
        return ReminderIntentType.LIST
    if detect_reminder_intent(query):
        return ReminderIntentType.CREATE
    return None


def detect_reminder_edit_intent(query: str) -> bool:
    """
    Detect if the query is asking to edit/update an existing reminder.

    Args:
        query: User query text

    Returns:
        True if the query indicates reminder edit intent
    """
    query_lower = query.lower()

    edit_patterns = [
        "change it to",
        "change that to",
        "change the reminder",
        "change my reminder",
        "move it to",
        "move that to",
        "move the reminder",
        "update the reminder",
        "update my reminder",
        "reschedule the reminder",
        "reschedule my reminder",
        "change the .* reminder",
        "update the .* reminder",
        "move the .* reminder",
        "reschedule the .* reminder",
        "make it ",  # "make it 7pm instead"
        "change to ",
    ]

    for pattern in edit_patterns:
        if re.search(pattern, query_lower):
            return True

    return False


def detect_reminder_list_intent(query: str) -> bool:
    """
    Detect if the query is asking to list/show reminders.

    Args:
        query: User query text

    Returns:
        True if the query indicates reminder list intent
    """
    query_lower = query.lower()

    list_patterns = [
        "what are my reminders",
        "what reminders",
        "show my reminders",
        "show reminders",
        "list my reminders",
        "list reminders",
        "what reminders do i have",
        "my reminders",
        "all reminders",
        "view reminders",
        "see my reminders",
        "see reminders",
        "upcoming reminders",
        "scheduled reminders",
    ]

    return any(pattern in query_lower for pattern in list_patterns)


def detect_reminder_delete_intent(query: str) -> bool:
    """
    Detect if the query is asking to delete/cancel a reminder.

    Args:
        query: User query text

    Returns:
        True if the query indicates reminder delete intent
    """
    query_lower = query.lower()

    delete_patterns = [
        "cancel that reminder",
        "cancel the reminder",
        "cancel my reminder",
        "delete that reminder",
        "delete the reminder",
        "delete my reminder",
        "remove that reminder",
        "remove the reminder",
        "remove my reminder",
        "cancel the .* reminder",
        "delete the .* reminder",
        "remove the .* reminder",
        "delete reminder",
        "cancel reminder",
        "remove reminder",
        "stop the reminder",
        "stop that reminder",
    ]

    for pattern in delete_patterns:
        if re.search(pattern, query_lower):
            return True

    return False


def extract_reminder_topic(query: str) -> Optional[str]:
    """
    Extract the topic/name of a reminder from a query.

    Used for matching existing reminders when editing/deleting.

    Args:
        query: User query like "change the library book reminder to 3pm"

    Returns:
        Topic string like "library book" or None
    """
    query_lower = query.lower()

    # Patterns to extract topic
    patterns = [
        r"the\s+(.+?)\s+reminder",  # "the library book reminder"
        r"my\s+(.+?)\s+reminder",   # "my library book reminder"
        r"reminder\s+(?:about|for)\s+(.+?)(?:\s+to|\s*$)",  # "reminder about library book"
    ]

    for pattern in patterns:
        match = re.search(pattern, query_lower)
        if match:
            topic = match.group(1).strip()
            # Filter out common words that aren't topics
            if topic not in {'that', 'this', 'it', 'the'}:
                return topic

    return None


# Verbs that mean "do something on the filesystem / terminal / browser".
# Each branch is anchored on the verb; the noun after the optional article +
# optional adjective(s) is the actual signal that this is a code-action.
# Up to 3 adjectives between article and noun keeps phrases like
# "create a Python script", "delete all .pyc files", "delete the old logs"
# in the positive set.
_CODE_NOUN_OBJECTS = (
    r"file|files|folder|directory|script|tests?|"
    r"function|class|module|component|page|fixture|migration|"
    r"logs?|cache|branch|stash|server|service"
)
_ARTICLE_AND_ADJECTIVES = r"(?:(?:a|an|the|all|my|some|new|this|that)\s+)?(?:\S+\s+){0,3}"

_CODE_ACTION_VERBS = re.compile(
    r"(?i)\b("
    r"create\s+" + _ARTICLE_AND_ADJECTIVES + r"(?:" + _CODE_NOUN_OBJECTS + r")\b"
    r"|edit\s+" + _ARTICLE_AND_ADJECTIVES + r"\S+\.\w+"
    r"|update\s+" + _ARTICLE_AND_ADJECTIVES + r"(?:code|css|html|config|file|tests?|\S+\.\w+|dashboard)\b"
    r"|fix\s+" + _ARTICLE_AND_ADJECTIVES + r"(?:bug|issue|tests?|error|build|server|"
    r"crash|leak|race|deadlock)\b"
    r"|debug\s+(?:the\s+|my\s+)?\S+"
    r"|implement\s+(?:a|an|the)\s+"
    r"|refactor\s+(?:the\s+|my\s+)?"
    r"|run\s+(?:the\s+)?(?:tests?|build|server|script|npm|pip|"
    r"\S+\.py|\S+\.sh|make|pytest|cargo)\b"
    r"|install\s+\S+"
    r"|upgrade\s+\S+"
    r"|delete\s+" + _ARTICLE_AND_ADJECTIVES + r"(?:files?|logs?|cache|"
    r"branch|stash|\S+\.\w+)\b"
    r"|remove\s+" + _ARTICLE_AND_ADJECTIVES + r"(?:files?|\S+\.\w+)\b"
    r"|rename\s+\S+"
    r"|git\s+commit\b"
    r"|commit\s+(?:my\s+|the\s+|all\s+)?(?:changes|work|fix|update|files?|wip)\b"
    r"|push\s+(?:my\s+|the\s+)?(?:branch|changes|to)\b"
    r"|pull\s+(?:from\s+|the\s+)?(?:main|master|origin)\b"
    r"|merge\s+(?:into\s+|the\s+)?"
    r"|browse\s+to\s+(?:https?:\/\/|\S+\.(?:com|org|net|io|dev|app|co)|localhost|127\.0\.0\.1)"
    r"|navigate\s+to\s+(?:https?:\/\/|\S+\.(?:com|org|net|io|dev|app|co)|localhost|127\.0\.0\.1)"
    r"|check\s+(?:the\s+)?(?:disk\s+usage|memory|cpu|process|logs?|"
    r"service\s+(?:status|logs?|health)|"
    r"server\s+(?:status|logs?|health))\b"
    r"|start\s+(?:the\s+|my\s+)?(?:server|service|container|docker)\b"
    r"|stop\s+(?:the\s+|my\s+)?(?:server|service|container|docker)\b"
    r"|restart\s+(?:the\s+|my\s+)?(?:server|service|container|docker|"
    r"process|browser|node|chrome|firefox|app|build|pod|deployment|"
    r"nginx|database|db|redis|mysql|postgres)\b"
    r"|kill\s+(?:the\s+|process|all)\s+"
    r")"
)

# Questions about code (not actions on it).
_CODE_QUESTION_PREFIXES = re.compile(
    r"(?i)^\s*(?:how\s+(?:do|does|can)|what\s+(?:is|are|does|do)|"
    r"why\s+(?:does|is|do)|when\s+(?:does|is|do)|where\s+(?:does|is)|"
    r"explain|describe|what'?s\s+the\s+difference|"
    r"can\s+you\s+(?:explain|tell\s+me|show\s+me\s+how)|"
    r"tell\s+me\s+(?:about|how|why|what)|"
    # Polite / deliberative question forms — the user is asking, not commanding.
    r"do\s+(?:i|you)\s+|should\s+(?:i|you)\s+|"
    r"could\s+you\s+|can\s+you\s+|can\s+i\s+|"
    r"would\s+you\s+|will\s+you\s+|"
    r"is\s+it\s+|are\s+there\s+|"
    r"have\s+i\s+|has\s+\S+\s+)"
)

# "remind me ..." / "remember to ..." reminder-creation verbs.
_REMINDER_VERBS = re.compile(
    r"(?i)\b(remind\s+me|remember\s+to|don'?t\s+forget|"
    r"add\s+\S.*\s+to\s+my\s+(?:list|to-?do))"
)

# Explicit temporal markers that disambiguate "reminder" from "task".
# If a reminder verb is paired with one of these, the user clearly wants
# a timed notification (not a to-do).
_TEMPORAL_MARKERS = re.compile(
    r"(?i)\b("
    r"at\s+\d|\d+\s*(?:am|pm)|in\s+(?:an?\s+)?(?:hour|minute|day|week|month)s?|"
    r"tomorrow|tonight|today|this\s+(?:morning|afternoon|evening|week|weekend)|"
    r"next\s+(?:week|month|year|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|day|hour|minute)|"
    r"on\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"every\s+(?:hour|day|week|month|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|morning|afternoon|evening)|"
    r"daily|weekly|monthly|nightly"
    r")\b"
)


def _is_claude_intent(query: str) -> bool:
    """Return True if the query asks for an action on the filesystem, terminal,
    or browser (i.e. needs Claude Code), as opposed to a question about code.
    """
    if _CODE_QUESTION_PREFIXES.match(query):
        return False
    return bool(_CODE_ACTION_VERBS.search(query))


def _is_ambiguous_task_reminder(query: str) -> bool:
    """Return True if the query reads as a reminder-or-task but doesn't carry
    an explicit temporal marker. The agent loop has separate `manage_tasks` and
    `manage_reminders` tools — when the user's intent could go either way, we
    short-circuit to a clarification question rather than let the agent guess.
    """
    if not _REMINDER_VERBS.search(query):
        return False
    if _TEMPORAL_MARKERS.search(query):
        return False
    return True


async def classify_action_intent(query: str, conversation_history: list = None) -> Optional[ActionIntent]:
    """Pre-agent intent dispatch — return one of two outcomes that change the
    code path downstream in :func:`api.routes.chat.ask_stream`:

    - ``ActionIntent("claude", None)`` — query needs Claude Code (terminal,
      filesystem, browser). ``ask_stream`` emits a ``claude_intent`` SSE event
      and returns, so the Telegram bot can spawn a Claude Code subprocess.
    - ``ActionIntent("ambiguous_task_reminder", None)`` — query reads as
      "task or timed reminder?" without an explicit time. ``ask_stream``
      short-circuits to a clarification prompt rather than letting the
      agent loop guess between ``manage_tasks`` and ``manage_reminders``.

    Every other intent — compose, task lookup/edit/delete, dated reminders,
    general questions — returns ``None`` so the agent loop's own tools
    (create_email_draft, manage_tasks, manage_reminders, create_calendar_event,
    search_*, …) handle it.

    Pure pattern-matching: no LLM call on the hot path. ``conversation_history``
    is accepted for API stability but currently unused — the only follow-up
    cases ("both", "the second one") are handled by the pending-selection
    machinery elsewhere in ``ask_stream``.
    """
    if _is_claude_intent(query):
        return ActionIntent("claude", None)
    if _is_ambiguous_task_reminder(query):
        return ActionIntent("ambiguous_task_reminder", None)
    return None


# Underscore-prefixed aliases for backward compatibility
_format_messages_for_synthesis = format_messages_for_synthesis
_format_raw_qa_section = format_raw_qa_section
