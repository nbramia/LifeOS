#!/usr/bin/env python3
"""Safely register a new persona Telegram bot.

Adding a persona bot today is entirely manual: register the bot with
@BotFather by hand, hand-edit the environment file to place its two new
variables correctly, and restart the service — with a real landmine along
the way, since rewriting the environment file in place (rather than
appending to it) can silently turn a symlink into a plain file and break a
symlink-based config-sync setup (#601).

This script replaces the "hand-edit" step only:

    python scripts/register_persona_bot.py travel <TOKEN> <CHAT_ID>

It appends the two ``KEY=value`` lines to ``.env`` in append mode (never
truncating or rewriting existing content, so a symlinked ``.env`` keeps
pointing at the same target), and adds the bot to the per-install registry
override (``config/telegram_bots.local.json``) — seeding it from the tracked
template (``config/telegram_bots.json``) if it doesn't exist yet, since the
override *replaces* rather than merges with the template
(``config.settings._telegram_bots_source``).

Out of scope, both intentionally manual/out-of-band:
  - Registering the bot with @BotFather (getting the token in the first
    place).
  - Restarting the service — this script only reports which one to restart.
  - Writing the persona's own behavior file (``config/personas/<name>.md``);
    see docs/guides/personas.md.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import _BOT_NAME_RE  # noqa: E402

RESERVED_NAMES = {"primary"}
# The service that loads the registry + env vars at startup (api/main.py's
# lifespan starts Telegram listeners, including specialized ones from the
# registry) — see api/services/telegram.py:get_telegram_listeners().
RESTART_SERVICE = "lifeos-api"


class RegistrationError(Exception):
    """A user-facing registration failure (bad name, duplicate bot, ...)."""


def _validate_name(name: str) -> str:
    name = name.strip().lower()
    if not name or not _BOT_NAME_RE.match(name):
        raise RegistrationError(
            f"Invalid bot name {name!r} — must match {_BOT_NAME_RE.pattern!r}"
        )
    if name in RESERVED_NAMES:
        raise RegistrationError(f"Bot name {name!r} is reserved")
    return name


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def append_env_vars(env_path: Path, pairs: list[tuple[str, str]]) -> None:
    """Append ``KEY=value`` lines to ``env_path`` without truncating it.

    Uses append-mode ``open()``, which writes to the existing inode (following
    a symlink to its target) rather than replacing the path — the file, and
    a symlink at that path, are never removed or recreated.
    """
    for key, value in pairs:
        if "\n" in value or "\r" in value:
            raise RegistrationError(f"{key} value contains a newline — refusing to write it")
    needs_leading_newline = False
    if env_path.exists() and env_path.stat().st_size > 0:
        with open(env_path, "rb") as f:
            f.seek(-1, 2)
            needs_leading_newline = f.read(1) != b"\n"
    with open(env_path, "a") as f:
        if needs_leading_newline:
            f.write("\n")
        for key, value in pairs:
            f.write(f"{key}={value}\n")


def _existing_env_keys(env_path: Path) -> set[str]:
    """Keys already assigned in ``env_path`` — including ``export KEY=...``
    lines, which python-dotenv (and this project's loader) also accept."""
    if not env_path.exists():
        return set()
    keys = set()
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            keys.add(stripped.split("=", 1)[0].strip())
    return keys


def _load_registry(registry_path: Path, template_path: Path) -> list[dict]:
    """The per-install override's entries, seeded from the template if the
    override doesn't exist yet (it replaces rather than merges with the
    template — config.settings._telegram_bots_source())."""
    source = registry_path if registry_path.exists() else template_path
    if not source.exists():
        return []
    text = source.read_text().strip()
    if not text:
        return []
    entries = json.loads(text)
    if not isinstance(entries, list):
        raise RegistrationError(f"{source} must contain a JSON list")
    return entries


def _write_registry(registry_path: Path, entries: list[dict]) -> None:
    with open(registry_path, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")


def register_bot(
    name: str,
    token: str,
    chat_id: str,
    *,
    token_env: str,
    chat_id_env: str,
    persona_file: str,
    label: str,
    orchestrates: bool,
    backend: str | None,
    env_path: Path,
    registry_path: Path,
    template_path: Path,
) -> None:
    name = _validate_name(name)

    for key in (token_env, chat_id_env):
        if not _ENV_KEY_RE.match(key):
            raise RegistrationError(
                f"{key!r} is not a valid environment variable name "
                f"(must match {_ENV_KEY_RE.pattern!r})"
            )
    for key, value in ((token_env, token), (chat_id_env, chat_id)):
        if "\n" in value or "\r" in value:
            raise RegistrationError(f"{key} value contains a newline — refusing to write it")

    existing_env_keys = _existing_env_keys(env_path)
    for key in (token_env, chat_id_env):
        if key in existing_env_keys:
            raise RegistrationError(f"{key} is already set in {env_path}")

    entries = _load_registry(registry_path, template_path)
    for entry in entries:
        if (entry.get("name") or "").strip().lower() == name:
            raise RegistrationError(
                f"Bot {name!r} already has an entry in {registry_path}"
            )

    entry: dict = {
        "name": name,
        "token_env": token_env,
        "chat_id_env": chat_id_env,
        "persona_file": persona_file,
    }
    if label:
        entry["label"] = label
    if orchestrates:
        entry["orchestrates"] = True
    if backend:
        entry["backend"] = backend

    # Do the registry write first — it's the one that can raise on bad JSON
    # (_load_registry above) or a duplicate name, and we'd rather fail before
    # touching the environment file than after.
    _write_registry(registry_path, entries + [entry])
    append_env_vars(env_path, [(token_env, token), (chat_id_env, chat_id)])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register a new persona Telegram bot's env vars + registry entry."
    )
    parser.add_argument("name", help="Bot name, e.g. 'travel' (lowercase, [a-z0-9_-]+)")
    parser.add_argument("token", help="Bot token from @BotFather")
    parser.add_argument("chat_id", help="Telegram chat id to receive this bot's messages")
    parser.add_argument(
        "--token-env", default=None,
        help="Env var name for the token (default: TELEGRAM_<NAME>_BOT_TOKEN)",
    )
    parser.add_argument(
        "--chat-id-env", default=None,
        help="Env var name for the chat id (default: TELEGRAM_<NAME>_CHAT_ID)",
    )
    parser.add_argument(
        "--persona-file", default=None,
        help="Persona file path (default: config/personas/<name>.md)",
    )
    parser.add_argument("--label", default="", help="Display label (default: <name>.capitalize())")
    parser.add_argument(
        "--orchestrates", action="store_true",
        help="Mark this bot as a self-repair-style orchestrator (rare — see docs/guides/personas.md)",
    )
    parser.add_argument("--backend", choices=["hermes", "lifeos"], default=None)
    parser.add_argument(
        "--project-root", default=".",
        help="Repo root the .env / config files live under (default: cwd)",
    )
    args = parser.parse_args()

    root = Path(args.project_root)
    # Bot names may contain hyphens (_BOT_NAME_RE allows [a-z0-9_-]+), but env
    # var names can't — swap them to underscores so the default is always a
    # valid, shell-sourceable identifier (e.g. "travel-bot" -> TRAVEL_BOT).
    name_upper = args.name.strip().upper().replace("-", "_")
    token_env = args.token_env or f"TELEGRAM_{name_upper}_BOT_TOKEN"
    chat_id_env = args.chat_id_env or f"TELEGRAM_{name_upper}_CHAT_ID"
    persona_file = args.persona_file or f"config/personas/{args.name.strip().lower()}.md"

    try:
        register_bot(
            args.name,
            args.token,
            args.chat_id,
            token_env=token_env,
            chat_id_env=chat_id_env,
            persona_file=persona_file,
            label=args.label,
            orchestrates=args.orchestrates,
            backend=args.backend,
            env_path=root / ".env",
            registry_path=root / "config" / "telegram_bots.local.json",
            template_path=root / "config" / "telegram_bots.json",
        )
    except RegistrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Registered bot {args.name!r}:")
    print(f"  - Appended {token_env} and {chat_id_env} to {root / '.env'}")
    print(f"  - Added entry to {root / 'config' / 'telegram_bots.local.json'}")
    print(f"  - Persona file (not created by this script): {persona_file}")
    print()
    print(f"Restart required: {RESTART_SERVICE} (./scripts/server.sh restart)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
