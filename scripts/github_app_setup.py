"""One-time GitHub App registration for the candidate-verification issuer.

GitHub's App Manifest flow is the only programmatic registration path, and
it still needs one authenticated-browser action: opening a submission page
and clicking "Create GitHub App" — there is no fully headless way to create
an App at any permission level. That action does not require a specific
human; any authenticated browser session with the right access (an
operator's own browser, or an authorized automated browser session acting
on their behalf) can perform it. Everything else here is scriptable and is
scripted: this module builds the manifest, runs a local callback server to
catch the resulting temporary `code`, exchanges that code for the App's
credentials, verifies the App is actually installed on the target
repository (registering an App and installing it are two separate steps),
confirms the destination environment's branch policy is actually locked to
`main` before writing anything, and only then writes the credentials
straight into the `candidate-verification-publish` GitHub Environment's
secret/variable store (see docs/guides/candidate-verification-ci.md) — the
private key is never printed, logged, or returned to a caller; only the
(non-secret) App id and slug are.

The requested permission is `checks: write` only, no other permission and
no webhook events, since this App exists solely to issue the
`candidate-verification` check.
"""
from __future__ import annotations

import base64
import html
import json
import os
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Sequence

from scripts.candidate_ci_setup import CandidateCiSetupError, audit_environment, require_environment_locked_to_main

Runner = Callable[..., subprocess.CompletedProcess]


class AppSetupError(RuntimeError):
    """The App registration, installation, or credential-storage flow failed."""


APP_NAME = "lifeos-candidate-verification"
CALLBACK_PATH = "/callback"


def build_app_manifest(*, redirect_url: str, app_name: str = APP_NAME) -> dict:
    """The exact manifest submitted to GitHub's App Manifest flow.

    `public: False` — this App is installed only on the operator's own
    repository, never listed. No webhook (`hook_attributes` omitted) and no
    subscribed events: this App issues check runs from the publisher's own
    workflow run, it never needs GitHub to push it anything.
    """
    return {
        "name": app_name,
        "url": "https://github.com/nbramia/LifeOS",
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": {"checks": "write"},
        "default_events": [],
    }


def manifest_submission_html(manifest: dict, *, state: str, target: str = "https://github.com/settings/apps/new") -> str:
    """A minimal auto-submitting form POSTing the manifest to GitHub.

    GitHub's manifest flow takes the manifest as a hidden form field named
    `manifest`, POSTed to this URL; `state` round-trips through the
    resulting redirect purely as CSRF protection for the callback below —
    this module refuses to proceed if it doesn't match. The manifest JSON
    is HTML-escaped (quotes included) before embedding: an unescaped
    apostrophe or double-quote anywhere in it — an app name, a URL — would
    otherwise terminate the attribute early and corrupt the form.
    """
    manifest_json = html.escape(json.dumps(manifest), quote=True)
    return (
        "<!doctype html><html><body>"
        f"<form id=\"f\" action=\"{html.escape(target, quote=True)}?state={urllib.parse.quote(state)}\" method=\"post\">"
        f"<input type=\"hidden\" name=\"manifest\" value=\"{manifest_json}\">"
        "<button type=\"submit\">Create GitHub App</button>"
        "</form>"
        "<script>document.getElementById('f').submit();</script>"
        "</body></html>"
    )


@dataclass(frozen=True)
class CallbackRequest:
    valid: bool
    code: str = ""
    reason: str = ""


def parse_callback_request(request_path: str, *, expected_state: str) -> CallbackRequest:
    """Validate one incoming request path against the callback contract.

    Never raises: an invalid request (wrong path — a browser's automatic
    `/favicon.ico` probe, say — missing/mismatched state, no code) is a
    normal, expected event the server must keep listening past, not a
    reason to abort the whole registration. Only the caller that receives
    ``valid=True`` gets to stop waiting.
    """
    parsed = urllib.parse.urlparse(request_path)
    if parsed.path != CALLBACK_PATH:
        return CallbackRequest(valid=False, reason=f"unexpected path {parsed.path!r}")
    params = urllib.parse.parse_qs(parsed.query)
    state_values = params.get("state") or []
    if not state_values or state_values[0] != expected_state:
        return CallbackRequest(valid=False, reason="callback state did not match")
    code_values = params.get("code") or []
    if not code_values:
        return CallbackRequest(valid=False, reason="callback carried no code")
    return CallbackRequest(valid=True, code=code_values[0])


# Backward-compatible alias used by earlier callers/tests that want the
# raise-on-invalid form of the same check.
def extract_callback_code(request_path: str, *, expected_state: str) -> str:
    result = parse_callback_request(request_path, expected_state=expected_state)
    if not result.valid:
        raise AppSetupError(result.reason)
    return result.code


def _make_callback_handler(expected_state: str, result_holder: list):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr access log
            pass

        def do_GET(self):
            outcome = parse_callback_request(self.path, expected_state=expected_state)
            if outcome.valid:
                result_holder.append(outcome.code)
                body, status = b"GitHub App created. You can close this tab.", 200
            else:
                body, status = outcome.reason.encode(), 400
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

    return _Handler


def await_callback_code(*, port: int, expected_state: str, timeout_seconds: float = 600.0) -> str:
    """Serve requests on ``port`` until a valid callback arrives or the
    deadline passes. A single invalid request (a favicon probe, a stray
    retry, a wrong state) is logged and ignored — the loop keeps listening,
    it never aborts registration over one bad request.
    """
    result_holder: list = []
    server = HTTPServer(("127.0.0.1", port), _make_callback_handler(expected_state, result_holder))
    deadline = time.monotonic() + timeout_seconds
    try:
        while not result_holder:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppSetupError(f"no valid callback received on port {port} within {timeout_seconds}s")
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    return result_holder[0]


@dataclass(frozen=True)
class AppCredentials:
    app_id: int
    slug: str


def _redact_if_sensitive(message: str) -> str:
    if "BEGIN" in message and "KEY" in message:
        return "detail withheld: response may contain key material"
    return message


def exchange_manifest_code(code: str, *, run: Runner = subprocess.run) -> tuple[AppCredentials, str]:
    """Convert the one-time manifest code into real App credentials.

    Returns the non-secret identity (id, slug) separately from the PEM
    private key — callers must pass the key straight to
    ``store_app_credentials`` and never print, log, or return it further.
    """
    result = run(
        ["gh", "api", "--method", "POST", f"/app-manifests/{code}/conversions"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AppSetupError(f"manifest code exchange failed: {_redact_if_sensitive(result.stderr.strip())}")
    data = json.loads(result.stdout)
    private_key = data.get("pem")
    if not private_key:
        raise AppSetupError("manifest code exchange response carried no private key")
    return AppCredentials(app_id=data["id"], slug=data.get("slug", "")), private_key


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _app_jwt(app_id: int, private_key_pem: str, *, now: Callable[[], float] = time.time) -> str:
    """Create the short-lived App JWT needed for GitHub App endpoints.

    The key is intentionally kept in memory: this returns only the signed
    token to ``gh`` and never writes either credential to disk or argv.
    """
    from google.auth.crypt import RSASigner

    issued_at = int(now())
    header = _base64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _base64url(json.dumps({"iat": issued_at - 60, "exp": issued_at + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    signed = f"{header}.{claims}"
    try:
        signer = RSASigner.from_string(private_key_pem)
    except (TypeError, ValueError) as exc:
        raise AppSetupError("could not load App private key") from exc
    signature = signer.sign(signed.encode("ascii"))
    return f"{signed}.{_base64url(signature)}"


def verify_app_installed(repository: str, app_id: int, private_key_pem: str, *, run: Runner = subprocess.run) -> bool:
    """Check whether this App is installed on exactly ``repository``.

    ``/user/installations`` authenticates as the CLI's OAuth user and can
    reject a just-created App.  The repository installation endpoint instead
    receives an in-memory App JWT signed with the manifest's held PEM.
    """
    token = _app_jwt(app_id, private_key_pem)
    result = run(
        ["gh", "api", f"repos/{repository}/installation"],
        env={**os.environ, "GH_TOKEN": token}, capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()
        if "404" in detail:
            return False
        raise AppSetupError(f"could not look up App installation: {_redact_if_sensitive(detail)}")
    return json.loads(result.stdout).get("app_id") == app_id


def install_url(slug: str) -> str:
    """Non-secret URL for the (separate, second) authenticated-browser
    action of actually installing an already-registered App."""
    return f"https://github.com/apps/{slug}/installations/new"


def await_app_installed(
    repository: str, app_id: int, private_key_pem: str, *, timeout_seconds: float = 1800.0, poll_interval_seconds: float = 5.0,
    run: Runner = subprocess.run, sleep: Callable[[float], None] = None, now: Callable[[], float] = None,
) -> bool:
    """Poll ``verify_app_installed`` until it turns true or the deadline
    passes. Registering an App does not install it, and a caller holding a
    just-exchanged private key in memory must not discard it just because
    installation hasn't happened yet — that key exists nowhere else and
    forces re-registration if lost. This keeps the caller waiting (key
    still only ever in memory) through the ordinary registration →
    installation gap instead.
    """
    import time as _time

    sleep = sleep or _time.sleep
    now = now or _time.monotonic
    deadline = now() + timeout_seconds
    while True:
        if verify_app_installed(repository, app_id, private_key_pem, run=run):
            return True
        if now() >= deadline:
            return False
        sleep(poll_interval_seconds)


def store_app_credentials(
    repository: str, environment: str, credentials: AppCredentials, private_key_pem: str, *,
    run: Runner = subprocess.run,
) -> None:
    """Write the App id as an environment VARIABLE and the private key as an
    environment SECRET, scoped to ``environment`` — never repository-level
    (see docs/guides/candidate-verification-ci.md for why that scoping is
    the actual security boundary). The PEM is passed via stdin, never a CLI
    argument, so it never touches argv or shell history.

    Refuses before writing anything unless ``environment``'s deployment
    branch policy is independently verified to be locked to exactly
    ``main`` — this function does not trust its own ``environment``
    argument's name to mean the policy is actually configured; a
    typo'd or not-yet-locked-down environment would otherwise silently
    receive the key with no real boundary at all.
    """
    try:
        require_environment_locked_to_main(audit_environment(repository, environment, run=run))
    except CandidateCiSetupError as exc:
        raise AppSetupError(f"refusing to store credentials: {exc}") from exc

    variable_result = run(
        ["gh", "variable", "set", "LIFEOS_CANDIDATE_APP_ID",
         "--repo", repository, "--env", environment, "--body", str(credentials.app_id)],
        capture_output=True, text=True,
    )
    if variable_result.returncode != 0:
        raise AppSetupError(f"could not store LIFEOS_CANDIDATE_APP_ID: {variable_result.stderr.strip()}")
    secret_result = run(
        ["gh", "secret", "set", "LIFEOS_CANDIDATE_APP_PRIVATE_KEY",
         "--repo", repository, "--env", environment],
        input=private_key_pem, capture_output=True, text=True,
    )
    if secret_result.returncode != 0:
        raise AppSetupError("could not store LIFEOS_CANDIDATE_APP_PRIVATE_KEY (detail withheld: may echo key material)")


def _main(argv: Sequence[str]) -> int:
    import argparse
    import secrets as secrets_module

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="OWNER/REPOSITORY to install the App on")
    parser.add_argument("--environment", default="candidate-verification-publish")
    parser.add_argument("--redirect-port", type=int, default=8734)
    parser.add_argument("--manifest-html-out", default="github-app-manifest.html",
                         help="Where to write the auto-submitting manifest form to open in a browser")
    parser.add_argument("--install-timeout-seconds", type=float, default=1800.0,
                         help="How long to keep the exchanged key in memory waiting for installation")
    args = parser.parse_args(argv)

    state = secrets_module.token_urlsafe(24)
    redirect_url = f"http://127.0.0.1:{args.redirect_port}{CALLBACK_PATH}"
    manifest = build_app_manifest(redirect_url=redirect_url)
    html_doc = manifest_submission_html(manifest, state=state)
    with open(args.manifest_html_out, "w", encoding="utf-8") as handle:
        handle.write(html_doc)
    print(f"Wrote {args.manifest_html_out}. Open it in an authenticated GitHub browser session and submit it.")
    print(f"Waiting for the callback on {redirect_url} ...")

    code = await_callback_code(port=args.redirect_port, expected_state=state)
    # The private key from here on exists ONLY in this process's memory —
    # registering the App does not install it, so this process keeps
    # running and holding it rather than discarding it and forcing the
    # operator to recreate the App from scratch if installation takes a
    # moment.
    credentials, private_key_pem = exchange_manifest_code(code)
    if not verify_app_installed(args.repository, credentials.app_id, private_key_pem):
        print(f"App {credentials.slug!r} (id {credentials.app_id}) created but not installed yet.")
        print(f"Install it on {args.repository} now: {install_url(credentials.slug)}")
        print(f"Waiting up to {args.install_timeout_seconds}s for the installation to appear ...")
        if not await_app_installed(args.repository, credentials.app_id, private_key_pem, timeout_seconds=args.install_timeout_seconds):
            raise AppSetupError(
                f"App {credentials.slug!r} (id {credentials.app_id}) was still not installed on "
                f"{args.repository} after {args.install_timeout_seconds}s. Re-run this script with a "
                f"longer --install-timeout-seconds once you're ready to complete installation promptly — "
                f"the exchanged key cannot be recovered from a prior run, only from a fresh registration."
            )
    store_app_credentials(args.repository, args.environment, credentials, private_key_pem)
    print(json.dumps({"app_id": credentials.app_id, "slug": credentials.slug, "installed": True, "stored": True}))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
