"""Tests for scripts/github_app_setup.py.

Only `exchange_manifest_code`/`verify_app_installed`/`store_app_credentials`
use an injected fake runner instead of the network. `await_callback_code`'s
real local HTTP server IS exercised directly against a real socket, from a
background thread acting as the "browser" — no actual browser is needed to
test it, only a plain HTTP client hitting 127.0.0.1. The private key must
never appear in a returned/printed value other than the one deliberate
``exchange_manifest_code`` return slot.
"""
from __future__ import annotations

import base64
import html
import json
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from scripts.github_app_setup import (
    AppCredentials,
    AppSetupError,
    await_app_installed,
    await_callback_code,
    build_app_manifest,
    exchange_manifest_code,
    extract_callback_code,
    install_url,
    manifest_submission_html,
    parse_callback_request,
    store_app_credentials,
    verify_app_installed,
    _app_jwt,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def app_private_key_pem(tmp_path_factory) -> str:
    """Generate a disposable test key without embedding any credential."""
    key_path = tmp_path_factory.mktemp("github-app-jwt") / "app.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-out", str(key_path), "-pkeyopt", "rsa_keygen_bits:2048"],
        check=True, capture_output=True, text=True,
    )
    return key_path.read_text()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# manifest / form construction
# ---------------------------------------------------------------------------

def test_build_app_manifest_requests_only_checks_write_and_no_events():
    manifest = build_app_manifest(redirect_url="http://127.0.0.1:8734/callback")
    assert manifest["default_permissions"] == {"checks": "write"}
    assert manifest["default_events"] == []
    assert manifest["public"] is False
    assert "hook_attributes" not in manifest
    assert manifest["redirect_url"] == "http://127.0.0.1:8734/callback"


def test_manifest_submission_html_embeds_the_manifest_and_state():
    manifest = build_app_manifest(redirect_url="http://127.0.0.1:8734/callback")
    doc = manifest_submission_html(manifest, state="unique-state-value")
    assert "unique-state-value" in doc
    assert "https://github.com/settings/apps/new" in doc
    start = doc.index('value="') + len('value="')
    end = doc.index('"', start)
    recovered = json.loads(html.unescape(doc[start:end]))
    assert recovered == manifest


def test_manifest_submission_html_escapes_an_apostrophe_in_the_manifest():
    """A raw apostrophe or double-quote anywhere in the manifest (an app
    name, a URL) must not be able to terminate the attribute early."""
    manifest = build_app_manifest(redirect_url="http://127.0.0.1:8734/callback", app_name="nathan's app \"test\"")
    doc = manifest_submission_html(manifest, state="s")
    start = doc.index('value="') + len('value="')
    end = doc.index('"', start)
    attribute_value = doc[start:end]
    assert "'" not in attribute_value or "&#x27;" in doc
    recovered = json.loads(html.unescape(attribute_value))
    assert recovered["name"] == "nathan's app \"test\""


# ---------------------------------------------------------------------------
# callback request parsing: wrong path / bad state must never abort
# ---------------------------------------------------------------------------

def test_parse_callback_request_accepts_matching_path_state_and_code():
    result = parse_callback_request("/callback?code=abc123&state=xyz", expected_state="xyz")
    assert result.valid and result.code == "abc123"


def test_parse_callback_request_rejects_wrong_path_without_raising():
    result = parse_callback_request("/favicon.ico", expected_state="xyz")
    assert not result.valid and "path" in result.reason


def test_parse_callback_request_rejects_mismatched_state_without_raising():
    result = parse_callback_request("/callback?code=abc123&state=WRONG", expected_state="xyz")
    assert not result.valid and "state" in result.reason


def test_parse_callback_request_rejects_missing_code_without_raising():
    result = parse_callback_request("/callback?state=xyz", expected_state="xyz")
    assert not result.valid and "code" in result.reason


def test_extract_callback_code_still_raises_for_callers_that_want_that():
    assert extract_callback_code("/callback?code=abc123&state=xyz", expected_state="xyz") == "abc123"
    with pytest.raises(AppSetupError, match="state did not match"):
        extract_callback_code("/callback?code=abc123&state=WRONG", expected_state="xyz")


# ---------------------------------------------------------------------------
# await_callback_code: a real local socket, no browser needed to test it
# ---------------------------------------------------------------------------

def test_await_callback_code_survives_an_invalid_request_then_succeeds():
    """Proves the server keeps listening past a bad request (a favicon
    probe, a stray retry) instead of aborting registration over it."""
    port = _free_port()
    result: dict = {}

    def server_thread():
        result["code"] = await_callback_code(port=port, expected_state="right-state", timeout_seconds=10.0)

    thread = threading.Thread(target=server_thread, daemon=True)
    thread.start()

    def _get(path: str) -> int:
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    # A bad request first — must not stop the server.
    assert _get("/favicon.ico") == 400
    # A mismatched-state request second — must also not stop it.
    assert _get("/callback?code=wrong&state=nope") == 400
    # The real callback.
    assert _get("/callback?code=REALCODE&state=right-state") == 200

    thread.join(timeout=5)
    assert result["code"] == "REALCODE"


def test_await_callback_code_times_out_when_nothing_valid_arrives():
    port = _free_port()
    with pytest.raises(AppSetupError, match="no valid callback"):
        await_callback_code(port=port, expected_state="s", timeout_seconds=0.3)


# ---------------------------------------------------------------------------
# exchange_manifest_code
# ---------------------------------------------------------------------------

def test_exchange_manifest_code_returns_identity_separately_from_private_key():
    def run(args, **kwargs):
        assert args == ["gh", "api", "--method", "POST", "/app-manifests/onetimecode/conversions"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "id": 123456, "slug": "lifeos-candidate-verification",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----\n",
        }), stderr="")

    credentials, private_key = exchange_manifest_code("onetimecode", run=run)
    assert credentials == AppCredentials(app_id=123456, slug="lifeos-candidate-verification")
    assert "BEGIN RSA PRIVATE KEY" in private_key


def test_exchange_manifest_code_raises_on_failure_without_leaking_body():
    def run(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="410 Gone")

    with pytest.raises(AppSetupError, match="410 Gone"):
        exchange_manifest_code("expiredcode", run=run)


def test_exchange_manifest_code_redacts_key_looking_error_text():
    def run(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="-----BEGIN RSA PRIVATE KEY----- leaked")

    with pytest.raises(AppSetupError) as excinfo:
        exchange_manifest_code("code", run=run)
    assert "BEGIN RSA PRIVATE KEY" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# verify_app_installed
# ---------------------------------------------------------------------------

def _installation_runner(response):
    def run(args, **kwargs):
        assert args == ["gh", "api", "repos/nbramia/LifeOS/installation"]
        assert "GH_TOKEN" in kwargs["env"]
        return response
    return run


def test_verify_app_installed_uses_held_pem_as_an_app_jwt(app_private_key_pem):
    seen = {}

    def run(args, **kwargs):
        assert args == ["gh", "api", "repos/nbramia/LifeOS/installation"]
        seen["token"] = kwargs["env"]["GH_TOKEN"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"app_id": 999}), stderr="")

    assert verify_app_installed("nbramia/LifeOS", 999, app_private_key_pem, run=run) is True
    # A three-part JWT passed via GH_TOKEN is materially different from the
    # CLI OAuth lookup at /user/installations, and the held PEM never leaves
    # this process as an argument or output value.
    assert seen["token"].count(".") == 2
    assert app_private_key_pem not in seen["token"]


def test_app_jwt_signature_verifies_with_the_generated_public_key(app_private_key_pem, tmp_path):
    token = _app_jwt(999, app_private_key_pem, now=lambda: 1_700_000_000)
    header, claims, encoded_signature = token.split(".")

    def decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    assert json.loads(decode(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(decode(claims)) == {"iat": 1_699_999_940, "exp": 1_700_000_540, "iss": "999"}

    key_path = tmp_path / "app.pem"
    public_key = tmp_path / "app.pub"
    payload = tmp_path / "payload"
    signature = tmp_path / "signature"
    key_path.write_text(app_private_key_pem)
    payload.write_text(f"{header}.{claims}")
    signature.write_bytes(decode(encoded_signature))
    subprocess.run(["openssl", "pkey", "-in", str(key_path), "-pubout", "-out", str(public_key)], check=True)
    verified = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(public_key), "-signature", str(signature), str(payload)],
        check=False, capture_output=True, text=True,
    )
    assert verified.returncode == 0, verified.stderr


def test_verify_app_installed_false_when_repository_has_no_installation(app_private_key_pem):
    run = _installation_runner(SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404: Not Found"))
    assert verify_app_installed("nbramia/LifeOS", 999, app_private_key_pem, run=run) is False


def test_verify_app_installed_false_when_repository_installation_is_another_app(app_private_key_pem):
    run = _installation_runner(SimpleNamespace(returncode=0, stdout=json.dumps({"app_id": 998}), stderr=""))
    assert verify_app_installed("nbramia/LifeOS", 999, app_private_key_pem, run=run) is False


def test_verify_app_installed_does_not_leak_held_pem_on_lookup_failure(app_private_key_pem):
    run = _installation_runner(SimpleNamespace(returncode=1, stdout="", stderr="HTTP 403: denied"))
    with pytest.raises(AppSetupError) as excinfo:
        verify_app_installed("nbramia/LifeOS", 999, app_private_key_pem, run=run)
    assert app_private_key_pem not in str(excinfo.value)


# ---------------------------------------------------------------------------
# store_app_credentials: refuses unless the environment is actually locked
# ---------------------------------------------------------------------------

def _locked_environment_run(extra_handlers=None):
    def run(args, **kwargs):
        if args[-1] == "repos/nbramia/LifeOS/environments/candidate-verification-publish":
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
            }), stderr="")
        if args[-1] == "repos/nbramia/LifeOS/environments/candidate-verification-publish/deployment-branch-policies":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"branch_policies": [{"name": "main"}]}), stderr="")
        if extra_handlers:
            for matcher, response in extra_handlers:
                if matcher(args):
                    return response
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_store_app_credentials_writes_id_as_variable_and_key_as_secret_via_stdin():
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs.get("input")))
        return _locked_environment_run()(args, **kwargs)

    store_app_credentials(
        "nbramia/LifeOS", "candidate-verification-publish",
        AppCredentials(app_id=999, slug="lifeos-candidate-verification"),
        "SECRET-PEM-CONTENT",
        run=run,
    )

    variable_calls = [c for c in calls if c[0][:3] == ["gh", "variable", "set"]]
    secret_calls = [c for c in calls if c[0][:3] == ["gh", "secret", "set"]]
    assert variable_calls[0][0] == [
        "gh", "variable", "set", "LIFEOS_CANDIDATE_APP_ID",
        "--repo", "nbramia/LifeOS", "--env", "candidate-verification-publish", "--body", "999",
    ]
    assert secret_calls[0][0] == [
        "gh", "secret", "set", "LIFEOS_CANDIDATE_APP_PRIVATE_KEY",
        "--repo", "nbramia/LifeOS", "--env", "candidate-verification-publish",
    ]
    assert secret_calls[0][1] == "SECRET-PEM-CONTENT", "the PEM must be passed via stdin, never a CLI argument"


def test_store_app_credentials_refuses_when_environment_uses_protected_branches_fallback():
    def run(args, **kwargs):
        if args[-1] == "repos/nbramia/LifeOS/environments/candidate-verification-publish":
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False},
            }), stderr="")
        raise AssertionError(f"must not reach gh variable/secret set: {args}")

    with pytest.raises(AppSetupError, match="protected branches"):
        store_app_credentials(
            "nbramia/LifeOS", "candidate-verification-publish",
            AppCredentials(app_id=999, slug="x"), "SECRET-PEM-CONTENT", run=run,
        )


def test_store_app_credentials_failure_message_never_echoes_key_material():
    def run(args, **kwargs):
        if args[:3] == ["gh", "secret", "set"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="SECRET-PEM-CONTENT rejected by API")
        return _locked_environment_run()(args, **kwargs)

    with pytest.raises(AppSetupError) as excinfo:
        store_app_credentials(
            "nbramia/LifeOS", "candidate-verification-publish",
            AppCredentials(app_id=999, slug="x"), "SECRET-PEM-CONTENT", run=run,
        )
    assert "SECRET-PEM-CONTENT" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# await_app_installed / install_url: the key must survive the ordinary
# registration -> installation gap, never be discarded because installation
# hasn't happened the instant registration finishes.
# ---------------------------------------------------------------------------

def test_install_url_is_non_secret_and_app_specific():
    assert install_url("lifeos-candidate-verification") == "https://github.com/apps/lifeos-candidate-verification/installations/new"


def test_await_app_installed_transitions_false_to_true_and_returns_true(app_private_key_pem):
    calls = {"n": 0}

    def run(args, **kwargs):
        calls["n"] += 1
        assert args == ["gh", "api", "repos/nbramia/LifeOS/installation"]
        assert kwargs["env"]["GH_TOKEN"].count(".") == 2
        if calls["n"] < 3:  # not installed on the first two polls
            return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404: Not Found")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"app_id": 999}), stderr="")

    clock = iter([0, 1, 2, 3])
    sleeps = []
    result = await_app_installed(
        "nbramia/LifeOS", 999, app_private_key_pem, timeout_seconds=100, poll_interval_seconds=1,
        run=run, sleep=lambda s: sleeps.append(s), now=lambda: next(clock),
    )
    assert result is True
    assert sleeps == [1, 1]


def test_await_app_installed_returns_false_on_timeout_without_raising(app_private_key_pem):
    def run(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404: Not Found")

    clock = iter([0, 50, 150])
    result = await_app_installed(
        "nbramia/LifeOS", 999, app_private_key_pem, timeout_seconds=100, poll_interval_seconds=1,
        run=run, sleep=lambda s: None, now=lambda: next(clock),
    )
    assert result is False


def test_end_to_end_setup_keeps_key_in_memory_through_delayed_installation_and_stores_it_exactly(app_private_key_pem):
    """The full registration -> delayed installation -> storage flow, using
    the module's own functions the way _main wires them: the key returned
    by exchange_manifest_code must survive an initial not-installed result
    and be the EXACT value handed to store_app_credentials once
    installation is confirmed — never re-fetched, never logged in between.
    """
    real_pem = app_private_key_pem
    calls = {"n": 0}
    stored = {}

    def run(args, **kwargs):
        if args == ["gh", "api", "--method", "POST", "/app-manifests/onetimecode/conversions"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "id": 999, "slug": "lifeos-candidate-verification", "pem": real_pem,
            }), stderr="")
        if args == ["gh", "api", "repos/nbramia/LifeOS/installation"]:
            calls["n"] += 1
            installed = calls["n"] >= 2  # false on first poll, true after
            assert kwargs["env"]["GH_TOKEN"].count(".") == 2
            if not installed:
                return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404: Not Found")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"app_id": 999}), stderr="")
        if args[-1] == "repos/nbramia/LifeOS/environments/candidate-verification-publish":
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
            }), stderr="")
        if args[-1] == "repos/nbramia/LifeOS/environments/candidate-verification-publish/deployment-branch-policies":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"branch_policies": [{"name": "main"}]}), stderr="")
        if args[:3] == ["gh", "variable", "set"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ["gh", "secret", "set"]:
            stored["pem"] = kwargs.get("input")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected call: {args}")

    credentials, private_key_pem = exchange_manifest_code("onetimecode", run=run)
    assert private_key_pem == real_pem

    installed = verify_app_installed("nbramia/LifeOS", credentials.app_id, private_key_pem, run=run)
    assert installed is False, "a freshly registered App is not installed yet on the first check"

    clock = iter([0, 1, 2])
    fully_installed = await_app_installed(
        "nbramia/LifeOS", credentials.app_id, private_key_pem, timeout_seconds=10, poll_interval_seconds=1,
        run=run, sleep=lambda s: None, now=lambda: next(clock),
    )
    assert fully_installed is True

    store_app_credentials("nbramia/LifeOS", "candidate-verification-publish", credentials, private_key_pem, run=run)
    assert stored["pem"] == real_pem, "the exact in-memory key must be what gets stored, never re-derived"
