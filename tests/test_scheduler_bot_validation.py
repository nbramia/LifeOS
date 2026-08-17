"""
Tests for schedule notification-bot validation (issue #575).

A schedule's ``bot`` field names which Telegram bot delivers its notification.
Before #575 nothing validated it, so a typo — or a name orphaned by a bot
rename — was accepted and then silently degraded to the primary bot and the
primary chat at every fire, putting domain-specific content in the general feed
with no signal anywhere but a log line.

Three surfaces are covered here:
- the registry-backed validation helpers in ``api.services.telegram``
- the scheduler HTTP routes (create + update) and the MCP tool handlers
- the fire path, which stays fail-open but now marks the misroute in the
  message it delivers

Every registry in this file is synthetic and patched in; nothing asserts against
the real bot names, tokens, or chat ids.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _synthetic_bots():
    """A stand-in registry — deliberately unlike the real one."""
    from config.settings import TelegramBotConfig
    return [
        TelegramBotConfig(name="alerts", token="ALERTS-TOK", chat_id="ALERTS-CHAT"),
        TelegramBotConfig(name="ledger", token="LEDGER-TOK", chat_id="LEDGER-CHAT"),
    ]


def _patch_registry(bots):
    """Patch the bot registry the validation path reads at call time."""
    mock_settings = MagicMock()
    mock_settings.telegram_bots = bots
    mock_settings.telegram_bot_token = "PRIMARY-TOK"
    mock_settings.telegram_chat_id = "PRIMARY-CHAT"
    return patch("api.services.telegram.settings", mock_settings)


# ---------------------------------------------------------------------------
# Registry-backed helpers
# ---------------------------------------------------------------------------

class TestValidBotNames:
    def test_lists_primary_plus_registry(self):
        from api.services.telegram import valid_bot_names
        with _patch_registry(_synthetic_bots()):
            assert valid_bot_names() == ["primary", "alerts", "ledger"]

    def test_empty_registry_still_offers_primary(self):
        from api.services.telegram import valid_bot_names
        with _patch_registry([]):
            assert valid_bot_names() == ["primary"]

    def test_read_at_call_time_not_captured(self):
        # A rename between calls must be visible immediately — nothing may
        # snapshot the registry at import time.
        from api.services.telegram import valid_bot_names
        with _patch_registry(_synthetic_bots()):
            assert "alerts" in valid_bot_names()
        with _patch_registry([]):
            assert "alerts" not in valid_bot_names()


class TestValidateBotName:
    @pytest.mark.parametrize("bot", [None, "", "primary", "alerts", "ledger"])
    def test_accepts_empty_primary_and_registered(self, bot):
        from api.services.telegram import validate_bot_name
        with _patch_registry(_synthetic_bots()):
            validate_bot_name(bot)  # does not raise

    def test_rejects_unknown_name_and_lists_options(self):
        from api.services.telegram import validate_bot_name
        with _patch_registry(_synthetic_bots()):
            with pytest.raises(ValueError) as exc:
                validate_bot_name("orphaned")
        message = str(exc.value)
        assert "orphaned" in message
        assert "primary" in message and "alerts" in message and "ledger" in message

    def test_empty_registry_accepts_unset_but_rejects_a_name(self):
        # A fresh clone with only a primary bot is a valid state, not a
        # rejection — but a name it doesn't have is still wrong.
        from api.services.telegram import validate_bot_name
        with _patch_registry([]):
            validate_bot_name("")
            validate_bot_name("primary")
            with pytest.raises(ValueError):
                validate_bot_name("alerts")

    @pytest.mark.parametrize("bot", [" ledger", "ledger ", "  ledger  "])
    def test_trims_surrounding_whitespace_and_returns_the_stored_name(self, bot):
        # Rejecting ' ledger' against a list showing a visually identical
        # 'ledger' is unhelpful for an LLM-authored tool argument.
        from api.services.telegram import validate_bot_name
        with _patch_registry(_synthetic_bots()):
            assert validate_bot_name(bot) == "ledger"

    def test_does_not_case_fold(self):
        # Exact matching is deliberate — parity with _resolve_bot.
        from api.services.telegram import validate_bot_name
        with _patch_registry(_synthetic_bots()):
            with pytest.raises(ValueError):
                validate_bot_name("Ledger")

    def test_is_known_bot_predicate(self):
        from api.services.telegram import is_known_bot
        with _patch_registry(_synthetic_bots()):
            assert is_known_bot("") is True
            assert is_known_bot(None) is True
            assert is_known_bot("primary") is True
            assert is_known_bot("alerts") is True
            assert is_known_bot("orphaned") is False


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

class TestSchedulerRoutesBotValidation:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api.main import app
        return TestClient(app)

    @pytest.fixture
    def mock_store(self):
        from api.services.scheduler_store import ScheduleEntry
        with patch("api.routes.scheduler.get_scheduler_store") as mock:
            store = mock.return_value
            entry = ScheduleEntry(
                id="sch-1", name="Balance check", schedule_type="cron",
                schedule_value="0 7 * * 1-5", action="notify",
                message_type="static", message_content="check the balance",
                bot="ledger",
            )
            store.create.return_value = entry
            store.update.return_value = entry
            store.get.return_value = entry
            yield store

    def _create_payload(self, **overrides):
        payload = {
            "name": "Balance check", "schedule_type": "cron",
            "schedule_value": "0 7 * * 1-5", "action": "notify",
            "message_content": "check the balance",
        }
        payload.update(overrides)
        return payload

    def test_create_rejects_unknown_bot_with_valid_options(self, client, mock_store):
        with _patch_registry(_synthetic_bots()):
            resp = client.post("/api/scheduler", json=self._create_payload(bot="orphaned"))
        assert resp.status_code == 422
        detail = resp.text
        assert "orphaned" in detail
        assert "alerts" in detail and "ledger" in detail and "primary" in detail
        mock_store.create.assert_not_called()

    def test_update_rejects_unknown_bot_and_leaves_schedule_untouched(self, client, mock_store):
        with _patch_registry(_synthetic_bots()):
            resp = client.put("/api/scheduler/sch-1", json={"bot": "orphaned"})
        assert resp.status_code == 422
        assert "alerts" in resp.text
        mock_store.update.assert_not_called()

    def test_create_without_bot_succeeds(self, client, mock_store):
        with _patch_registry(_synthetic_bots()):
            resp = client.post("/api/scheduler", json=self._create_payload())
        assert resp.status_code == 200
        assert mock_store.create.call_args.kwargs["bot"] == ""

    def test_create_with_empty_bot_succeeds(self, client, mock_store):
        with _patch_registry(_synthetic_bots()):
            resp = client.post("/api/scheduler", json=self._create_payload(bot=""))
        assert resp.status_code == 200
        assert mock_store.create.call_args.kwargs["bot"] == ""

    def test_create_with_primary_succeeds(self, client, mock_store):
        with _patch_registry(_synthetic_bots()):
            resp = client.post("/api/scheduler", json=self._create_payload(bot="primary"))
        assert resp.status_code == 200
        assert mock_store.create.call_args.kwargs["bot"] == "primary"

    def test_create_with_registered_bot_persists_the_name(self, client, mock_store):
        with _patch_registry(_synthetic_bots()):
            resp = client.post("/api/scheduler", json=self._create_payload(bot="ledger"))
        assert resp.status_code == 200
        assert mock_store.create.call_args.kwargs["bot"] == "ledger"
        assert resp.json()["bot"] == "ledger"

    def test_update_with_registered_bot_succeeds(self, client, mock_store):
        with _patch_registry(_synthetic_bots()):
            resp = client.put("/api/scheduler/sch-1", json={"bot": "alerts"})
        assert resp.status_code == 200
        assert mock_store.update.call_args.kwargs["bot"] == "alerts"

    def test_create_trims_whitespace_before_storing(self, client, mock_store):
        # Stored untrimmed, the name would pass validation and then misroute at
        # fire time — so the write path has to store the trimmed value.
        with _patch_registry(_synthetic_bots()):
            resp = client.post("/api/scheduler", json=self._create_payload(bot=" ledger "))
        assert resp.status_code == 200
        assert mock_store.create.call_args.kwargs["bot"] == "ledger"

    def test_update_trims_whitespace_before_storing(self, client, mock_store):
        with _patch_registry(_synthetic_bots()):
            resp = client.put("/api/scheduler/sch-1", json={"bot": " alerts "})
        assert resp.status_code == 200
        assert mock_store.update.call_args.kwargs["bot"] == "alerts"

    def test_empty_registry_accepts_a_schedule_without_a_bot(self, client, mock_store):
        # Fresh clone: only a primary bot configured.
        with _patch_registry([]):
            resp = client.post("/api/scheduler", json=self._create_payload())
        assert resp.status_code == 200
        mock_store.create.assert_called_once()

    def test_empty_registry_rejects_a_named_bot(self, client, mock_store):
        with _patch_registry([]):
            resp = client.post("/api/scheduler", json=self._create_payload(bot="alerts"))
        assert resp.status_code == 422
        mock_store.create.assert_not_called()


# ---------------------------------------------------------------------------
# MCP tool handlers
# ---------------------------------------------------------------------------

class TestMcpScheduleBotValidation:
    """The API is the only validator; MCP just surfaces its rejection.

    The MCP server deliberately does *not* pre-check the bot name. Its view of
    the registry is not the API's: ``config/telegram_bots.json`` is read
    relative to the process cwd (the stdio child inherits the agent's cwd, not
    the repo), and a client pointed at a remote ``LIFEOS_API_URL`` has neither
    the same file nor the token env vars. A local pre-check would reject names
    the server accepts, so the 422 is allowed to propagate instead.
    """

    @pytest.fixture
    def server(self):
        from mcp_server import LifeOSMCPServer
        srv = LifeOSMCPServer.__new__(LifeOSMCPServer)
        srv.client = MagicMock()
        srv._result_cache = None
        return srv

    def _http_422(self, detail):
        """Make the transport raise the way httpx does on a 422."""
        import httpx
        response = MagicMock()
        response.status_code = 422
        response.text = f'{{"detail":"{detail}"}}'
        return httpx.HTTPStatusError("422", request=MagicMock(), response=response)

    @pytest.mark.parametrize(
        "tool_name,arguments",
        [
            ("lifeos_schedule_create", {
                "name": "Balance check", "schedule_type": "cron",
                "schedule_value": "0 7 * * 1-5", "action": "notify",
                "bot": "orphaned",
            }),
            ("lifeos_schedule_update", {"schedule_id": "sch-1", "bot": "orphaned"}),
        ],
    )
    def test_api_rejection_surfaces_as_the_tool_error(self, server, tool_name, arguments):
        detail = ("Unknown Telegram bot 'orphaned'. Configured names: "
                  "primary, alerts, ledger")
        error = self._http_422(detail)
        server.client.post.side_effect = error
        server.client.request.side_effect = error

        result = server._call_api(tool_name, arguments)

        assert "error" in result
        assert "422" in result["error"]
        # The valid options reach the agent verbatim from the API.
        assert "orphaned" in result["error"]
        assert "alerts" in result["error"] and "primary" in result["error"]
        # And the agent sees it as an error, not a successful write.
        assert server._format_response(tool_name, result).startswith("Error:")

    def test_no_local_precheck_blocks_a_name_the_api_would_accept(self, server):
        # The regression guarded here: an MCP client whose local registry is
        # empty (wrong cwd, or a remote API) must still be able to write a bot
        # name the server knows. A pre-flight check would have hard-rejected it.
        response = MagicMock()
        response.json.return_value = {"id": "sch-2", "name": "Balance check", "bot": "ledger"}
        server.client.post.return_value = response

        with _patch_registry([]):
            result = server._call_api("lifeos_schedule_create", {
                "name": "Balance check", "schedule_type": "cron",
                "schedule_value": "0 7 * * 1-5", "action": "notify", "bot": "ledger",
            })

        assert "error" not in result
        assert result["bot"] == "ledger"
        assert server.client.post.call_args.kwargs["json"]["bot"] == "ledger"

    def test_omitted_bot_passes_through(self, server):
        response = MagicMock()
        response.json.return_value = {"id": "sch-3", "name": "Ping"}
        server.client.post.return_value = response

        with _patch_registry([]):
            result = server._call_api("lifeos_schedule_create", {
                "name": "Ping", "schedule_type": "cron",
                "schedule_value": "0 9 * * *", "action": "notify",
            })
        assert "error" not in result
        server.client.post.assert_called_once()


# ---------------------------------------------------------------------------
# Fire path — fail-open delivery, but marked
# ---------------------------------------------------------------------------

class TestFireTimeMisrouteMarker:
    @pytest.fixture
    def scheduler(self, tmp_path):
        from api.services.scheduler_store import SchedulerStore, SchedulerScheduler
        store = SchedulerStore(
            vault_path=tmp_path / "vault",
            index_path=tmp_path / "scheduler_index.json",
        )
        return SchedulerScheduler(store)

    def _entry(self, scheduler, bot):
        return scheduler.store.create(
            name="Balance check", schedule_type="cron", schedule_value="0 7 * * 1-5",
            action="notify", message_type="static",
            message_content="the balance is fine", bot=bot,
        )

    @pytest.mark.asyncio
    async def test_orphaned_bot_still_delivers_with_a_marker(self, scheduler):
        entry = self._entry(scheduler, "orphaned")
        with _patch_registry(_synthetic_bots()):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_entry(entry)

        mock_send.assert_called_once()
        sent = mock_send.call_args[0][0]
        # The notification is never dropped on account of the bad name.
        assert "the balance is fine" in sent
        # And the misroute is visible in the channel it lands in.
        assert "orphaned" in sent
        assert "not configured" in sent
        assert scheduler.store.get(entry.id).last_status == "sent"

    @pytest.mark.asyncio
    async def test_marker_leaks_no_chat_id_or_token(self, scheduler):
        entry = self._entry(scheduler, "orphaned")
        with _patch_registry(_synthetic_bots()):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_entry(entry)

        sent = mock_send.call_args[0][0]
        for secret in ("PRIMARY-TOK", "PRIMARY-CHAT", "ALERTS-TOK", "ALERTS-CHAT"):
            assert secret not in sent

    @pytest.mark.asyncio
    async def test_registered_bot_delivers_unmarked(self, scheduler):
        entry = self._entry(scheduler, "ledger")
        with _patch_registry(_synthetic_bots()):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_entry(entry)

        sent = mock_send.call_args[0][0]
        assert sent == "*Balance check*\n\nthe balance is fine"
        assert mock_send.call_args.kwargs["bot"] == "ledger"

    @pytest.mark.asyncio
    async def test_unset_bot_delivers_unmarked_on_an_empty_registry(self, scheduler):
        entry = self._entry(scheduler, "")
        with _patch_registry([]):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_entry(entry)

        sent = mock_send.call_args[0][0]
        assert sent == "*Balance check*\n\nthe balance is fine"

    @pytest.mark.asyncio
    async def test_send_survives_a_registry_read_that_raises(self, scheduler):
        # The banner exists to annotate the send; it must never be the thing
        # that loses it. A malformed registry makes the lookup raise, and the
        # notification still has to go out.
        entry = self._entry(scheduler, "orphaned")
        with patch("api.services.telegram.is_known_bot",
                   side_effect=RuntimeError("malformed registry")):
            with patch("api.services.telegram.send_message_async",
                       new_callable=AsyncMock, return_value=True) as mock_send:
                await scheduler._fire_entry(entry)

        mock_send.assert_called_once()
        sent = mock_send.call_args[0][0]
        assert "the balance is fine" in sent
        # No banner is better than no message: it degrades, it doesn't abort.
        assert "not configured" not in sent
        assert scheduler.store.get(entry.id).last_status == "sent"

    @pytest.mark.asyncio
    async def test_failure_notification_also_carries_the_marker(self, scheduler):
        entry = scheduler.store.create(
            name="Balance check", schedule_type="cron", schedule_value="0 7 * * 1-5",
            action="endpoint", message_type="endpoint",
            endpoint_config={"endpoint": "/api/nope"}, bot="orphaned",
        )
        with _patch_registry(_synthetic_bots()):
            with patch.object(scheduler, "_generate_message",
                             new_callable=AsyncMock, side_effect=RuntimeError("boom")):
                with patch("api.services.telegram.send_message_async",
                           new_callable=AsyncMock, return_value=True) as mock_send:
                    await scheduler._fire_entry(entry)

        mock_send.assert_called_once()
        sent = mock_send.call_args[0][0]
        assert "orphaned" in sent and "not configured" in sent
        assert "boom" in sent
        assert scheduler.store.get(entry.id).last_status == "failed"
