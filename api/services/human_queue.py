"""Human queue: fire-and-forget cards any agent can file for the operator (#852).

A human-queue card is a task (`api/services/task_manager.py`) with tag
`human` and status `blocked` ("open"). This module is the single place that
knows the card shape (`fields`: `key`, `source_host`, `source_cwd`,
`source_session`, `done_when`) and its dedupe/resolve rules, so the REST
routes (`api/routes/tasks.py`), the native chat tool (`api/services/
agent_tools.py`), and the morning-briefing line (`api/services/briefings.py`)
all agree on it. The worker's `done_when` poll tick
(`api/services/agent_worker/worker.py`) talks to this queue over the HTTP
API instead — see that module for why.

`done_when` is stored as a compact JSON string in the `done_when` field
(fields values may not contain a newline or `<!--`, but JSON free of those
round-trips cleanly through the markdown task line — see
`task_manager._validate_text_fields`).
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from api.services.task_manager import Task, get_task_manager

logger = logging.getLogger(__name__)

TAG = "human"
STATUS_OPEN = "blocked"

# `done_when` types. Deliberately excludes a `shell` type — a remote-
# execution surface reachable by any agent (see the issue's Out of Scope).
_ENDPOINT_TYPE = "endpoint"
_FILE_EXISTS_TYPE = "file_exists"

# Dedupe key shape — kept narrow because a key is interpolated unescaped
# into a URL path segment (`PUT /api/tasks/human-queue/{id_or_key}/resolve`
# and the sync integration's `sync:<source>` keys); `/`, `?`, `#` would
# split the path or start a query/fragment, making the card unresolvable
# by key. Word characters plus `.`, `:`, `-` cover every key this codebase
# files (`sync:gmail`, `monarch-reauth`) with room for more.
_KEY_RE = re.compile(r"^[\w.:-]+$")

# `equals` must be JSON-scalar — anything else can't round-trip through the
# compact JSON stored in the `done_when` field, or through the worker's `==`
# comparison against a JSON-Pointer-extracted value.
_SCALAR_TYPES = (str, int, float, bool, type(None))


class DoneWhenError(ValueError):
    """Raised for a malformed `done_when`. The route layer maps this to
    HTTP 422, same as `task_manager.TaskManager`'s own `ValueError`s."""


def _reject_bracket(name: str, value: str) -> None:
    """`task_manager._validate_text_fields` rejects a `]` anywhere in a
    `fields` value (done_when is stored as one compact-JSON fields value),
    which would otherwise surface as an opaque `fields['done_when'] must
    not contain ']'` error with no hint of which done_when key was at
    fault. Check each string sub-value up front so the error names it."""
    if "]" in value:
        raise DoneWhenError(f"done_when.{name} must not contain ']'")


def validate_done_when(done_when: Optional[dict]) -> Optional[dict]:
    """Validate and normalize a `done_when` dict, or return None unchanged.

    Only `{type: "endpoint", path, pointer, equals}` and
    `{type: "file_exists", path}` are accepted; anything else raises
    `DoneWhenError`. Not implemented as FastAPI/pydantic field validation —
    `api/main.py`'s global `RequestValidationError` handler converts every
    pydantic validation failure to HTTP 400, but the issue requires 422 for
    an invalid `done_when` — so this is a manual check the route layer
    catches and maps to 422 itself, matching the existing
    `_require_valid_status` pattern in `api/routes/tasks.py`.

    `path` (both types) must be a string starting with `/` and not `//` —
    the worker builds the request URL as `f"{api_base}{path}"`
    (`agent_worker/worker.py`), so an unvalidated `path` like `@host/x` or
    `//host/x` would re-parse the authority and make the worker's
    `done_when` poll an SSRF primitive reachable by any agent that can file
    a card. `pointer` must be a string; `equals` must be a JSON scalar.
    """
    if done_when is None:
        return None
    if not isinstance(done_when, dict):
        raise DoneWhenError("done_when must be an object")
    dw_type = done_when.get("type")
    if dw_type == _ENDPOINT_TYPE:
        missing = [k for k in ("path", "pointer", "equals") if k not in done_when]
        if missing:
            raise DoneWhenError(
                f"done_when type 'endpoint' missing required key(s): {', '.join(missing)}"
            )
        path, pointer, equals = done_when["path"], done_when["pointer"], done_when["equals"]
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise DoneWhenError("done_when.path must be a string starting with '/' (not '//')")
        if not isinstance(pointer, str):
            raise DoneWhenError("done_when.pointer must be a string")
        if not isinstance(equals, _SCALAR_TYPES):
            raise DoneWhenError("done_when.equals must be a string, number, boolean, or null")
        _reject_bracket("path", path)
        _reject_bracket("pointer", pointer)
        if isinstance(equals, str):
            _reject_bracket("equals", equals)
        return {
            "type": _ENDPOINT_TYPE,
            "path": path,
            "pointer": pointer,
            "equals": equals,
        }
    if dw_type == _FILE_EXISTS_TYPE:
        if "path" not in done_when:
            raise DoneWhenError("done_when type 'file_exists' missing required key: path")
        path = done_when["path"]
        if not isinstance(path, str) or not path:
            raise DoneWhenError("done_when.path must be a non-empty string")
        _reject_bracket("path", path)
        return {"type": _FILE_EXISTS_TYPE, "path": path}
    raise DoneWhenError(
        f"invalid done_when type {dw_type!r}; must be 'endpoint' or 'file_exists'"
    )


def _has_tag(task: Task, tag: str) -> bool:
    return any(t.lstrip("#").lower() == tag for t in task.tags)


def _find_open_card_by_key(key: str) -> Optional[Task]:
    manager = get_task_manager()
    for t in manager.list_tasks(tag=TAG, status=STATUS_OPEN):
        if t.fields.get("key") == key:
            return t
    return None


def _find_any_card(id_or_key: str) -> Optional[Task]:
    """Find the human-queue (tag `human`) card matched by task id or dedupe
    key, for resolve. The key lookup is OPEN-only (`_find_open_card_by_key`)
    rather than an all-status scan: `resolve_card` only ever resolves an
    open card anyway, and once a key has both a done card (from a prior
    resolve) and a reopened open card, an all-status scan could return
    either one depending on list order — sometimes the done card, which
    `resolve_card` then rejects as not-open, turning a legitimate
    resolve-by-key into a spurious 404. An id or key that only matches a
    non-human task, or matches nothing, is "not found" from this queue's
    perspective.
    """
    manager = get_task_manager()
    task = manager.get(id_or_key)
    if task is not None and _has_tag(task, TAG):
        return task
    return _find_open_card_by_key(id_or_key)


def add_card(
    title: str,
    notes: Optional[str] = None,
    key: Optional[str] = None,
    done_when: Optional[dict] = None,
    source_host: Optional[str] = None,
    source_cwd: Optional[str] = None,
    source_session: Optional[str] = None,
) -> Task:
    """File a human-queue card, or update the matching OPEN card if `key`
    is already open — no duplicate, notes replaced, `updated_at` advances
    (`TaskManager.update` always restamps it). A `key` whose only match is a
    DONE card is treated as unclaimed: a fresh open card is created.

    Raises `DoneWhenError` (422) for a malformed `done_when`, or `ValueError`
    (422) for `title`/`notes`/field content that would corrupt the task line,
    or a `key` that doesn't match `^[\\w.:-]+$` — see
    `task_manager._validate_text_fields`. Never touches the calling
    session's own status; this is fire-and-forget, unlike the worker's
    blocking `lifeos_agent_user_ask`.
    """
    if key and not _KEY_RE.match(key):
        raise ValueError(f"key {key!r} must match ^[\\w.:-]+$ (letters, digits, '.', ':', '-', '_')")
    validated_done_when = validate_done_when(done_when)
    fields: dict[str, str] = {}
    if key:
        fields["key"] = key
    if source_host:
        fields["source_host"] = source_host
    if source_cwd:
        fields["source_cwd"] = source_cwd
    if source_session:
        fields["source_session"] = source_session
    if validated_done_when is not None:
        fields["done_when"] = json.dumps(validated_done_when, separators=(",", ":"))

    manager = get_task_manager()
    existing = _find_open_card_by_key(key) if key else None
    if existing is not None:
        update_kwargs: dict[str, Any] = {"notes": notes}
        if fields:
            update_kwargs["fields"] = fields
        task = manager.update(existing.id, **update_kwargs)
        if task is not None:
            return task
        # Rare race: the card was deleted between the lookup above and the
        # update (e.g. an operator deleted it by hand in Obsidian just now).
        # Fall through and file a fresh card rather than raising.

    return manager.create(
        description=title,
        status=STATUS_OPEN,
        tags=[TAG],
        notes=notes,
        fields=fields,
    )


def resolve_card(id_or_key: str, note: Optional[str] = None) -> Optional[Task]:
    """Mark the OPEN human-queue card matched by task id or dedupe key done,
    appending `note` to its notes body. Returns None if no open card matches
    — the route layer maps that to HTTP 404."""
    card = _find_any_card(id_or_key)
    if card is None or card.status != STATUS_OPEN:
        return None

    resolution = f"Resolved: {note}" if note else "Resolved."
    new_notes = f"{card.notes}\n\n{resolution}" if card.notes else resolution
    return get_task_manager().update(card.id, status="done", notes=new_notes)


def _age_hours(task: Task, now: Optional[datetime] = None) -> Optional[float]:
    """Hours since `updated_at` — for a card that has never been touched
    since filing, `TaskManager.create` stamps `updated_at` at creation time,
    so this is the card's age. Refiling an existing open card (dedupe)
    advances `updated_at`, resetting the age clock — intentional: it means
    an agent re-observed the same problem just now."""
    if not task.updated_at:
        return None
    try:
        ts = datetime.fromisoformat(task.updated_at)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return round((now - ts).total_seconds() / 3600.0, 1)


def _card_to_dict(task: Task, now: Optional[datetime] = None) -> dict:
    done_when = None
    raw = task.fields.get("done_when")
    if raw:
        try:
            done_when = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("human_queue: card %s has unparseable done_when field", task.id)
    return {
        "id": task.id,
        "title": task.description,
        "key": task.fields.get("key"),
        "notes": task.notes,
        "age_hours": _age_hours(task, now),
        "source_host": task.fields.get("source_host"),
        "source_cwd": task.fields.get("source_cwd"),
        "source_session": task.fields.get("source_session"),
        "done_when": done_when,
    }


def list_open_cards() -> list[dict]:
    """Open (`blocked`, tag `human`) cards as plain dicts — the shape both
    the REST route and the native chat tool return."""
    manager = get_task_manager()
    now = datetime.now(timezone.utc)
    return [_card_to_dict(t, now) for t in manager.list_tasks(tag=TAG, status=STATUS_OPEN)]


def open_cards_older_than(hours: float = 24) -> list[dict]:
    """Open cards whose age (see `_age_hours`) is at least `hours`."""
    return [c for c in list_open_cards() if c["age_hours"] is not None and c["age_hours"] >= hours]
