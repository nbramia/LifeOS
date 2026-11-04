"""
Tests for GET /api/slack/thread and GET /api/slack/context.

The real SlackClient runs against an httpx.MockTransport that replays the
recorded (synthetic) Slack responses in tests/fixtures/slack_context/, so
pagination, filtering and rate-limit retries are exercised end to end with
no live Slack calls.
"""
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.slack_integration import SlackClient

FIXTURE = Path(__file__).parent / "fixtures" / "slack_context" / "recorded.json"
CHANNEL = "C0TESTCHAN"
THREAD_TS = "1767225600.000100"


class RecordedSlack:
    """Replays recorded Slack responses; optional per-method failures first."""

    def __init__(self, failures=None):
        self.recorded = json.loads(FIXTURE.read_text())
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content or b"{}")
        self.calls.append((method, params))
        queued = self.failures.get(method)
        if queued:
            return queued.pop(0)
        if method == "conversations.history" and "latest" in params:
            return httpx.Response(200, json=self._history(params))
        page = self.recorded[method][params.get("cursor", "")]
        return httpx.Response(200, json=page)

    def _history(self, params):
        """Serve the recorded history like Slack: newest first, honouring
        latest/inclusive/limit, with an offset cursor over the filtered list."""
        recorded = self.recorded["conversations.history"]
        msgs = [m for page in recorded.values() for m in page["messages"]]
        latest = float(params["latest"])
        inclusive = params.get("inclusive", False)
        msgs = [
            m for m in msgs
            if float(m["ts"]) < latest or (inclusive and float(m["ts"]) == latest)
        ]
        offset = int(params.get("cursor", "0"))
        limit = params.get("limit", 100)
        page = msgs[offset: offset + limit]
        more = offset + limit < len(msgs)
        return {
            "ok": True,
            "messages": page,
            "has_more": more,
            "response_metadata": {"next_cursor": str(offset + limit) if more else ""},
        }

    def client(self) -> SlackClient:
        c = SlackClient(token="xoxp-test-token")
        c._http_client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return c


@pytest.fixture
def api():
    return TestClient(app)


def _run(api, url, slack: RecordedSlack, **params):
    with patch("api.routes.slack.is_slack_enabled", return_value=True), \
         patch("api.routes.slack.get_slack_client", return_value=slack.client()), \
         patch("time.sleep") as sleep:
        response = api.get(url, params=params)
    return response, sleep


# =============================================================================
# /api/slack/thread
# =============================================================================

@pytest.mark.unit
class TestSlackThread:

    def test_returns_all_replies_in_order_across_pages(self, api):
        slack = RecordedSlack()
        response, _ = _run(api, "/api/slack/thread", slack, channel=CHANNEL, ts=THREAD_TS)

        assert response.status_code == 200
        data = response.json()
        ts_list = [m["ts"] for m in data["messages"]]
        assert ts_list == sorted(ts_list, key=float)
        assert ts_list[0] == THREAD_TS  # parent included
        assert data["total"] == 5
        assert data["truncated"] is False
        # Both pages were read: second request carried the cursor.
        cursors = [p.get("cursor") for m, p in slack.calls if m == "conversations.replies"]
        assert cursors == [None, "page2"]

    def test_includes_non_roster_people_and_bots(self, api):
        response, _ = _run(api, "/api/slack/thread", RecordedSlack(), channel=CHANNEL, ts=THREAD_TS)

        by_ts = {m["ts"]: m for m in response.json()["messages"]}
        assert by_ts["1767225660.000200"]["user"] == "U0OUTSIDER"
        bot = by_ts["1767225720.000300"]
        assert bot["user"] == "B0TESTBOT1"
        assert bot["bot_id"] == "B0TESTBOT1"
        assert bot["subtype"] == "bot_message"
        assert by_ts["1767225840.000500"]["subtype"] == "thread_broadcast"

    def test_message_shape(self, api):
        response, _ = _run(api, "/api/slack/thread", RecordedSlack(), channel=CHANNEL, ts=THREAD_TS)

        msg = response.json()["messages"][0]
        assert set(msg) == {"ts", "user", "bot_id", "text", "thread_ts", "subtype"}
        assert msg["thread_ts"] == THREAD_TS
        assert msg["text"].startswith("Synthetic question")

    def test_truncated_at_the_cap_keeps_the_oldest(self):
        slack = RecordedSlack()
        messages, truncated = slack.client().get_thread_messages(CHANNEL, THREAD_TS, max_messages=3)
        assert truncated is True
        assert [m["ts"] for m in messages] == [THREAD_TS, "1767225660.000200", "1767225720.000300"]

    def test_out_of_order_replies_come_back_oldest_first(self, api):
        slack = RecordedSlack()
        for page in slack.recorded["conversations.replies"].values():
            page["messages"].reverse()
        slack.recorded["conversations.replies"]["page2"], slack.recorded["conversations.replies"][""] = (
            slack.recorded["conversations.replies"][""], slack.recorded["conversations.replies"]["page2"])
        slack.recorded["conversations.replies"][""]["response_metadata"] = {"next_cursor": "page2"}
        slack.recorded["conversations.replies"]["page2"]["response_metadata"] = {"next_cursor": ""}
        response, _ = _run(api, "/api/slack/thread", slack, channel=CHANNEL, ts=THREAD_TS)
        ts_list = [m["ts"] for m in response.json()["messages"]]
        assert ts_list == sorted(ts_list, key=float) and len(ts_list) == 5

    def test_drops_channel_noise(self, api):
        response, _ = _run(api, "/api/slack/thread", RecordedSlack(), channel=CHANNEL, ts=THREAD_TS)

        assert all(m["subtype"] != "channel_join" for m in response.json()["messages"])


# =============================================================================
# /api/slack/context
# =============================================================================

@pytest.mark.unit
class TestSlackContext:

    def test_default_before_is_10_strictly_before_oldest_first(self, api):
        slack = RecordedSlack()
        ts = "1767225500.000900"  # equal to the newest recorded message
        response, _ = _run(api, "/api/slack/context", slack, channel=CHANNEL, ts=ts)

        assert response.status_code == 200
        data = response.json()
        assert data["before"] == 10
        ts_list = [m["ts"] for m in data["messages"]]
        assert ts not in ts_list  # strictly before
        assert all(float(t) < float(ts) for t in ts_list)
        assert ts_list == sorted(ts_list, key=float)
        # channel_join noise dropped; the bot message kept.
        assert data["total"] == 4
        params = [p for m, p in slack.calls if m == "conversations.history"][0]
        assert params["latest"] == ts
        assert params["inclusive"] is False

    def test_at_most_before_messages_keeps_the_latest(self, api):
        response, _ = _run(
            api, "/api/slack/context", RecordedSlack(),
            channel=CHANNEL, ts="1767225600.000000", before=2,
        )

        data = response.json()
        assert data["total"] == 2
        assert [m["ts"] for m in data["messages"]] == [
            "1767225300.000700", "1767225500.000900",
        ]

    def test_noise_does_not_count_toward_before(self, api):
        """before=4 with a join in the first page still yields 4 real messages."""
        response, _ = _run(
            api, "/api/slack/context", RecordedSlack(),
            channel=CHANNEL, ts="1767225600.000000", before=4,
        )

        msgs = response.json()["messages"]
        assert len(msgs) == 4
        assert all(m["subtype"] != "channel_join" for m in msgs)

    @pytest.mark.parametrize("before", [0, 51])
    def test_before_bounds(self, api, before):
        response, _ = _run(
            api, "/api/slack/context", RecordedSlack(),
            channel=CHANNEL, ts="1767225600.000000", before=before,
        )
        assert response.status_code == 400  # LifeOS maps validation errors to 400

    def test_max_before_50_accepted(self, api):
        response, _ = _run(
            api, "/api/slack/context", RecordedSlack(),
            channel=CHANNEL, ts="1767225600.000000", before=50,
        )
        assert response.status_code == 200
        assert response.json()["total"] == 5


# =============================================================================
# Shared: visibility, validation, rate limits
# =============================================================================

ENDPOINTS = [
    ("/api/slack/thread", "conversations.replies", {}),
    ("/api/slack/context", "conversations.history", {"before": 5}),
]


@pytest.mark.unit
class TestSlackThreadContextErrors:

    @pytest.mark.parametrize("url,method,extra", ENDPOINTS)
    @pytest.mark.parametrize("error", ["channel_not_found", "not_in_channel", "thread_not_found", "access_denied"])
    def test_not_visible_is_404_without_slack_text(self, api, url, method, extra, error):
        slack = RecordedSlack(failures={
            method: [httpx.Response(200, json={"ok": False, "error": error})],
        })
        response, _ = _run(api, url, slack, channel=CHANNEL, ts=THREAD_TS, **extra)

        assert response.status_code == 404
        body = response.text
        assert "Synthetic" not in body
        assert response.json() == {"detail": "Channel or thread not visible to this token"}

    @pytest.mark.parametrize("url,method,extra", ENDPOINTS)
    def test_retries_429_with_retry_after(self, api, url, method, extra):
        slack = RecordedSlack(failures={
            method: [httpx.Response(429, headers={"Retry-After": "3"}, text="")],
        })
        response, sleep = _run(api, url, slack, channel=CHANNEL, ts=THREAD_TS, **extra)

        assert response.status_code == 200
        assert response.json()["total"] > 0
        sleep.assert_any_call(3)

    @pytest.mark.parametrize("url,method,extra", ENDPOINTS)
    def test_rate_limit_exhausted_is_503(self, api, url, method, extra):
        limited = [httpx.Response(429, headers={"Retry-After": "1"}, text="")] * 10
        slack = RecordedSlack(failures={method: limited})
        response, _ = _run(api, url, slack, channel=CHANNEL, ts=THREAD_TS, **extra)

        assert response.status_code == 503

    @pytest.mark.parametrize("url,method,extra", ENDPOINTS)
    def test_missing_scope_is_403(self, api, url, method, extra):
        slack = RecordedSlack(failures={
            method: [httpx.Response(200, json={"ok": False, "error": "missing_scope"})],
        })
        response, _ = _run(api, url, slack, channel=CHANNEL, ts=THREAD_TS, **extra)
        assert response.status_code == 403

    @pytest.mark.parametrize("url,_method,extra", ENDPOINTS)
    @pytest.mark.parametrize("channel,ts", [
        ("general", THREAD_TS),
        ("C0TEST CHAN", THREAD_TS),
        (CHANNEL, "yesterday"),
        (CHANNEL, "1767225600"),
    ])
    def test_malformed_params_rejected_before_slack(self, api, url, _method, extra, channel, ts):
        slack = RecordedSlack()
        response, _ = _run(api, url, slack, channel=channel, ts=ts, **extra)

        assert response.status_code == 400
        assert slack.calls == []

    @pytest.mark.parametrize("url,method,extra", ENDPOINTS)
    def test_other_slack_errors_are_502_with_the_code_only(self, api, url, method, extra):
        slack = RecordedSlack(failures={
            method: [httpx.Response(200, json={"ok": False, "error": "fatal_error"})],
        })
        response, _ = _run(api, url, slack, channel=CHANNEL, ts=THREAD_TS, **extra)
        assert response.status_code == 502
        assert response.json() == {"detail": "Slack API error: fatal_error"}

    @pytest.mark.parametrize("url,method,extra", ENDPOINTS)
    def test_a_non_json_slack_body_is_502(self, api, url, method, extra):
        slack = RecordedSlack(failures={method: [httpx.Response(500, text="<html>Synthetic outage</html>")]})
        response, _ = _run(api, url, slack, channel=CHANNEL, ts=THREAD_TS, **extra)
        assert response.status_code == 502 and "Synthetic" not in response.text

    @pytest.mark.parametrize("url,_method,extra", ENDPOINTS)
    @pytest.mark.parametrize("channel", ["D0TESTDM01", "G0TESTGRP1"])
    def test_dm_and_private_ids_are_accepted(self, api, url, _method, extra, channel):
        response, _ = _run(api, url, RecordedSlack(), channel=channel, ts=THREAD_TS, **extra)
        assert response.status_code == 200

    @pytest.mark.parametrize("url,_method,extra", ENDPOINTS)
    def test_disabled_is_503(self, api, url, _method, extra):
        with patch("api.routes.slack.is_slack_enabled", return_value=False):
            response = api.get(url, params={"channel": CHANNEL, "ts": THREAD_TS, **extra})
        assert response.status_code == 503
