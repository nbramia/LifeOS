"""
E2E tests for the pre-agent intent classifier.

After PR #89/#87, `classify_action_intent` returns only two categories
that change the downstream code path in `ask_stream`:
  - `code` — query needs Claude Code (terminal/filesystem/browser)
  - `ambiguous_task_reminder` — query reads as "task or reminder?" with
    no temporal marker, so we short-circuit to a clarification prompt

Everything else returns `None` and falls through to the agent loop, which
has dedicated tools (create_email_draft, manage_tasks, manage_reminders,
create_calendar_event, search_*, …) and routes the query itself. The tests
here verify exactly that contract — no LLM is called on the hot path.
"""
import pytest

pytestmark = pytest.mark.unit


class TestCodeIntent:
    """`classify_action_intent` returns `code` for filesystem/terminal/browser actions."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "fix the bug in server.py",
        "create a Python script that does X",
        "update the CSS for the dashboard",
        "run the tests",
        "delete all .pyc files",
        "browse to google.com and search for X",
        "check disk usage",
        "implement a retry helper",
        "refactor the orchestrator",
        "run npm install",
        "commit my changes",
        "delete the old logs",
        "create a file called test.py",
    ])
    async def test_code_action_detected(self, message):
        from api.services.chat_helpers import classify_action_intent
        result = await classify_action_intent(message, [])
        assert result is not None, f"Expected code intent for: {message}"
        assert result.category == "code", f"Got {result.category} for: {message}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        # Questions about code are not actions on code
        "how do I write a for loop in Python",
        "what does this function do",
        "explain the code in server.py",
        "what's the difference between let and const",
        "tell me about server.py",
        "why does the test fail",
        "describe the architecture",
        # Non-code phrases that previously over-matched code-action verbs
        "restart the dishwasher",
        "restart my conversation",
        "restart the workout",
        "browse to the kitchen",
        "navigate to the meeting room",
        "check the service hours of the restaurant",
        "check the status of my order",
        "commit to a decision",
        "commit to the plan",
        # Polite / deliberative question forms — the user is asking, not commanding
        "Do I need to run the tests",
        "Do you think I should commit my changes",
        "Should I delete the cache",
        "Should you commit the changes now",
        "Could you run the tests",
        "Can you run the tests",
        "Can I delete the old logs",
        "Would you mind committing my changes",
        "Will you push the branch",
        "Is it possible to delete the cache",
        "Are there any tests to run",
        "Have I committed my changes",
        "Has the build finished",
    ])
    async def test_non_code_phrases_not_classified_as_code(self, message):
        """Questions about code AND non-code phrases that share verbs (e.g.
        "restart the dishwasher") must not be routed to Claude Code."""
        from api.services.chat_helpers import classify_action_intent
        result = await classify_action_intent(message, [])
        assert result is None or result.category != "code", (
            f"Expected non-code for: {message}, got {result.category if result else None}"
        )


class TestAmbiguousTaskReminder:
    """`classify_action_intent` returns `ambiguous_task_reminder` only when the
    user uses reminder-creation language without a temporal marker."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "remind me to call mom",
        "remember to call mom",
        "don't forget the meeting",
        "add submit taxes to my list",
    ])
    async def test_ambiguous_detected(self, message):
        from api.services.chat_helpers import classify_action_intent
        result = await classify_action_intent(message, [])
        assert result is not None, f"Expected ambiguous intent for: {message}"
        assert result.category == "ambiguous_task_reminder", (
            f"Got {result.category} for: {message}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "remind me at 3pm to call mom",
        "remind me tomorrow to call mom",
        "remind me in an hour to call mom",
        "remind me to call mom every morning",
        "remind me at 9am tomorrow to take meds",
    ])
    async def test_temporal_marker_disambiguates(self, message):
        """If the user includes a clear time, the agent loop handles it as a reminder."""
        from api.services.chat_helpers import classify_action_intent
        result = await classify_action_intent(message, [])
        assert result is None, (
            f"Expected None (agent handles dated reminders) for: {message}, "
            f"got {result.category if result else None}"
        )


class TestEverythingElseFallsThrough:
    """`classify_action_intent` returns None for the categories the agent handles."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        # Tasks — agent uses manage_tasks
        "add a task to review the PR",
        "what tasks do I have",
        "mark the PR review as done",
        "delete the old task",
        # Reminders with explicit time — agent uses manage_reminders
        "set a reminder for tomorrow at 3pm",
        "show my reminders",
        # Compose — agent uses create_email_draft
        "draft an email to John",
        "compose a message to the team",
        # Calendar — agent uses search_calendar / create_calendar_event
        "what's on my calendar tomorrow",
        "schedule a meeting with Alice next week",
        # General questions — agent uses search_*
        "what's the weather",
        "who is the president",
        "what did I discuss with John last week",
    ])
    async def test_agent_handled_intents_return_none(self, message):
        from api.services.chat_helpers import classify_action_intent
        result = await classify_action_intent(message, [])
        assert result is None, (
            f"Expected None (agent handles) for: {message}, "
            f"got {result.category if result else None}"
        )


class TestNoLLMCallOnHotPath:
    """The classifier must not call an LLM — that's the whole point of this change."""

    @pytest.mark.asyncio
    async def test_classify_action_intent_does_not_call_synthesizer(self, monkeypatch):
        # If something tries to use the synthesizer for classification, fail loudly.
        sentinel = []

        def boom(*args, **kwargs):
            sentinel.append(("get_synthesizer", args, kwargs))
            raise RuntimeError("classify_action_intent should not call the synthesizer")

        monkeypatch.setattr("api.services.synthesizer.get_synthesizer", boom)
        from api.services.chat_helpers import classify_action_intent
        # Exercise several messages — none should invoke the synthesizer
        for msg in [
            "fix the bug in server.py",
            "remind me to call mom",
            "remind me at 3pm to call mom",
            "what's the weather",
        ]:
            await classify_action_intent(msg, [])
        assert sentinel == [], f"Synthesizer was called {len(sentinel)} times"
