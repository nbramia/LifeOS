"""Tests for the on-demand originals/preview routes and api/services/photos_origin.py.

A fake ``ssh`` executable first on PATH stands in for the Photos Mac: it
records its argument list and replays a per-subcommand scenario (stdout, exit
code, or an endless stream that only a kill stops).
"""
import asyncio
import base64
import json
import os
import shlex
import stat
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from tests._photos_fakes import UUID_A, UUID_B, UUID_UNKNOWN

pytestmark = pytest.mark.unit

FAKE_SSH = '''#!{python}
import base64, json, os, shlex, sys, time

argv = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
remote = shlex.split(argv[-1])
subcommand = remote[2]
scenario = json.load(open(os.environ["FAKE_SSH_SCENARIO"])).get(subcommand, {{}})
with open(os.environ["FAKE_SSH_PIDFILE"], "a") as pids:
    pids.write(str(os.getpid()) + "\\n")
if scenario.get("hang"):
    while True:
        sys.stdout.buffer.write(b"x" * 1024)
        sys.stdout.buffer.flush()
        time.sleep(0.02)
if scenario.get("silent_hang"):
    time.sleep(600)
data = base64.b64decode(scenario.get("stdout_b64", ""))
sys.stdout.buffer.write(data)
sys.stdout.buffer.flush()
sys.exit(scenario.get("exit", 0))
'''

INFO_AVAILABLE = {
    "available": True, "filename": "IMG_0001.HEIC", "size_bytes": 11,
    "kind": "photo", "previewable": True,
}


class Origin:
    """Handle on the fake ssh: configure scenarios, inspect calls."""

    def __init__(self, tmp_path):
        self.log = tmp_path / "ssh.log"
        self.pidfile = tmp_path / "ssh.pids"
        self.scenario_file = tmp_path / "scenario.json"
        self.scenario_file.write_text("{}")

    def scenario(self, **by_subcommand):
        self.scenario_file.write_text(json.dumps(by_subcommand))

    @staticmethod
    def output(data: bytes, exit_code: int = 0) -> dict:
        return {"stdout_b64": base64.b64encode(data).decode(), "exit": exit_code}

    def info(self, record: dict) -> dict:
        return self.output(json.dumps(record).encode() + b"\n")

    @property
    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    @property
    def pids(self) -> list:
        if not self.pidfile.exists():
            return []
        return [int(p) for p in self.pidfile.read_text().split()]


def _gone(pid: int) -> bool:
    """True when the process neither runs nor lingers as an unreaped zombie."""
    try:
        open(f"/proc/{pid}/stat").read()
    except FileNotFoundError:
        return True
    return False


def process_dead(pid: int, timeout: float = 5.0) -> bool:
    """Poll (blocking) until the process is not running. A zombie counts:
    TestClient closes its event loop right after the response, so nothing is
    left to reap the child; strict reaping is asserted in the async tests."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


async def process_gone_async(pid: int, timeout: float = 5.0) -> bool:
    """Same, without blocking the event loop that reaps the child."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _gone(pid):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
def origin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH.format(python=sys.executable))
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    handle = Origin(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(handle.log))
    monkeypatch.setenv("FAKE_SSH_PIDFILE", str(handle.pidfile))
    monkeypatch.setenv("FAKE_SSH_SCENARIO", str(handle.scenario_file))
    return handle


@pytest.fixture
def configured(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "photos_origin_ssh_host", "mac-host")
    monkeypatch.setattr(settings, "photos_origin_repo_path", "~/Code/LifeOS")


@pytest.fixture(autouse=True)
def fresh_semaphore():
    """Each test gets its own concurrency semaphore (and event loop)."""
    from api.services import photos_origin
    photos_origin._semaphore = None
    yield
    photos_origin._semaphore = None


@pytest.fixture
def store(tmp_path):
    from api.services.interaction_store import Interaction, InteractionStore
    real = InteractionStore(str(tmp_path / "interactions.db"), strict=False)
    real.add(Interaction(
        id="i-1", person_id="person-1", timestamp=datetime(2024, 5, 1, tzinfo=timezone.utc),
        source_type="photos", title="Photo", source_link=f"photos://asset/{UUID_A}",
        source_id=f"{UUID_A}:7",
    ))
    with patch("api.services.interaction_store.get_interaction_store", return_value=real):
        yield real


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


class TestGuardsRunNoSsh:
    def test_unknown_uuid_is_404_without_ssh(self, client, origin, configured, store):
        for path in (f"/api/photos/original/{UUID_UNKNOWN}/info",
                     f"/api/photos/original/{UUID_UNKNOWN}",
                     f"/api/photos/preview/{UUID_UNKNOWN}"):
            assert client.get(path).status_code == 404
        assert origin.calls == []

    def test_a_non_photos_interaction_does_not_count(self, client, origin, configured, store):
        from api.services.interaction_store import Interaction
        store.add(Interaction(
            id="i-2", person_id="person-1", timestamp=datetime(2024, 5, 1, tzinfo=timezone.utc),
            source_type="imessage", title="Hi", source_id=f"{UUID_B}:7",
        ))
        assert client.get(f"/api/photos/preview/{UUID_B}").status_code == 404
        assert origin.calls == []

    def test_a_uuid_prefix_of_a_known_asset_does_not_match(self, client, origin, configured, store):
        # Same first 35 characters as a known asset, different last character.
        near_miss = UUID_A[:-1] + "2"
        assert client.get(f"/api/photos/preview/{near_miss}").status_code == 404
        assert origin.calls == []

    def test_unconfigured_is_503_without_ssh(self, client, origin, store):
        from config.settings import settings
        assert settings.photos_origin_ssh_host == ""
        for path in (f"/api/photos/original/{UUID_A}/info",
                     f"/api/photos/original/{UUID_A}",
                     f"/api/photos/preview/{UUID_A}"):
            response = client.get(path)
            assert response.status_code == 503
            assert response.headers["x-photos-origin"] == "unconfigured"
        assert origin.calls == []

    def test_unconfigured_is_decided_before_the_database_is_consulted(
        self, client, origin, store
    ):
        with patch.object(type(store), "has_photo_asset", side_effect=AssertionError("db consulted")):
            response = client.get(f"/api/photos/preview/{UUID_UNKNOWN}")
        assert response.status_code == 503
        assert origin.calls == []

    def test_building_a_command_requires_a_host_and_a_valid_uuid(self, origin):
        from api.services import photos_origin
        with pytest.raises(photos_origin.OriginError) as unconfigured:
            photos_origin.build_ssh_command("info", UUID_A)
        assert unconfigured.value.kind == "unconfigured"

    def test_building_a_command_rejects_a_malformed_uuid(self, origin, configured):
        from api.services import photos_origin
        with pytest.raises(photos_origin.OriginError) as err:
            photos_origin.build_ssh_command("info", "$(touch pwned)")
        assert err.value.kind == "error"

    @pytest.mark.parametrize("host", ["-oProxyCommand=touch /tmp/x", "-F/etc/passwd", "a b"])
    def test_unusable_host_is_rejected_without_ssh(
        self, client, origin, store, monkeypatch, host
    ):
        from config.settings import settings
        monkeypatch.setattr(settings, "photos_origin_ssh_host", host)
        response = client.get(f"/api/photos/preview/{UUID_A}")
        assert response.status_code == 503
        assert response.headers["x-photos-origin"] == "unconfigured"
        assert origin.calls == []

    @pytest.mark.parametrize("bad", ["not-a-uuid", "A" * 35, "A" * 35 + ";", "A" * 35 + "`"])
    def test_malformed_uuid_is_400_without_ssh(self, client, origin, configured, store, bad):
        for path in (f"/api/photos/original/{bad}/info", f"/api/photos/original/{bad}",
                     f"/api/photos/preview/{bad}"):
            assert client.get(path).status_code == 400
        assert origin.calls == []


class TestCommandConstruction:
    def test_argument_list_with_host_from_settings(self, client, origin, configured, store):
        origin.scenario(preview=origin.output(b"jpeg"))
        client.get(f"/api/photos/preview/{UUID_A}")
        (argv,) = origin.calls
        assert argv[:5] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--"]
        assert argv[5] == "mac-host"
        assert len(argv) == 7
        assert shlex.split(argv[6]) == [
            "/usr/bin/python3", "$HOME/Code/LifeOS/scripts/photos_origin_helper.py",
            "preview", UUID_A,
        ]

    def test_home_is_expanded_by_the_remote_shell(self, origin, configured):
        from api.services import photos_origin
        remote = photos_origin.build_ssh_command("info", UUID_A)[-1]
        assert '"$HOME"' in remote and "'~" not in remote

    def test_repo_path_is_shell_quoted(self, origin, configured, monkeypatch):
        from api.services import photos_origin
        from config.settings import settings
        monkeypatch.setattr(settings, "photos_origin_repo_path", "/opt/my repo; touch pwned")
        remote = photos_origin.build_ssh_command("info", UUID_A)[-1]
        assert shlex.split(remote) == [
            "/usr/bin/python3", "/opt/my repo; touch pwned/scripts/photos_origin_helper.py",
            "info", UUID_A,
        ]

    def test_host_follows_the_option_terminator(self, origin, configured, monkeypatch):
        from api.services import photos_origin
        from config.settings import settings
        monkeypatch.setattr(settings, "photos_origin_ssh_host", "someone@mac.example")
        cmd = photos_origin.build_ssh_command("info", UUID_A)
        assert cmd[cmd.index("--") + 1] == "someone@mac.example"
        assert cmd[0] == "ssh"

    def test_subprocess_is_started_without_a_shell(self, origin, configured):
        from api.services import photos_origin
        origin.scenario(info=origin.info(INFO_AVAILABLE))

        async def run():
            with patch("asyncio.create_subprocess_exec", wraps=asyncio.create_subprocess_exec) as spawn, \
                 patch("asyncio.create_subprocess_shell") as shell:
                await photos_origin.fetch_info(UUID_A)
            return spawn, shell

        spawn, shell = asyncio.run(run())
        shell.assert_not_called()
        assert spawn.call_args.args[0] == "ssh"


class TestInfoRoute:
    def test_returns_the_helper_record(self, client, origin, configured, store):
        origin.scenario(info=origin.info(INFO_AVAILABLE))
        response = client.get(f"/api/photos/original/{UUID_A}/info")
        assert response.status_code == 200
        assert response.json() == INFO_AVAILABLE

    def test_unavailable_original_is_reported_not_an_error(self, client, origin, configured, store):
        record = dict(INFO_AVAILABLE, available=False, size_bytes=None)
        origin.scenario(info=origin.info(record))
        response = client.get(f"/api/photos/original/{UUID_A}/info")
        assert response.status_code == 200
        assert response.json()["available"] is False

    def test_asset_unknown_to_the_mac_is_404(self, client, origin, configured, store):
        origin.scenario(info=origin.output(b"", 3))
        assert client.get(f"/api/photos/original/{UUID_A}/info").status_code == 404

    def test_unreachable_mac_is_503_unreachable(self, client, origin, configured, store):
        origin.scenario(info=origin.output(b"", 255))
        response = client.get(f"/api/photos/original/{UUID_A}/info")
        assert response.status_code == 503
        assert response.headers["x-photos-origin"] == "unreachable"
        assert "unreachable" in response.json()["detail"].lower()

    def test_helper_crash_is_502(self, client, origin, configured, store):
        origin.scenario(info=origin.output(b"", 1))
        assert client.get(f"/api/photos/original/{UUID_A}/info").status_code == 502

    def test_garbled_output_is_502(self, client, origin, configured, store):
        origin.scenario(info=origin.output(b"not json"))
        assert client.get(f"/api/photos/original/{UUID_A}/info").status_code == 502

    def test_a_stalled_mac_times_out_and_is_killed(self, client, origin, configured, store):
        from api.services import photos_origin
        origin.scenario(info={"silent_hang": True})
        with patch.object(photos_origin, "INFO_TIMEOUT_SECONDS", 0.5):
            response = client.get(f"/api/photos/original/{UUID_A}/info")
        assert response.status_code == 503
        assert response.headers["x-photos-origin"] == "unreachable"
        assert origin.pids and all(process_dead(pid) for pid in origin.pids)


class TestOriginalRoute:
    def test_streams_the_file_as_an_attachment(self, client, origin, configured, store):
        body = b"synthetic-heic"
        origin.scenario(
            info=origin.info(dict(INFO_AVAILABLE, size_bytes=len(body))),
            original=origin.output(body),
        )
        response = client.get(f"/api/photos/original/{UUID_A}")
        assert response.status_code == 200
        assert response.content == body
        assert response.headers["content-type"] == "image/heic"
        assert response.headers["content-length"] == str(len(body))
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert 'filename="IMG_0001.HEIC"' in disposition

    def test_large_body_is_relayed_intact(self, client, origin, configured, store):
        body = os.urandom(300 * 1024)
        origin.scenario(
            info=origin.info(dict(INFO_AVAILABLE, size_bytes=len(body))),
            original=origin.output(body),
        )
        assert client.get(f"/api/photos/original/{UUID_A}").content == body

    @pytest.mark.parametrize("name, safe_part", [
        ('../../etc/passwd', 'filename="passwd"'),
        ('evil"\r\nX-Injected: 1.jpg', 'filename="evilX-Injected_ 1.jpg"'),
        ('C:\\Users\\x\\photo.png', 'filename="photo.png"'),
        ('café ☕.jpg', "filename*=UTF-8''caf%C3%A9%20%E2%98%95.jpg"),
        ('', f'filename="{UUID_A}"'),
    ])
    def test_filename_is_sanitized(self, client, origin, configured, store, name, safe_part):
        origin.scenario(
            info=origin.info(dict(INFO_AVAILABLE, filename=name, size_bytes=1)),
            original=origin.output(b"x"),
        )
        response = client.get(f"/api/photos/original/{UUID_A}")
        disposition = response.headers["content-disposition"]
        assert safe_part in disposition
        assert "\r" not in disposition and "\n" not in disposition
        assert "/" not in disposition.split("filename*")[0]
        assert "X-Injected" not in response.headers

    @pytest.mark.parametrize("name, content_type", [
        ("a.heic", "image/heic"), ("a.JPG", "image/jpeg"), ("a.png", "image/png"),
        ("a.mov", "video/quicktime"), ("a.mp4", "video/mp4"), ("a.unknownext", "application/octet-stream"),
    ])
    def test_content_type_follows_the_extension(
        self, client, origin, configured, store, name, content_type
    ):
        origin.scenario(
            info=origin.info(dict(INFO_AVAILABLE, filename=name, size_bytes=1)),
            original=origin.output(b"x"),
        )
        assert client.get(f"/api/photos/original/{UUID_A}").headers["content-type"] == content_type

    def test_original_missing_on_the_mac_is_404_and_never_streams(
        self, client, origin, configured, store
    ):
        origin.scenario(info=origin.info(dict(INFO_AVAILABLE, available=False, size_bytes=None)))
        assert client.get(f"/api/photos/original/{UUID_A}").status_code == 404
        subcommands = [shlex.split(call[-1])[2] for call in origin.calls]
        assert subcommands == ["info"]

    def test_original_vanishing_between_info_and_download_is_404(
        self, client, origin, configured, store
    ):
        origin.scenario(info=origin.info(INFO_AVAILABLE), original=origin.output(b"", 4))
        assert client.get(f"/api/photos/original/{UUID_A}").status_code == 404

    def test_unreachable_is_503(self, client, origin, configured, store):
        origin.scenario(info=origin.output(b"", 255))
        response = client.get(f"/api/photos/original/{UUID_A}")
        assert response.status_code == 503
        assert response.headers["x-photos-origin"] == "unreachable"


class TestPreviewRoute:
    def test_streams_jpeg(self, client, origin, configured, store):
        origin.scenario(preview=origin.output(b"\xff\xd8preview\xff\xd9"))
        response = client.get(f"/api/photos/preview/{UUID_A}")
        assert response.status_code == 200
        assert response.content == b"\xff\xd8preview\xff\xd9"
        assert response.headers["content-type"] == "image/jpeg"

    def test_nothing_servable_is_404(self, client, origin, configured, store):
        origin.scenario(preview=origin.output(b"", 4))
        assert client.get(f"/api/photos/preview/{UUID_A}").status_code == 404

    def test_not_in_the_mac_library_is_404(self, client, origin, configured, store):
        origin.scenario(preview=origin.output(b"", 3))
        assert client.get(f"/api/photos/preview/{UUID_A}").status_code == 404

    def test_unreachable_before_any_byte_is_503(self, client, origin, configured, store):
        origin.scenario(preview=origin.output(b"", 255))
        response = client.get(f"/api/photos/preview/{UUID_A}")
        assert response.status_code == 503
        assert response.headers["x-photos-origin"] == "unreachable"

    def test_helper_error_before_any_byte_is_502(self, client, origin, configured, store):
        origin.scenario(preview=origin.output(b"", 1))
        assert client.get(f"/api/photos/preview/{UUID_A}").status_code == 502

    def test_stalled_start_is_503_and_killed(self, client, origin, configured, store):
        from api.services import photos_origin
        origin.scenario(preview={"silent_hang": True})
        with patch.object(photos_origin, "FIRST_BYTE_TIMEOUT_SECONDS", 0.5):
            response = client.get(f"/api/photos/preview/{UUID_A}")
        assert response.status_code == 503
        assert origin.pids and all(process_dead(pid) for pid in origin.pids)

    def test_empty_but_successful_output_is_an_empty_200(self, client, origin, configured, store):
        origin.scenario(preview=origin.output(b"", 0))
        response = client.get(f"/api/photos/preview/{UUID_A}")
        assert response.status_code == 200 and response.content == b""


class TestProcessLifecycle:
    async def test_early_close_kills_the_subprocess(self, origin, configured):
        from api.services import photos_origin
        origin.scenario(preview={"hang": True})

        session, first = await photos_origin.open_stream("preview", UUID_A)
        stream = session.stream(first)
        assert await stream.__anext__()
        (pid,) = origin.pids
        await stream.aclose()

        assert await process_gone_async(pid)

    async def test_cancelled_consumer_kills_the_subprocess(self, origin, configured):
        from api.services import photos_origin
        origin.scenario(preview={"hang": True})
        session, first = await photos_origin.open_stream("preview", UUID_A)
        (pid,) = origin.pids

        async def consume():
            async for _ in session.stream(first):
                pass

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await process_gone_async(pid)

    async def test_failed_open_leaves_no_process_and_frees_the_slot(self, origin, configured):
        from api.services import photos_origin
        origin.scenario(preview=origin.output(b"", 4))
        with pytest.raises(photos_origin.OriginError) as err:
            await photos_origin.open_stream("preview", UUID_A)
        assert err.value.kind == "not_found"
        for pid in origin.pids:
            assert await process_gone_async(pid)
        assert photos_origin._slots()._value == photos_origin.MAX_CONCURRENT_SESSIONS

    async def test_completed_stream_releases_its_slot(self, origin, configured):
        from api.services import photos_origin
        origin.scenario(preview=origin.output(b"abc"))
        session, first = await photos_origin.open_stream("preview", UUID_A)
        assert b"".join([chunk async for chunk in session.stream(first)]) == b"abc"
        assert photos_origin._slots()._value == photos_origin.MAX_CONCURRENT_SESSIONS

    async def test_close_is_idempotent(self, origin, configured):
        from api.services import photos_origin
        origin.scenario(preview={"hang": True})
        session, first = await photos_origin.open_stream("preview", UUID_A)
        session.close()
        session.close()
        assert photos_origin._slots()._value == photos_origin.MAX_CONCURRENT_SESSIONS

    async def test_client_disconnect_through_the_asgi_app_kills_the_subprocess(
        self, origin, configured, store
    ):
        """Drive the real app over raw ASGI and hang up after the first body
        chunk, the way a browser closing a download does."""
        from api.main import app
        origin.scenario(preview={"hang": True})

        sent = []
        first_body = asyncio.Event()

        async def receive():
            await first_body.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": f"/api/photos/preview/{UUID_A}",
            "raw_path": f"/api/photos/preview/{UUID_A}".encode(), "query_string": b"",
            "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), 15)

        assert sent[0]["status"] == 200
        (pid,) = origin.pids
        assert await process_gone_async(pid)

    async def test_concurrency_is_bounded(self, origin, configured):
        from api.services import photos_origin
        origin.scenario(preview={"hang": True})
        sessions = []
        for _ in range(photos_origin.MAX_CONCURRENT_SESSIONS):
            session, _first = await photos_origin.open_stream("preview", UUID_A)
            sessions.append(session)
        assert len(origin.pids) == photos_origin.MAX_CONCURRENT_SESSIONS

        with patch.object(photos_origin, "SLOT_WAIT_SECONDS", 0.3):
            with pytest.raises(photos_origin.OriginError) as err:
                await photos_origin.open_stream("preview", UUID_A)
        assert err.value.kind == "busy"
        # The refused request never started a fifth ssh.
        assert len(origin.pids) == photos_origin.MAX_CONCURRENT_SESSIONS

        for session in sessions:
            session.close()
        session, _first = await photos_origin.open_stream("preview", UUID_A)
        session.close()

    async def test_busy_maps_to_503(self, origin, configured, store):
        from api.main import app
        from httpx import ASGITransport, AsyncClient
        from api.services import photos_origin
        origin.scenario(preview={"hang": True})
        held = []
        for _ in range(photos_origin.MAX_CONCURRENT_SESSIONS):
            session, _first = await photos_origin.open_stream("preview", UUID_A)
            held.append(session)
        try:
            with patch.object(photos_origin, "SLOT_WAIT_SECONDS", 0.3):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
                    response = await http.get(f"/api/photos/preview/{UUID_A}")
            assert response.status_code == 503
            assert response.headers["x-photos-origin"] == "busy"
        finally:
            for session in held:
                session.close()
