"""
The native chat stream with person context connected.

Drives real ``POST /api/ask/stream`` turns against throwaway stores, with the
agent loop faked so nothing reaches a model. Every person here is synthetic.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from api.routes import chat, conversations
from api.services import chat_turns
from api.services.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    monkeypatch.setattr(chat, "get_store", lambda: s)
    monkeypatch.setattr(conversations, "get_store", lambda: s)
    return s


@pytest.fixture
def crm(tmp_path, monkeypatch):
    from api.services import entity_resolver, interaction_store, person_entity, person_facts
    from api.services.entity_resolver import EntityResolver
    from api.services.interaction_store import InteractionStore
    from api.services.link_override import LinkOverrideStore
    from api.services.person_entity import PersonEntityStore
    from api.services.person_facts import PersonFactStore

    s = PersonEntityStore(str(tmp_path / "crm.db"))
    overrides = LinkOverrideStore(tmp_path / "crm.db")
    monkeypatch.setattr(entity_resolver, "get_link_override_store", lambda: overrides)
    monkeypatch.setattr(person_entity, "_entity_store", s)
    monkeypatch.setattr(entity_resolver, "_entity_resolver", EntityResolver(s))
    monkeypatch.setattr(interaction_store, "_interaction_store", InteractionStore(str(tmp_path / "interactions.db")))
    monkeypatch.setattr(person_facts, "_fact_store", PersonFactStore(str(tmp_path / "crm.db")))
    return s


def add_person(store, name, strength=30.0):
    from api.services.person_entity import PersonEntity

    person = PersonEntity(canonical_name=name, aliases=[])
    person.relationship_strength = strength
    return store.add(person)


@pytest.fixture
def loop_calls(monkeypatch):
    """Fake the agent loop, recording what it was given."""
    import api.services.agent_loop as agent_loop_mod

    calls = []

    async def fake_loop(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "content": "Hello "}
        yield {"type": "text", "content": "there"}
        yield {"type": "result", "result": SimpleNamespace(
            total_input_tokens=1, total_output_tokens=1, total_cost_usd=0.0,
            model="m", tool_calls_log=[], full_text="Hello there",
        )}

    async def no_intent(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_loop_mod, "run_agent_loop", fake_loop)
    monkeypatch.setattr(chat, "classify_action_intent", no_intent)
    return calls


async def ask(question, conversation_id=None, **extra):
    app = FastAPI()
    app.include_router(chat.router)
    body = {"question": question, **extra}
    if conversation_id:
        body["conversation_id"] = conversation_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as client:
        response = await client.post("/api/ask/stream", json=body, timeout=10)
    frames = [
        json.loads(line[len("data: "):])
        for line in response.text.split("\n")
        if line.startswith("data: ")
    ]
    return response, frames


def frame_types(frames):
    return [f["type"] for f in frames if f["type"] != "perf_trace"]


def stored_routing(store, frames):
    conv_id = next(f["conversation_id"] for f in frames if f["type"] == "conversation_id")
    return store.get_messages(conv_id, limit=10)[-1].routing, conv_id


class TestPromptWiring:
    async def test_a_full_name_reaches_the_model_as_an_asserted_person(self, store, crm, loop_calls):
        person = add_person(crm, "Robin Alpha", 60.0)

        await ask("Who is Robin Alpha?")

        block = loop_calls[0]["personal_context"]
        assert "'Robin Alpha' is Robin Alpha." in block
        assert person.id in block

    async def test_a_shared_first_name_reaches_the_model_as_options(self, store, crm, loop_calls):
        add_person(crm, "Robin Alpha", 60.0)
        add_person(crm, "Robin Beta", 55.0)

        await ask("tell me about Robin")

        block = loop_calls[0]["personal_context"]
        assert "does not identify one person" in block
        assert "Robin Alpha" in block and "Robin Beta" in block
        assert "'Robin' is" not in block

    async def test_a_message_about_no_one_adds_nothing(self, store, crm, loop_calls):
        add_person(crm, "Robin Alpha", 60.0)

        await ask("what is the weather like?")

        assert loop_calls[0]["personal_context"] == ""

    async def test_it_joins_the_persona_people_block_rather_than_replacing_it(
        self, store, crm, loop_calls, monkeypatch
    ):
        monkeypatch.setattr(chat.settings.__class__, "personal_context", lambda self, pid: "Persona people.")
        add_person(crm, "Robin Alpha", 60.0)

        await ask("Who is Robin Alpha?")

        block = loop_calls[0]["personal_context"]
        assert block.startswith("Persona people.\n\n")
        assert "'Robin Alpha' is Robin Alpha." in block

    async def test_the_journal_persona_goes_without(self, store, crm, loop_calls, tmp_path, monkeypatch):
        import api.services.journal_capture as journal_capture_mod
        import api.services.person_context as module
        import config.settings as settings_mod
        from api.routes import vault as vault_route_mod

        root = tmp_path / "vault"
        root.mkdir()
        for obj in (settings_mod.settings, journal_capture_mod.settings, vault_route_mod.settings):
            monkeypatch.setattr(obj, "vault_path", root)
        registry = tmp_path / "bots.json"
        registry.write_text(json.dumps([{
            "name": "journal", "token_env": "TG_JOURNAL_TEST", "persona_file": "config/personas/journal.md",
        }]))
        monkeypatch.setattr("config.settings._TELEGRAM_BOTS_FILE", registry)
        monkeypatch.setenv("TG_JOURNAL_TEST", "tok")
        add_person(crm, "Robin Alpha", 60.0)
        built = []
        monkeypatch.setattr(module, "build_person_context", lambda *args: built.append(args))

        await ask("Who is Robin Alpha?", persona_id="journal")

        assert len(loop_calls) == 1
        assert built == []
        assert "Robin Alpha" not in loop_calls[0]["personal_context"]


class TestFollowUpTurns:
    async def test_the_resolved_person_is_stored_and_settles_the_next_turn(self, store, crm, loop_calls):
        wanted = add_person(crm, "Robin Beta", 40.0)
        add_person(crm, "Robin Alpha", 60.0)

        _, frames = await ask("Who is Robin Beta?")
        routing, conv_id = stored_routing(store, frames)
        assert routing["person"] == "Robin Beta"
        assert routing["person_id"] == wanted.id
        assert routing["person_tier"] == "confident"
        assert {"person_tier", "person", "person_id", "fetch_depth"} & set(routing) == {
            "person_tier",
            "person",
            "person_id",
        }

        await ask("what does Robin do", conversation_id=conv_id)

        block = loop_calls[1]["personal_context"]
        assert "from the recent conversation" in block
        assert wanted.id in block

    async def test_an_unconfirmed_person_is_not_stored_as_the_subject(self, store, crm, loop_calls):
        add_person(crm, "Robin Alpha", 60.0)
        add_person(crm, "Robin Beta", 55.0)

        _, frames = await ask("tell me about Robin")
        routing, _ = stored_routing(store, frames)

        assert {"person_tier", "person", "person_id", "fetch_depth"} & set(routing) == {"person_tier"}
        assert routing["person_tier"] == "uncertain"

    async def test_routing_holds_no_person_fields_without_a_person(self, store, crm, loop_calls):
        _, frames = await ask("what is the weather like?")
        routing, _ = stored_routing(store, frames)

        assert not {"person", "person_id", "person_tier", "fetch_depth"} & set(routing)


class TestStreaming:
    EXPECTED = ["conversation_id", "routing", "content", "content", "usage", "done"]

    async def test_a_person_turn_streams_the_same_frames_as_any_other(self, store, crm, loop_calls):
        add_person(crm, "Robin Alpha", 60.0)

        response, with_person = await ask("Who is Robin Alpha?", include_sources=False)
        _, without_person = await ask("what is the weather like?", include_sources=False)

        assert response.headers["content-type"].startswith("text/event-stream")
        assert frame_types(with_person) == self.EXPECTED
        assert frame_types(without_person) == self.EXPECTED

    async def test_routing_reaches_a_client_while_the_lookup_is_still_running(
        self, store, crm, loop_calls, monkeypatch
    ):
        import api.services.person_context as module

        release = asyncio.Event()

        async def held_gather(question, history=None):
            await release.wait()

        monkeypatch.setattr(module, "gather_person_context", held_gather)

        response = await chat.ask_stream(chat.AskStreamRequest(question="Who is Robin Alpha?"))
        reader = response.body_iterator
        seen = []
        async for raw in reader:
            text = raw.decode() if isinstance(raw, bytes) else raw
            seen += [json.loads(line[6:])["type"] for line in text.split("\n") if line.startswith("data: ")]
            if "routing" in seen:
                break

        assert seen == ["conversation_id", "routing"]
        assert loop_calls == []

        release.set()
        async for raw in reader:
            text = raw.decode() if isinstance(raw, bytes) else raw
            seen += [json.loads(line[6:])["type"] for line in text.split("\n") if line.startswith("data: ")]

        assert "content" in seen and seen[-1] == "done"


class TestDegradation:
    async def test_a_lookup_that_raises_leaves_the_turn_as_it_was(self, store, crm, loop_calls, monkeypatch):
        import api.services.person_context as module

        def boom(*args):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(module, "build_person_context", boom)

        response, frames = await ask("Who is Robin Alpha?", include_sources=False)

        assert response.status_code == 200
        assert frame_types(frames) == TestStreaming.EXPECTED
        assert loop_calls[0]["personal_context"] == ""
        assert "".join(f["content"] for f in frames if f["type"] == "content") == "Hello there"

        routing, _ = stored_routing(store, frames)
        assert "person_tier" not in routing

    async def test_a_lookup_that_overruns_leaves_the_turn_as_it_was(self, store, crm, loop_calls, monkeypatch):
        import api.services.person_context as module

        monkeypatch.setattr(module, "PERSON_CONTEXT_TIMEOUT_SECONDS", 0.05)

        def slow(*args):
            import time

            time.sleep(0.3)
            return "too late"

        monkeypatch.setattr(module, "build_person_context", slow)

        response, frames = await ask("Who is Robin Alpha?", include_sources=False)

        assert response.status_code == 200
        assert frame_types(frames) == TestStreaming.EXPECTED
        assert loop_calls[0]["personal_context"] == ""

    async def test_a_turn_cancelled_during_the_lookup_never_reaches_the_model(
        self, store, crm, loop_calls, monkeypatch
    ):
        import api.services.person_context as module

        async def hanging_gather(question, history=None):
            await asyncio.Event().wait()

        monkeypatch.setattr(module, "gather_person_context", hanging_gather)

        response = await chat.ask_stream(chat.AskStreamRequest(question="Who is Robin Alpha?"))
        reader = response.body_iterator
        conversation_id = None
        async for raw in reader:
            text = raw.decode() if isinstance(raw, bytes) else raw
            for line in text.split("\n"):
                if line.startswith("data: ") and json.loads(line[6:]).get("conversation_id"):
                    conversation_id = json.loads(line[6:])["conversation_id"]
            if "routing" in text:
                break

        app = FastAPI()
        app.include_router(conversations.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as client:
            cancelled = await client.post(f"/api/conversations/{conversation_id}/cancel")

        assert cancelled.json() == {"ok": True, "cancelled": True}
        turn = chat_turns.get_turn_registry().get_by_conversation(conversation_id)
        if turn is not None and turn.task is not None:
            with pytest.raises(asyncio.CancelledError):
                await turn.task
        await reader.aclose()
        assert loop_calls == []


class TestNamesComeFromTheMessage:
    @pytest.mark.parametrize("follow_up", [
        "Are they open on Sunday?",
        "and what about the weather",
        "is it raining there",
        "what time does it close",
        "tell me more",
        "but is the pharmacy open",
    ])
    async def test_a_follow_up_naming_no_one_gets_no_profile(self, store, crm, loop_calls, follow_up):
        add_person(crm, "Robin Alpha", 60.0)

        _, frames = await ask("Tell me about Robin Alpha")
        _, conv_id = stored_routing(store, frames)
        await ask(follow_up, conversation_id=conv_id)

        assert loop_calls[1]["personal_context"] == ""

    async def test_the_lookup_is_given_what_the_user_wrote(self, store, crm, loop_calls, monkeypatch):
        import api.services.person_context as module

        add_person(crm, "Robin Alpha", 60.0)
        seen = []
        real = module.build_person_context

        def recording(question, history=None):
            seen.append(question)
            return real(question, history)

        monkeypatch.setattr(module, "build_person_context", recording)

        _, frames = await ask("Tell me about Robin Alpha")
        _, conv_id = stored_routing(store, frames)
        await ask("Are they open on Sunday?", conversation_id=conv_id)

        assert seen == ["Tell me about Robin Alpha", "Are they open on Sunday?"]

    async def test_a_first_name_is_still_settled_by_the_conversation(self, store, crm, loop_calls):
        wanted = add_person(crm, "Robin Beta", 40.0)
        add_person(crm, "Robin Alpha", 60.0)

        _, frames = await ask("Tell me about Robin Beta")
        _, conv_id = stored_routing(store, frames)
        await ask("When is Robin's birthday?", conversation_id=conv_id)

        assert "from the recent conversation" in loop_calls[1]["personal_context"]
        assert wanted.id in loop_calls[1]["personal_context"]

    async def test_two_people_in_one_thread_are_both_offered_and_neither_stored(self, store, crm, loop_calls):
        add_person(crm, "Robin Alpha", 40.0)
        add_person(crm, "Robin Beta", 40.0)

        _, frames = await ask("Tell me about Robin Alpha")
        _, conv_id = stored_routing(store, frames)
        await ask("Tell me about Robin Beta", conversation_id=conv_id)
        _, frames = await ask("tell me about Robin", conversation_id=conv_id)

        block = loop_calls[2]["personal_context"]
        assert "does not identify one person" in block
        assert "Robin Alpha" in block and "Robin Beta" in block
        routing, _ = stored_routing(store, frames)
        assert "person" not in routing and "person_id" not in routing

    async def test_a_place_word_never_reaches_routing(self, store, crm, loop_calls):
        add_person(crm, "Austin Alpha", 60.0)

        _, frames = await ask("tell me about Austin")
        routing, _ = stored_routing(store, frames)

        assert routing["person_tier"] == "likely"
        assert "person" not in routing and "person_id" not in routing
