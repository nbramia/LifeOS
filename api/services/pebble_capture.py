"""Bounded, local-only filing for finalized Pebble capture results.

The archive is producer-owned quoted data.  This module never edits it and
never feeds its text into a general tool loop: a structured classifier may
select a small plan, and this applicator can only file Inbox tasks,
Scheduler Inbox entries, or a deduplicated human-queue card.  SQLite is a
receipt ledger, not a second content store; Markdown remains authoritative for
the objects it creates.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from croniter import croniter

from api.services.human_queue import add_card
from api.services.agent_board import (
    AGENT_EXECUTOR_TAGS,
    AGENT_PICKUP_TAGS,
    ASSIGNEE_TAGS,
    EXTERNAL_AGENT_ASSIGNEES,
)
from api.services.jev_client import JevClient, JevError, jev_configured
from api.services.jev_task_routing import judge_task
from api.services.journal_filing_policy import PEBBLE_DISPOSITION_CRITERIA, classifier_prompt
from api.services.llm_client import LocalLLMClient, extract_json
from api.services.scheduler_store import SchedulerStore
from api.services.sqlite_connect import connect_closing
from api.services.task_manager import TaskManager
from api.services.time_parser import parse_contextual_time
from config.settings import settings

logger = logging.getLogger(__name__)

_VALID_EXECUTORS = frozenset(AGENT_EXECUTOR_TAGS)
# External agents (duet, muse) claim their own work through MCP; a ring
# capture does not hand work to them.
_VALID_TASK_ASSIGNEES = frozenset(
    t for t in (*ASSIGNEE_TAGS, *AGENT_EXECUTOR_TAGS) if t not in EXTERNAL_AGENT_ASSIGNEES
)
_ROUTING_TAGS = frozenset((*_VALID_TASK_ASSIGNEES, *AGENT_PICKUP_TAGS))
_VALID_KINDS = {"task", "schedule", "human"}
_EFFECT_LEASE_SECONDS = 60
_INLINE_AUTHORITY_RE = re.compile(r"\[\s*\w+\s*::|#[\w-]+")

# Single source of truth for executor aliasing: how a spoken/typed phrase
# resolves to the canonical tag the board and the authority gate use.
# Speech-to-text commonly renders "Claude" as "clod" or "quad" and "Claude
# Code" as "clod code", "quad code", or "cloud code"; "deepseek"/"fireworks" name the configured
# remote provider, which the board tags "cloud" (mirrors the
# `cloud|deepseek|fireworks` group in agent_worker/worker.py) -- never the
# Anthropic API. Every canonical executor tag also maps to itself so a
# lookup never needs a separate identity branch.
EXECUTOR_ALIASES: dict[str, str] = {
    "clod code": "claude",
    "claude code": "claude",
    "cloud code": "claude",
    "clod": "claude",
    "quad code": "claude",
    "quad": "claude",
    "deepseek": "cloud",
    "fireworks": "cloud",
    **{tag: tag for tag in AGENT_EXECUTOR_TAGS},
}

# Alternation tried longest-phrase-first, so a multi-word alias like "cloud
# code" is matched whole before the identity entry for the bare "cloud" tag
# nested inside it could otherwise fire and leave " code" dangling.
# ASCII-only word-boundary/case-folding: every alias and canonical tag is
# plain ASCII, and this keeps an exotic Unicode case fold from producing a
# matched span that isn't literally one of EXECUTOR_ALIASES' keys.
_EXECUTOR_ALIAS_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(phrase) for phrase in sorted(EXECUTOR_ALIASES, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE | re.ASCII,
)


def normalize_executor(text: str) -> Optional[str]:
    """Resolve one executor phrase to its canonical tag.

    Case-insensitive and whole-phrase (the caller passes one candidate
    phrase, not free text -- see `canonicalize_executor_mentions` for
    scanning a larger transcript). Returns None for anything not in
    `EXECUTOR_ALIASES`, including an empty or non-string value.
    """
    if not isinstance(text, str):
        return None
    match = _EXECUTOR_ALIAS_RE.fullmatch(text.strip())
    if not match:
        return None
    return EXECUTOR_ALIASES.get(match.group(0).casefold())


def canonicalize_executor_mentions(text: str) -> str:
    """Replace every alias phrase in free text with its canonical tag.

    Matched longest-phrase-first (see `_EXECUTOR_ALIAS_RE`), so "cloud code"
    collapses whole to "claude" rather than partially matching the bare
    "cloud" tag nested inside it. Text outside an alias phrase, including a
    canonical tag (mapped to itself), is left untouched. Callers that need
    literal transcript evidence (`_is_unquoted_evidence`,
    `_explicit_action_evidence`) must keep operating on the original,
    uncanonicalized text -- evidence is copied verbatim from the transcript.
    """
    if not text:
        return text
    return _EXECUTOR_ALIAS_RE.sub(
        lambda match: EXECUTOR_ALIASES.get(match.group(0).casefold(), match.group(0)), text
    )


class PebbleCaptureError(ValueError):
    """A producer result or constrained plan is malformed or unsafe."""


def _safe_markdown_text(value: Any, *, field: str, limit: int) -> str:
    """Validate model text before it can enter an authoritative Markdown field.

    Titles later share a line with task/schedule metadata, and scheduled agent
    messages can later become task descriptions.  Reject syntax that either
    parser could reinterpret as authority instead of trying to escape it into
    a subtly different operator-visible value.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PebbleCaptureError(f"classifier {field} is unsafe")
    if "<!--" in value or _INLINE_AUTHORITY_RE.search(value):
        raise PebbleCaptureError(f"classifier {field} is unsafe")
    if any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in value):
        raise PebbleCaptureError(f"classifier {field} is unsafe")
    return value.strip()


def _loopback_llm_url(value: Any) -> str:
    """Return a local inference URL or reject a remote-capable setting."""
    if not isinstance(value, str):
        raise PebbleCaptureError("Pebble classifier requires a loopback local LLM URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        # Accessing port also rejects malformed/non-numeric port syntax.
        parsed.port
    except (ValueError, TypeError):
        parsed = None
        host = None
    try:
        literal_loopback = bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        literal_loopback = False
    if (parsed is None or parsed.scheme not in {"http", "https"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or not (host == "localhost" or literal_loopback)):
        raise PebbleCaptureError("Pebble classifier requires a loopback local LLM URL")
    return value


def parse_framed_blocks(document: str | bytes) -> list[dict[str, Any]]:
    """Read only exact v1 producer frames, never the human-readable view.

    The framing's byte count and repeated digest deliberately make a partial
    sync write, a truncated final line, and transcript-shaped Markdown inert.
    Invalid blocks are ignored independently so one damaged historical record
    cannot prevent recovery of a later valid one.
    """
    start_re = re.compile(r"^<!-- pebble-capture-v1 bytes=(\d+) sha256=([0-9a-f]{64}) -->$")
    end_re = re.compile(r"^<!-- /pebble-capture-v1 sha256=([0-9a-f]{64}) -->$")
    # The producer's contract is LF-delimited.  Do not use splitlines(): it
    # treats U+2028/U+2029 inside JSON strings as record boundaries. Scan
    # bytes so an unrelated torn UTF-8 tail cannot hide later valid frames.
    encoded_document = document.encode("utf-8") if isinstance(document, str) else document
    lines = encoded_document.split(b"\n")
    blocks: list[dict[str, Any]] = []
    for index, line in enumerate(lines[:-2]):
        try:
            start_line = line.decode("ascii")
            end_line = lines[index + 2].decode("ascii")
        except UnicodeDecodeError:
            continue
        start = start_re.fullmatch(start_line)
        if not start:
            continue
        raw_json = lines[index + 1]
        end = end_re.fullmatch(end_line)
        if not end:
            continue
        expected_size, expected_digest = int(start.group(1)), start.group(2)
        if expected_size > 131072 or len(raw_json) != expected_size or end.group(1) != expected_digest:
            continue
        if hashlib.sha256(raw_json).hexdigest() != expected_digest:
            continue
        try:
            payload = json.loads(raw_json.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if isinstance(payload, dict) and canonical.encode("utf-8") == raw_json:
            blocks.append(payload)
    return blocks


@dataclass(frozen=True)
class CaptureIdentity:
    source_id: str
    capture_id: str

    @property
    def key(self) -> str:
        raw = json.dumps([self.source_id, self.capture_id], separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class PlannedAction:
    kind: str
    title: str
    index: int
    due_date: str = ""
    schedule_type: str = ""
    schedule_value: str = ""
    timezone: str = ""
    action: str = ""
    executor: str = ""
    message: str = ""
    tags: tuple[str, ...] = ()
    delegation_evidence: str = ""
    action_evidence: str = ""
    human_key: str = ""
    decision_evidence: str = ""
    parent_index: Optional[int] = None
    # Set only when a task's assignee tag was granted on a Jev assignee
    # judgment (see `_jev_task_assignment`): "jev" plus Jev's confidence,
    # and for an agent the probability of Jev's targeted confirmation.
    assignee_source: str = ""
    assignee_confidence: Optional[float] = None
    agent_confirmation: Optional[float] = None

    def operation_key(self, identity: CaptureIdentity) -> str:
        raw = f"{identity.source_id}\0{identity.capture_id}\0{self.index}".encode()
        return f"pebble:{hashlib.sha256(raw).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "title": self.title, "index": self.index,
            "due_date": self.due_date, "schedule_type": self.schedule_type,
            "schedule_value": self.schedule_value, "timezone": self.timezone,
            "action": self.action, "executor": self.executor, "message": self.message,
            "tags": list(self.tags), "delegation_evidence": self.delegation_evidence,
            "action_evidence": self.action_evidence,
            "human_key": self.human_key, "decision_evidence": self.decision_evidence,
            "parent_index": self.parent_index,
            "assignee_source": self.assignee_source,
            "assignee_confidence": self.assignee_confidence,
            "agent_confirmation": self.agent_confirmation,
        }


def _utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise PebbleCaptureError("producer timestamp must include a timezone")
    return dt.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _once_instant(value: str, zone: ZoneInfo) -> datetime:
    """Resolve one local wall time without guessing through DST gaps/overlaps."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        local = parsed.astimezone(zone)
        if (local.replace(tzinfo=None) != parsed.replace(tzinfo=None)
                or local.utcoffset() != parsed.utcoffset()):
            raise PebbleCaptureError("schedule offset is inconsistent with its timezone")
        return parsed.astimezone(timezone.utc)

    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        candidate = parsed.replace(tzinfo=zone, fold=fold)
        instant = candidate.astimezone(timezone.utc)
        round_trip = instant.astimezone(zone)
        if round_trip.replace(tzinfo=None) == parsed:
            candidates[instant] = candidate
    if len(candidates) != 1:
        raise PebbleCaptureError("schedule local time is ambiguous or nonexistent")
    return next(iter(candidates))


def ready_result(payload: dict[str, Any]) -> tuple[CaptureIdentity, str, str]:
    """Validate the small trusted envelope before looking at quoted text."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PebbleCaptureError("unsupported Pebble capture schema")
    reconciliation = payload.get("reconciliation")
    action_eligible = reconciliation.get("action_eligible") if isinstance(reconciliation, dict) else None
    if (payload.get("kind") != "result" or payload.get("status") != "ready"
            or not isinstance(reconciliation, dict) or reconciliation.get("status") != "ready"
            or action_eligible is False
            or ("action_eligible" in reconciliation and not isinstance(action_eligible, bool))):
        raise PebbleCaptureError("only ready result blocks may be classified")
    source = payload.get("source")
    if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
            or not 1 <= len(source["id"]) <= 128
            or source.get("client") is not None and not isinstance(source.get("client"), str)
            or source.get("trigger") is not None and not isinstance(source.get("trigger"), str)):
        raise PebbleCaptureError("ready result has no source id")
    capture_id = payload.get("capture_id")
    revision = payload.get("revision")
    final_text = payload.get("final_text")
    if (not isinstance(capture_id, str) or not 1 <= len(capture_id) <= 128
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
        raise PebbleCaptureError("ready result has no stable capture revision")
    if not isinstance(final_text, str) or not final_text.strip():
        raise PebbleCaptureError("ready result has no final text")
    for required in (
        "received_at_utc", "availability", "provenance", "pebble_text", "audio",
        "whisper_raw", "whisper_polished", "comparison", "models",
    ):
        if required not in payload:
            raise PebbleCaptureError("ready result is missing required producer evidence")
    provenance = payload["provenance"]
    availability = payload["availability"]
    pebble_text = payload["pebble_text"]
    audio = payload["audio"]
    whisper_raw = payload["whisper_raw"]
    whisper_polished = payload["whisper_polished"]
    if (not isinstance(availability, dict)
            or not all(isinstance(availability.get(key), bool) for key in ("audio", "pebble_text"))
            or not isinstance(provenance, dict)
            or provenance.get("receipt") != "durable" or provenance.get("interpretation") != "complete"):
        raise PebbleCaptureError("ready result provenance is not action-safe")
    if (availability["pebble_text"] and (not isinstance(pebble_text, str) or not pebble_text)
            or not availability["pebble_text"] and pebble_text is not None
            or availability["audio"] and not isinstance(audio, dict)
            or not availability["audio"] and audio is not None):
        raise PebbleCaptureError("ready result availability contradicts its evidence")
    if isinstance(audio, dict) and (
        not isinstance(audio.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", audio["sha256"])
        or not isinstance(audio.get("reference"), str)
        or not audio["reference"]
        or not isinstance(audio.get("content_type"), str)
        or not audio["content_type"]
    ):
        raise PebbleCaptureError("ready result audio evidence is malformed")
    raw_stt = provenance.get("raw_stt")
    if (raw_stt not in {None, "durable", "unavailable"}
            or raw_stt == "durable" and not isinstance(whisper_raw, str)
            or raw_stt == "durable" and not availability["audio"]
            or raw_stt == "unavailable" and (
                availability["audio"] or whisper_raw is not None or whisper_polished is not None
            )):
        raise PebbleCaptureError("ready result raw transcript provenance is inconsistent")
    if pebble_text is not None and not isinstance(pebble_text, str):
        raise PebbleCaptureError("ready result transcript evidence is malformed")
    if (whisper_raw is not None and not isinstance(whisper_raw, str)
            or whisper_polished is not None and not isinstance(whisper_polished, str)
            or not isinstance(payload["comparison"], str)
            or not isinstance(payload["models"], dict)
            or audio is not None and not isinstance(audio, dict)):
        raise PebbleCaptureError("ready result reconciliation evidence is malformed")
    try:
        _utc(str(payload.get("recorded_at_utc", "")))
        _utc(str(payload.get("received_at_utc", "")))
    except (TypeError, ValueError) as exc:
        raise PebbleCaptureError("producer timestamp is invalid") from exc
    return CaptureIdentity(source["id"], capture_id), str(revision), final_text


class CaptureLedger:
    """Crash-safe local receipt ledger keyed by source identity and capture id."""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pebble_captures (
                    source_id TEXT NOT NULL, capture_id TEXT NOT NULL,
                    revision TEXT NOT NULL, payload_digest TEXT NOT NULL DEFAULT '', plan_json TEXT, state TEXT NOT NULL,
                    PRIMARY KEY (source_id, capture_id)
                );
                CREATE TABLE IF NOT EXISTS pebble_effects (
                    source_id TEXT NOT NULL, capture_id TEXT NOT NULL, action_index INTEGER NOT NULL,
                    operation_key TEXT NOT NULL, object_kind TEXT, object_id TEXT, state TEXT NOT NULL,
                    claimed_at REAL NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (source_id, capture_id, action_index)
                );
            """)
            # Existing local dry-run ledgers from early development are safe
            # to upgrade in place; SQLite lacks ADD COLUMN IF NOT EXISTS.
            columns = {row[1] for row in db.execute("PRAGMA table_info(pebble_effects)")}
            if "claimed_at" not in columns:
                db.execute("ALTER TABLE pebble_effects ADD COLUMN claimed_at REAL NOT NULL DEFAULT 0")
            if "generation" not in columns:
                db.execute("ALTER TABLE pebble_effects ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
            capture_columns = {row[1] for row in db.execute("PRAGMA table_info(pebble_captures)")}
            if "payload_digest" not in capture_columns:
                db.execute("ALTER TABLE pebble_captures ADD COLUMN payload_digest TEXT NOT NULL DEFAULT ''")

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection, closing it on the way out — see
        `SessionStore._connect` for the same pattern."""
        with connect_closing(self.path, timeout=10, isolation_level=None) as db:
            db.row_factory = sqlite3.Row
            yield db

    def select_plan(self, identity: CaptureIdentity, revision: str, digest: str, plan: list[PlannedAction]) -> str:
        """Store a first plan once; changed final revisions are held, not replayed."""
        encoded = json.dumps([a.to_dict() for a in plan], sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision, payload_digest FROM pebble_captures WHERE source_id=? AND capture_id=?",
                (identity.source_id, identity.capture_id),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO pebble_captures (source_id,capture_id,revision,payload_digest,plan_json,state) VALUES (?, ?, ?, ?, ?, 'planned')",
                    (identity.source_id, identity.capture_id, revision, digest, encoded),
                )
                db.commit()
                return "new"
            db.commit()
            if row["revision"] != revision:
                return "revision_changed"
            return "same" if row["payload_digest"] == digest else "conflict"

    def revision_state(self, identity: CaptureIdentity, revision: str, digest: str) -> Optional[str]:
        """Return ``same``/``revision_changed`` without reopening a plan."""
        with self._connect() as db:
            row = db.execute(
                "SELECT revision, payload_digest FROM pebble_captures WHERE source_id=? AND capture_id=?",
                (identity.source_id, identity.capture_id),
            ).fetchone()
        if row is None:
            return None
        if row["revision"] != revision:
            return "revision_changed"
        return "same" if row["payload_digest"] == digest else "conflict"

    def load_plan(self, identity: CaptureIdentity) -> list[PlannedAction]:
        with self._connect() as db:
            row = db.execute("SELECT plan_json FROM pebble_captures WHERE source_id=? AND capture_id=?", (identity.source_id, identity.capture_id)).fetchone()
        if row is None or not row["plan_json"]:
            return []
        return [PlannedAction(**{**item, "tags": tuple(item.get("tags", []))}) for item in json.loads(row["plan_json"])]

    def effect(self, identity: CaptureIdentity, index: int) -> Optional[sqlite3.Row]:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM pebble_effects WHERE source_id=? AND capture_id=? AND action_index=?",
                (identity.source_id, identity.capture_id, index),
            ).fetchone()

    def claim_effect(
        self, identity: CaptureIdentity, action: PlannedAction,
        *, lease_seconds: int = _EFFECT_LEASE_SECONDS,
    ) -> Optional[int]:
        """Acquire one SQLite-backed effect lease.

        Ordinary stale claims are reconciled by the consumer rather than
        blindly reclaimed: absence from Markdown is ambiguous between a
        pre-commit crash and an operator deletion after commit.  The receipt
        sentinel has no external object, so it alone can be reclaimed.
        """
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state, claimed_at, generation FROM pebble_effects WHERE source_id=? AND capture_id=? AND action_index=?",
                (identity.source_id, identity.capture_id, action.index),
            ).fetchone()
            if row and row["state"] == "applied":
                db.commit()
                return None
            if row:
                if action.index != -1 or now - row["claimed_at"] < lease_seconds:
                    db.commit()
                    return None
            generation = (row["generation"] if row else 0) + 1
            db.execute(
                "INSERT OR REPLACE INTO pebble_effects "
                "(source_id,capture_id,action_index,operation_key,object_kind,object_id,state,claimed_at,generation) "
                "VALUES (?, ?, ?, ?, NULL, NULL, 'applying', ?, ?)",
                (identity.source_id, identity.capture_id, action.index, action.operation_key(identity), now, generation),
            )
            db.commit()
            return generation

    def note_effect_object(
        self, identity: CaptureIdentity, action: PlannedAction,
        kind: str, object_id: str, generation: int,
    ) -> bool:
        """Durably remember the object before the final applied transition."""
        with self._lock, self._connect() as db:
            result = db.execute("""
                UPDATE pebble_effects SET object_kind=?, object_id=?, claimed_at=?
                WHERE source_id=? AND capture_id=? AND action_index=? AND generation=? AND state='applying'
            """, (kind, object_id, time.time(), identity.source_id, identity.capture_id,
                    action.index, generation))
            return result.rowcount == 1

    def clear_uncommitted_claim(
        self, identity: CaptureIdentity, action: PlannedAction, generation: int,
    ) -> None:
        """Release a claim when the store call synchronously failed."""
        with self._lock, self._connect() as db:
            db.execute("""
                DELETE FROM pebble_effects
                WHERE source_id=? AND capture_id=? AND action_index=?
                  AND generation=? AND state='applying' AND object_id IS NULL
            """, (identity.source_id, identity.capture_id, action.index, generation))

    def record_effect(self, identity: CaptureIdentity, action: PlannedAction, kind: str, object_id: str, generation: int) -> bool:
        with self._lock, self._connect() as db:
            result = db.execute("""
                UPDATE pebble_effects SET object_kind=?, object_id=?, state='applied', claimed_at=?
                WHERE source_id=? AND capture_id=? AND action_index=? AND generation=? AND state='applying'
            """, (kind, object_id, time.time(), identity.source_id, identity.capture_id, action.index, generation))
            return result.rowcount == 1

    def all_actions_applied(self, identity: CaptureIdentity, actions: list[PlannedAction]) -> bool:
        with self._connect() as db:
            rows = db.execute(
                "SELECT action_index, state FROM pebble_effects WHERE source_id=? AND capture_id=?",
                (identity.source_id, identity.capture_id),
            ).fetchall()
        states = {row["action_index"]: row["state"] for row in rows}
        return all(states.get(action.index) == "applied" for action in actions)

    def mark_complete(self, identity: CaptureIdentity) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE pebble_captures SET state='complete' WHERE source_id=? AND capture_id=?",
                (identity.source_id, identity.capture_id),
            )


def _mask_quoted(text: str) -> str:
    """Replace quoted spans with spaces while preserving character offsets."""
    pairs = {'"': '"', "“": "”", "‘": "’", "`": "`"}
    chars = list(text)
    index = 0
    while index < len(chars):
        opener = chars[index]
        single_quote = opener == "'" and (index == 0 or not chars[index - 1].isalnum())
        closer = "'" if single_quote else pairs.get(opener)
        if closer is None:
            index += 1
            continue
        end = text.find(closer, index + 1)
        if end < 0:
            index += 1
            continue
        chars[index:end + 1] = " " * (end + 1 - index)
        index = end + 1
    return "".join(chars)


def _positive_clauses(text: str) -> list[str]:
    """Return unquoted clauses without a negated delegation/execution verb."""
    masked = _mask_quoted(text)
    clauses: list[str] = []
    for match in re.finditer(r"[^.!?;\n]+", masked):
        clause = match.group(0)
        # Fail closed for the whole clause. This covers negation after the
        # apparent assignment as well as hypothetical/conditional language;
        # neither is an instruction that may grant execution authority.
        if re.search(
            r"\b(?:do\s+not|don't|never|not|without|except|if|unless|would|"
            r"could|might|maybe|hypothetically)\b",
            clause,
            re.I,
        ):
            continue
        if re.search(
            r"\b(?:according\s+to|per\s+[\w-]+|i\s+(?:heard|remember|recall|wrote)|"
            r"(?:i\s+was|we\s+were)\s+told|"
            r"(?:my\s+)?(?:notes?|reminder)\s+(?:say|says|said|reads?|read)|"
            r"[\w-]+\s+(?:said|says|reported|told\s+me|asked\s+me|instructed\s+me)|"
            r"(?:write|wrote)\s+down)\b",
            clause,
            re.I,
        ):
            continue
        if re.search(r"\b(?:quote|quoted)\b[\s\S]{0,40}\b(?:assign|delegate|route|schedule|run)\b", clause, re.I):
            continue
        clauses.append(clause)
    return clauses


def _explicit_tags(text: str) -> set[str]:
    """Return only positive tag/delegation instructions, never mentions."""
    result: set[str] = set()
    for clause in _positive_clauses(text):
        # Collapse a spoken/typed executor alias ("clod code", "deepseek", ...)
        # to its canonical tag before any tag/assignment regex runs, so an
        # aliased mention is recognized the same as the tag itself.
        clause = canonicalize_executor_mentions(clause)
        # Explicit label instructions may retain ordinary non-routing tags.
        for match in re.finditer(
            r"\btag\s+(?:this|it|that|the\s+task)?\s*(?:as|with)?\s*#([\w-]+)\b",
            clause,
            re.I,
        ):
            tag = match.group(1).lower()
            if tag not in _ROUTING_TAGS:
                result.add(tag)
        assignment_patterns = (
            r"\b(?:assign|delegate|route)\s+[^.!?;\n]{0,100}?\bto\s+#?([\w-]+)\b",
            r"\bask\s+#?([\w-]+)\s+to\s+(?!whether\b)[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
            r"^\s*(?:please\s+)?have\s+#?([\w-]+)\s+(?:to\s+)?[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
            # "let me <verb>" ("let me know", "let me check", ...) is
            # idiomatic, not a self-delegation, even though "me" is itself a
            # valid assignee tag: exclude it rather than let it read as a
            # filing request.
            r"^\s*(?:please\s+)?let\s+(?!me\b)#?([\w-]+)\s+[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
            r"\b#?([\w-]+)\s*(?:,\s*(?:please\s+)?|please\s+)(?!i\b|we\b|they\b|he\b|she\b)[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
        )
        if re.search(r"\bremind\s+me\b", clause, re.I):
            continue
        for pattern in assignment_patterns:
            for match in re.finditer(pattern, clause, re.I):
                tag = match.group(1).lower()
                if tag in _VALID_TASK_ASSIGNEES:
                    result.add(tag)
    return result


_SCOPE_STOP_WORDS = frozenset({
    "a", "an", "and", "as", "at", "do", "for", "handle", "have", "it", "let",
    "please", "route", "run", "synthetic", "take", "task", "that", "the", "this",
    "to", "work", "assign", "delegate", "execute", "ask",
})


def _scope_terms(text: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[\w-]+", text)
        if (len(token) >= 3
            and token.casefold() not in _SCOPE_STOP_WORDS
            and token.casefold() not in _ROUTING_TAGS)
    }


def _single_positive_evidence_clause(evidence: str) -> bool:
    clauses = [part for part in re.split(r"[.!?;\n]+", _mask_quoted(evidence)) if part.strip()]
    return len(clauses) == 1 and len(_positive_clauses(evidence)) == 1


def _explicit_action_evidence(transcript: str, evidence: str, action_evidence: str) -> bool:
    """Bind an executable action to exact, safe source wording.

    The model may paraphrase a display title, so title-token overlap cannot be
    an authority boundary.  Instead executable effects use this literal span
    as their canonical title/message.
    """
    if not isinstance(action_evidence, str):
        return False
    try:
        _safe_markdown_text(action_evidence, field="action_evidence", limit=500)
    except PebbleCaptureError:
        return False
    return (
        bool(_scope_terms(action_evidence))
        and _is_unquoted_evidence(transcript, action_evidence)
        and action_evidence.casefold() in evidence.casefold()
    )


def _explicit_task_delegation(
    transcript: str, executor: str, evidence: str, action_evidence: str
) -> bool:
    """Require action-specific quoted-source proof for one task assignment."""
    return (
        executor in _VALID_TASK_ASSIGNEES
        and _is_unquoted_evidence(transcript, evidence)
        and _single_positive_evidence_clause(evidence)
        and executor in _explicit_tags(transcript)
        and executor in _explicit_tags(evidence)
        and _explicit_action_evidence(transcript, evidence, action_evidence)
    )


# Minimum Jev confidence for an assignee judgment to decide a task's
# assignee; the classifier and `validate_plan` both apply it.
_JEV_ASSIGNEE_FLOOR = 0.7
# Minimum probability of Jev's targeted confirmation that the speaker is
# directly instructing the proposed agent (`JevPebbleClassifier._confirm_agent`).
_JEV_AGENT_CONFIRMATION_FLOOR = 0.8


def _named_executors(text: str) -> set[str]:
    """Canonical executor tags named anywhere in a positive clause.

    A name mention, not a phrasing pattern: any alias in
    `EXECUTOR_ALIASES` counts, wherever it falls in the clause. Negated,
    hypothetical, conditional, quoted, and reported-speech clauses are
    excluded by `_positive_clauses`.
    """
    named: set[str] = set()
    for clause in _positive_clauses(text):
        for match in _EXECUTOR_ALIAS_RE.finditer(clause):
            named.add(EXECUTOR_ALIASES[match.group(0).casefold()])
    return named


def _probability_at_least(value: Any, floor: float) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and floor <= value <= 1)


def _executor_bound_to_title(transcript: str, tag: str, title: str) -> bool:
    """True when `tag` is named in the same sentence as `title`, outside the
    title span itself.

    A literal binding, not a phrasing pattern: the agent's name (any alias
    in `EXECUTOR_ALIASES`) must sit beside the to-do it is said to do. A
    mention in another sentence, or inside the to-do's own wording ("ask
    Taylor about the claude code bill"), does not bind.
    """
    if not title:
        return False
    title_re = re.compile(re.escape(title), re.IGNORECASE)
    for sentence in re.findall(r"[^.!?;\n]+", transcript):
        for match in title_re.finditer(sentence):
            outside = sentence[:match.start()] + " " + sentence[match.end():]
            if any(EXECUTOR_ALIASES[mention.group(0).casefold()] == tag
                   for mention in _EXECUTOR_ALIAS_RE.finditer(outside)):
                return True
    return False


def _agent_locks(transcript: str, tag: str, action_evidence: str, agent_confirmation: Any) -> bool:
    """The two independent locks every Jev-classified agent outcome needs.

    (a) the agent's name bound to the to-do in the transcript
    (`_executor_bound_to_title`, after the cheap `_named_executors`
    positive-clause pre-filter), and (b) Jev's targeted confirmation that
    the speaker themself asks that agent to do it, at
    `_JEV_AGENT_CONFIRMATION_FLOOR` or above.
    """
    return (
        tag in _named_executors(transcript)
        and _executor_bound_to_title(transcript, tag, action_evidence)
        and _probability_at_least(agent_confirmation, _JEV_AGENT_CONFIRMATION_FLOOR)
    )


def _jev_task_assignment(
    transcript: str, tag: str, confidence: Any, action_evidence: str,
    agent_confirmation: Any = None,
) -> bool:
    """Accept an assignee tag backed by a Jev assignee judgment.

    Jev's judgment must meet `_JEV_ASSIGNEE_FLOOR` and the task's title must
    be an exact unquoted transcript span. `me` needs nothing more. An agent
    executor also needs both `_agent_locks`: Jev's assignee judgment alone
    never grants execution authority.
    """
    if not _probability_at_least(confidence, _JEV_ASSIGNEE_FLOOR):
        return False
    if not isinstance(action_evidence, str) or not _scope_terms(action_evidence):
        return False
    try:
        _safe_markdown_text(action_evidence, field="action_evidence", limit=500)
    except PebbleCaptureError:
        return False
    if not _is_unquoted_evidence(transcript, action_evidence):
        return False
    if tag in _VALID_EXECUTORS:
        return _agent_locks(transcript, tag, action_evidence, agent_confirmation)
    return tag == "me"


def _scheduled_executors(text: str) -> set[str]:
    """Resolve positive natural-language scheduled execution requests."""
    result: set[str] = set()
    temporal = re.compile(r"\b(?:schedule|tomorrow|tonight|today|next|every|at\s+\d|on\s+\w|cron)\b", re.I)
    patterns = (
        r"\bschedule\s+#?([\w-]+)\s+(?:to|for)\b",
        r"\b(?:schedule|queue)\s+(?:this|it|that|the\s+(?:task|job))\s+(?:for|with|using)\s+#?([\w-]+)\b",
        r"\b(?:run|execute)\b[\s\S]{0,80}\b(?:with|using)\s+#?([\w-]+)\b",
        r"\bask\s+#?([\w-]+)\s+to\s+(?!whether\b)[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
        r"^\s*(?:please\s+)?have\s+#?([\w-]+)\s+(?:to\s+)?[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
        # See the matching exclusion in `_explicit_tags`: "let me <verb>" is
        # idiomatic, not a delegation request.
        r"^\s*(?:please\s+)?let\s+(?!me\b)#?([\w-]+)\s+[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
        r"\b#?([\w-]+)\s*(?:,\s*(?:please\s+)?|please\s+)(?!i\b|we\b|they\b|he\b|she\b)[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
        r"\b(?:assign|delegate|route)\s+(?:this|it|that|the\s+task)?\s*to\s+#?([\w-]+)\b",
    )
    for clause in _positive_clauses(text):
        # See the matching comment in `_explicit_tags`: aliases must
        # collapse to their canonical tag before the patterns below run.
        clause = canonicalize_executor_mentions(clause)
        if not temporal.search(clause) or re.search(r"\bremind\s+me\b", clause, re.I):
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, clause, re.I):
                executor = match.group(1).lower()
                if executor in _VALID_EXECUTORS:
                    result.add(executor)
    return result


def _is_unquoted_evidence(transcript: str, evidence: str) -> bool:
    if not evidence or len(evidence) > 1000:
        return False
    lowered, target = transcript.casefold(), evidence.casefold()
    masked = _mask_quoted(transcript).casefold()
    start = 0
    while (index := lowered.find(target, start)) >= 0:
        if masked[index:index + len(evidence)] == target:
            return True
        start = index + 1
    return False


def _explicit_scheduled_delegation(
    transcript: str, executor: str, evidence: str, action_evidence: str
) -> bool:
    """Require an actual spoken scheduling request, not a tag discussion."""
    return (
        _is_unquoted_evidence(transcript, evidence)
        and _single_positive_evidence_clause(evidence)
        and executor in _scheduled_executors(transcript)
        and executor in _scheduled_executors(evidence)
        and _explicit_action_evidence(transcript, evidence, action_evidence)
    )


def _explicit_operator_decision(transcript: str, evidence: str) -> bool:
    """Require quoted-source proof that only the operator can unblock work."""
    if not _is_unquoted_evidence(transcript, evidence):
        return False
    return bool(re.search(
        r"\b(?:approve|approval|authorize|authorization|choose|decide|decision|"
        r"credential|password|sign\s+in|log\s+in|which\s+one|should\s+I)\b",
        evidence,
        re.I,
    ))


def validate_plan(
    raw: Iterable[dict[str, Any]], *, transcript: str, recorded_at: str,
    jev_classified: bool,
) -> list[PlannedAction]:
    """Convert untrusted model JSON into an explicitly bounded action list.

    `jev_classified` is required, so every caller states which classifier
    proposed the plan. With it set (a `JevPebbleClassifier` plan), every
    agent-executable outcome -- a delegated task on any path and an agent
    schedule -- also needs both `_agent_locks`; an agent schedule that
    fails its gate files as an unassigned task instead.
    """
    if not isinstance(raw, list) or len(raw) > 8:
        raise PebbleCaptureError("classifier actions must be a list of at most eight items")
    allowed_tags = _explicit_tags(transcript)
    result: list[PlannedAction] = []
    seen_indexes: set[int] = set()
    used_delegations: set[str] = set()
    used_action_evidence: set[str] = set()
    # Read only by the agent-schedule authority gate below. A plain,
    # non-delegated task's evidence consumption must not feed it -- that
    # gate raises on collision, and an independently-valid agent schedule
    # must never be aborted by an unrelated plain task that merely happened
    # to cite the same span first. Only a delegated task (one that actually
    # carries a proven assignee tag) or a filed schedule writes here.
    used_scheduled_delegation_evidence: set[str] = set()
    # Task indexes eligible to be named as a parent by a later child: kept
    # (not dropped for reusing evidence), not themselves a child (hierarchy
    # is one level deep), and not delegated to an agent executor -- a task
    # handed to `#codex`/`#claude`/etc. never joins the hierarchy, as
    # either parent or child; `#me` is a valid assignee but not an executor
    # tag, so a `#me` task may still be a parent. Populated only once an
    # action has fully survived validation and been appended to ``result``,
    # so "earlier in the plan" is enforced structurally: a child can only
    # reference an index already present here, never a later or dropped one.
    available_parents: set[int] = set()
    for raw_action in raw:
        if not isinstance(raw_action, dict):
            raise PebbleCaptureError("classifier action must be an object")
        kind = raw_action.get("kind")
        index = raw_action.get("index")
        title = raw_action.get("title")
        if (kind not in _VALID_KINDS or isinstance(index, bool) or not isinstance(index, int)
                or index < 0 or index in seen_indexes or raw_action.get("ambiguous") is True):
            raise PebbleCaptureError("classifier action kind or index is invalid")
        title = _safe_markdown_text(title, field="title", limit=500)
        seen_indexes.add(index)
        raw_tags = raw_action.get("tags", [])
        if not isinstance(raw_tags, list) or len(raw_tags) > 16:
            raise PebbleCaptureError("task tags must be a bounded list")
        evidence = raw_action.get("delegation_evidence") or ""
        action_evidence = raw_action.get("action_evidence") or ""
        if not isinstance(evidence, str) or not isinstance(action_evidence, str):
            raise PebbleCaptureError("task delegation evidence is invalid")
        assignee_source = raw_action.get("assignee_source") or ""
        assignee_confidence = raw_action.get("assignee_confidence")
        agent_confirmation = raw_action.get("agent_confirmation")
        if assignee_source not in ("", "jev"):
            raise PebbleCaptureError("task assignee source is invalid")
        jev_assigned = False
        jev_agent = False
        tags: list[str] = []
        # A span already spent by an earlier action -- for delegation or for
        # plain filing -- cannot also back this one: one governed span may
        # justify at most one filed action, not several.
        action_key = action_evidence.casefold()
        reused_action_evidence = kind == "task" and bool(action_evidence) and action_key in used_action_evidence
        parent_index: Optional[int] = None
        # Tags are task metadata. A model may redundantly copy an agent
        # schedule's executor into ``tags``; ignore it here so only the
        # schedule-specific authority check below consumes its evidence.
        if kind == "task":
            raw_parent_index = raw_action.get("parent_index")
            if raw_parent_index is not None:
                if (isinstance(raw_parent_index, bool) or not isinstance(raw_parent_index, int)
                        or raw_parent_index == index or raw_parent_index not in available_parents):
                    raise PebbleCaptureError("task parent_index is invalid")
                parent_index = raw_parent_index
            for raw_tag in raw_tags:
                if not isinstance(raw_tag, str):
                    continue
                tag = raw_tag.lstrip("#").lower()
                if tag in _VALID_TASK_ASSIGNEES:
                    evidence_key = evidence.casefold()
                    if assignee_source == "jev":
                        granted = _jev_task_assignment(
                            transcript, tag, assignee_confidence, action_evidence,
                            agent_confirmation,
                        )
                    else:
                        granted = _explicit_task_delegation(
                            transcript, tag, evidence, action_evidence
                        ) and (
                            not jev_classified or tag not in _VALID_EXECUTORS
                            or _agent_locks(transcript, tag, action_evidence, agent_confirmation)
                        )
                    if (not reused_action_evidence
                            and evidence_key not in used_delegations
                            and granted):
                        tags.append(tag)
                        used_delegations.add(evidence_key)
                        jev_assigned = assignee_source == "jev"
                        jev_agent = (jev_classified or jev_assigned) and tag in _VALID_EXECUTORS
                elif tag not in _ROUTING_TAGS and tag in allowed_tags:
                    tags.append(tag)
        normalized_tags = tuple(dict.fromkeys(tags))
        if parent_index is not None and any(tag in _VALID_EXECUTORS for tag in normalized_tags):
            raise PebbleCaptureError("a delegated task cannot join the parent/child hierarchy")
        action = PlannedAction(
            kind=kind, title=title, index=index, tags=normalized_tags,
            delegation_evidence=evidence, action_evidence=action_evidence,
            parent_index=parent_index,
            assignee_source="jev" if jev_assigned else "",
            assignee_confidence=float(assignee_confidence) if jev_assigned else None,
            agent_confirmation=float(agent_confirmation) if jev_agent else None,
        )
        if kind == "task":
            due = raw_action.get("due_date", "")
            if due:
                try:
                    date.fromisoformat(due)
                except (TypeError, ValueError) as exc:
                    raise PebbleCaptureError("task due date is invalid") from exc
            if reused_action_evidence:
                # A span already spent by an earlier action cannot also back
                # this one: dropped, never raised -- the capture must still
                # log and complete. Whether a plain task is filed at all is
                # the classifier's judgment call under the filing policy
                # prompt, not something application code re-decides here.
                continue
            if action_evidence:
                used_action_evidence.add(action_key)
            is_delegated_task = any(tag in _VALID_TASK_ASSIGNEES for tag in normalized_tags)
            if is_delegated_task:
                title = _safe_markdown_text(
                    action_evidence, field="action_evidence", limit=500
                )
                if action_evidence:
                    used_scheduled_delegation_evidence.add(action_key)
            action = replace(action, title=title, due_date=due)
        elif kind == "schedule":
            schedule_type = raw_action.get("schedule_type")
            schedule_value = raw_action.get("schedule_value")
            zone = raw_action.get("timezone") or settings.timezone
            scheduled_action = raw_action.get("action", "notify")
            message = raw_action.get("message") or title
            if (schedule_type not in {"once", "cron"} or not isinstance(schedule_value, str)
                    or not isinstance(zone, str)):
                raise PebbleCaptureError("schedule shape is invalid")
            message = _safe_markdown_text(message, field="message", limit=4000)
            try:
                ZoneInfo(zone)
                if schedule_type == "cron":
                    croniter(schedule_value)
                else:
                    when = _once_instant(schedule_value, ZoneInfo(zone))
                    if when <= max(_utc(recorded_at), _now_utc()):
                        raise PebbleCaptureError("elapsed schedules are held")
            except PebbleCaptureError:
                raise
            except Exception as exc:
                raise PebbleCaptureError("schedule time or timezone is invalid") from exc
            raw_executor = raw_action.get("executor") or ""
            evidence = raw_action.get("delegation_evidence") or ""
            action_evidence = raw_action.get("action_evidence") or ""
            if (not isinstance(raw_executor, str) or not isinstance(evidence, str)
                    or not isinstance(action_evidence, str)):
                raise PebbleCaptureError("schedule delegation fields are invalid")
            executor = raw_executor.lstrip("#").lower()
            if scheduled_action == "agent":
                # The model cannot grant execution: both its claimed evidence
                # and the executor tag must be literally present in the quote.
                evidence_key = evidence.casefold()
                action_key = action_evidence.casefold()
                if (executor not in _VALID_EXECUTORS
                        or evidence_key in used_delegations
                        or action_key in used_scheduled_delegation_evidence
                        or not _explicit_scheduled_delegation(
                            transcript, executor, evidence, action_evidence
                        )
                        or (jev_classified and not _agent_locks(
                            transcript, executor, action_evidence, agent_confirmation
                        ))):
                    if not jev_classified:
                        raise PebbleCaptureError("agent schedule lacks explicit valid delegation")
                    # A Jev plan's unproven agent schedule files as the
                    # speaker's unassigned to-do, never as agent work.
                    task_title = title
                    if action_evidence and action_key not in used_action_evidence:
                        try:
                            task_title = _safe_markdown_text(
                                action_evidence, field="action_evidence", limit=500
                            )
                        except PebbleCaptureError:
                            task_title = title
                        used_action_evidence.add(action_key)
                    result.append(PlannedAction(
                        kind="task", title=task_title, index=index, action_evidence=action_evidence,
                    ))
                    continue
                used_delegations.add(evidence_key)
                used_action_evidence.add(action_key)
                used_scheduled_delegation_evidence.add(action_key)
                title = _safe_markdown_text(
                    action_evidence, field="action_evidence", limit=500
                )
                message = title
            elif scheduled_action != "notify":
                raise PebbleCaptureError("Pebble schedules may only notify or explicitly delegate")
            else:
                # A notify reminder never carries an executor tag. If the
                # classifier sees real scheduled delegation it must select
                # action=agent and pass the independent evidence gate above.
                executor = ""
            action = replace(
                action,
                title=title,
                schedule_type=schedule_type,
                schedule_value=schedule_value,
                timezone=zone,
                action=scheduled_action,
                executor=executor,
                message=message,
                delegation_evidence=evidence,
                action_evidence=action_evidence,
                agent_confirmation=(
                    float(agent_confirmation)
                    if jev_classified and scheduled_action == "agent" else None
                ),
            )
        else:
            key = raw_action.get("human_key") or f"pebble:{index}"
            evidence = raw_action.get("decision_evidence") or ""
            if not isinstance(key, str) or not isinstance(evidence, str) or not _explicit_operator_decision(transcript, evidence):
                raise PebbleCaptureError("human action lacks an explicit operator-only decision")
            action = replace(action, human_key=key, decision_evidence=evidence)
        result.append(action)
        if (kind == "task" and parent_index is None
                and not any(tag in _VALID_EXECUTORS for tag in normalized_tags)):
            available_parents.add(index)
    return result


def _pebble_llm_client() -> LocalLLMClient:
    """Return the configured remote provider, or the local llama-server.

    The remote provider (`settings.remote_llm_configured`) is used, unprobed,
    whenever it's configured -- same construction the `#agent` preflight
    classifier and the `remote` LLM backend already use. A keyless install
    with no remote provider configured falls back to the local llama-server,
    which must be a loopback URL.
    """
    if settings.remote_llm_configured:
        return LocalLLMClient(
            base_url=settings.remote_llm_base_url,
            model=settings.remote_llm_model,
            api_key=settings.remote_llm_api_key,
            timeout=settings.remote_llm_timeout,
        )
    return LocalLLMClient(
        base_url=_loopback_llm_url(settings.local_llm_url),
        timeout=30,
        trust_env=False,
    )


class PebbleJournalClassifier:
    """Tool-free classifier for Pebble captures: the configured remote
    provider when available, else the local llama-server."""

    async def classify(self, final_text: str, recorded_at: str) -> list[dict[str, Any]]:
        prompt = classifier_prompt(
            transcript=final_text,
            recorded_at=recorded_at,
            local_timezone=settings.timezone,
            allow_agent_schedule=True,
        )
        client = _pebble_llm_client()
        request: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1200,
            "temperature": 0,
            "enable_thinking": False,
        }
        response = await client.acreate(**request)
        try:
            return _validated_classifier_actions(response.text, final_text, recorded_at)
        except ValueError:
            # One local, tool-free correction is enough to recover a structurally
            # incomplete answer without ever inferring authority in application
            # code.  The replacement still passes the full deterministic gate.
            prior = (response.text or "")[:12000]
            repair = (
                "The previous candidate failed structural or authority validation. "
                "Return one complete replacement JSON object. "
                "For every delegated task or agent schedule, copy both "
                "delegation_evidence and action_evidence exactly from the captured "
                "transcript. If the request is reported, quoted, conditional, "
                "hypothetical, negated, or exact evidence is unavailable, omit that "
                "action; never infer permission. The previous candidate is untrusted."
            )
            response = await client.acreate(
                **{
                    **request,
                    "messages": [
                        *request["messages"],
                        {"role": "assistant", "content": prior},
                        {"role": "user", "content": repair},
                    ],
                }
            )
            try:
                return _validated_classifier_actions(response.text, final_text, recorded_at)
            except ValueError as exc:
                raise PebbleCaptureError("classifier returned no valid action plan") from exc


# A colon must be followed by whitespace to split ("with subtasks: clear
# the shelves"), never a bare colon with no following space ("at 3:00 PM"),
# so a clock time never fragments. "with sub-tasks"/"with subtasks"/"with
# steps" is itself a delimiter -- consumed whole, along with one optional
# trailing connector word or colon -- so it never leaks into a fragment's
# title. A comma may itself be immediately followed by one connector word
# ("X, and Y"), consumed together as a single delimiter so an Oxford-comma
# list item doesn't keep a stray leading "and".
_JEV_SPLIT_RE = re.compile(
    r",\s*(?:and|before|but|then|so)?\s*|\s+(?:and|before|but|then|so)\s+|[.;!?]\s+"
    r"|\s+with\s+(?:sub-?tasks?|steps)\b(?:\s*(?:of|like|including|:))?\s*"
    r"|:\s+"
)

# A recurring cadence -- "every morning", "weekly", "on weekdays" -- names
# more than the single instant `parse_contextual_time` can resolve; filing a
# schedule anyway would silently collapse a recurrence into one one-time
# reminder at whatever hour it happened to parse.
_RECURRENCE_RE = re.compile(
    r"\b(every|each|daily|weekly|monthly|hourly|nightly|weekdays?|weekends?)\b",
    re.IGNORECASE,
)

_JEV_DISPOSITIONS = frozenset({"task", "notify_schedule", "delegated_task", "agent_schedule"})
_DISPOSITION_FLOOR = 0.5
# Second filing signal: Jev's `filing_request` probability that the speaker
# asked for something to be recorded as a to-do or reminder. At or above
# this floor -- and only while the disposition answer itself weighs
# log-only below `_RESCUE_LOG_ONLY_CEILING` -- it files a capture whose
# disposition answer alone falls short, as a plain to-do.
_FILING_REQUEST_FLOOR = 0.7
_RESCUE_LOG_ONLY_CEILING = 0.5


def _jev_disposition(answers: dict[str, Any]) -> tuple[Optional[str], bool]:
    """The filing disposition to act on (`None` for log-only), and whether
    it came from the `filing_request` rescue.

    A filing disposition at `_DISPOSITION_FLOOR` confidence or above
    decides. Otherwise the capture files as a plain `task` -- never a
    reminder or agent work -- only when `filing_request` reaches
    `_FILING_REQUEST_FLOOR` and the disposition's own log-only probability
    is present and below `_RESCUE_LOG_ONLY_CEILING`.
    """
    disposition = answers.get("disposition") or {}
    confidence = disposition.get("confidence")
    choice = disposition.get("choice")
    if (_probability_at_least(confidence, _DISPOSITION_FLOOR)
            and choice in _JEV_DISPOSITIONS):
        return choice, False
    request = (answers.get("filing_request") or {}).get("noul")
    if not _probability_at_least(request, _FILING_REQUEST_FLOOR):
        return None, False
    probabilities = disposition.get("probabilities")
    log_only = probabilities.get("log_only") if isinstance(probabilities, dict) else None
    if (isinstance(log_only, bool) or not isinstance(log_only, (int, float))
            or not 0 <= log_only < _RESCUE_LOG_ONLY_CEILING):
        return None, False
    return "task", True


def _segment_transcript(text: str) -> list[str]:
    """Split a transcript into small candidate fragments.

    Jev names which fragment was actually asked to be filed rather than
    inventing wording of its own; code owns the segmentation. Falls back to
    the whole stripped transcript on the rare input that leaves nothing
    after stripping every fragment.
    """
    segments: list[str] = []
    pos = 0
    for match in _JEV_SPLIT_RE.finditer(text):
        segments.append(text[pos:match.start()])
        pos = match.end()
    segments.append(text[pos:])
    fragments = [segment.strip(" .") for segment in segments if segment.strip(" .")]
    return fragments or [text.strip(" .")]


_EXECUTOR_BASE_DESCRIPTIONS: dict[str, str] = {
    "claude": "Claude Code, the CLI coding agent.",
    "codex": "The Codex CLI coding agent.",
    "hermes": "The Hermes Telegram gateway.",
    "local": "The local llama-server model running on this machine.",
    "cloud": "The configured remote OpenAI-compatible provider (DeepSeek via "
             "Fireworks) -- never the Anthropic API.",
    "cloud-haiku": "An Anthropic Managed Agent running Claude Haiku.",
    "cloud-sonnet": "An Anthropic Managed Agent running Claude Sonnet.",
}


_EXECUTOR_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "hermes": "Hermes",
    "local": "the local model",
    "cloud": "DeepSeek",
    "cloud-haiku": "Claude Haiku",
    "cloud-sonnet": "Claude Sonnet",
}


def _executor_criteria() -> dict[str, str]:
    """Jev `executor` question options, generated from `EXECUTOR_ALIASES` so
    the alias table stays the only place aliases are defined."""
    aliases_by_tag: dict[str, list[str]] = {}
    for phrase, tag in EXECUTOR_ALIASES.items():
        if phrase != tag:
            aliases_by_tag.setdefault(tag, []).append(phrase)
    criteria: dict[str, str] = {}
    for tag in AGENT_EXECUTOR_TAGS:
        description = _EXECUTOR_BASE_DESCRIPTIONS[tag]
        aliases = aliases_by_tag.get(tag)
        if aliases:
            description += " Commonly transcribed as " + ", ".join(f'"{a}"' for a in aliases) + "."
        criteria[tag] = description
    criteria["none"] = "No AI agent was asked to do anything."
    return criteria


def _assignee_criteria() -> dict[str, str]:
    """Jev `assignee` question options: nobody, the speaker, or each agent."""
    criteria = {
        "none": "Nobody: the speaker did not ask for the task to be assigned to anyone.",
        "me": (
            "The speaker themselves: they asked for the task to be theirs or "
            "claimed it for themselves (\"assign it to me\", \"for me\", "
            "\"that's mine\", \"put it on my list\", \"my to-do\", \"I'll take "
            "this one\", \"I'm doing it myself\")."
        ),
    }
    for tag, description in _executor_criteria().items():
        if tag != "none":
            criteria[tag] = f"The AI agent {tag}: {description}"
    return criteria


# Title candidates for one fragment are its word-boundary cuts: every span
# starting within its first `_TITLE_MAX_LEAD_WORDS` words and ending at most
# `_TITLE_MAX_TRAIL_CUT` words before its end, so a leading filing request
# and a trailing assignee remark can each be cut away. Every candidate is an
# exact slice of the transcript.
_TITLE_MAX_LEAD_WORDS = 12
_TITLE_MAX_TRAIL_CUT = 8
_TITLE_EDGE_CHARS = " \t,.;:!?—–-"
# Fragments past this many get no title question; their titles fall back to
# `_strip_task_request`.
_TITLE_QUESTION_FRAGMENTS = 8
_JEV_TITLE_FLOOR = 0.5


def _title_candidates(fragment: str) -> list[str]:
    """Literal word-boundary spans of `fragment` that could be its title."""
    words = list(re.finditer(r"\S+", fragment))
    candidates: list[str] = []
    seen: set[str] = set()
    for start in range(min(len(words), _TITLE_MAX_LEAD_WORDS)):
        for end in range(len(words), max(start, len(words) - _TITLE_MAX_TRAIL_CUT - 1), -1):
            span = fragment[words[start].start():words[end - 1].end()].strip(_TITLE_EDGE_CHARS)
            if span and _scope_terms(span) and span.casefold() not in seen:
                seen.add(span.casefold())
                candidates.append(span)
    return candidates


# A Pebble note is automatic speech-recognition output, and a misheard
# filing request ("add a task" heard as "at a desk") is the costly
# mishearing. Jev's state carries this beside the note, and the filing
# questions' wording repeats it; titles still come only from literal spans,
# so a misheard request is simply left out of the chosen one.
_JEV_VOICE_NOTE_SOURCE = (
    "automatic speech-recognition transcript of a spoken voice note; it may "
    "contain misheard words, especially in the opening words where a filing "
    "request such as \"add a task\" or \"make a task\" usually sits"
)


def _jev_state(final_text: str) -> dict[str, str]:
    """The state every Pebble Jev call judges: the note and its source."""
    return {"voice_note": final_text, "source": _JEV_VOICE_NOTE_SOURCE}


class JevPebbleClassifier:
    """Pebble classifier backed by TypeSafe's Jev typed-judgment API.

    Code segments the transcript into fragments and asks Jev, in one call,
    which disposition applies, which fragment was requested, which fragment
    is the delegated work, which executor was named, who the speaker asked
    the task to be assigned to, and -- for each fragment -- which literal
    cut of it states the to-do itself (`_title_candidates`). Same `classify()`
    interface as `PebbleJournalClassifier`; `_default_pebble_classifier`
    selects between them via `LIFEOS_PEBBLE_CLASSIFIER`. Every proposed
    action still passes through `validate_plan` unchanged -- this class only
    proposes, it never grants execution authority.
    """

    def __init__(self, client: Optional[JevClient] = None):
        self._client = client or JevClient()
        self.last_answers: dict[str, Any] = {}

    async def classify(self, final_text: str, recorded_at: str) -> list[dict[str, Any]]:
        fragments = _segment_transcript(final_text)
        item_criteria = {f"s{i}": fragment for i, fragment in enumerate(fragments)}
        item_criteria["none"] = "No single fragment -- the whole note is the request"
        work_criteria = {f"s{i}": fragment for i, fragment in enumerate(fragments)}
        parent_criteria = {f"s{i}": fragment for i, fragment in enumerate(fragments)}
        parent_criteria["none"] = "No fragment names a parent to-do or project"
        questions = {
            "disposition": {
                "type": "choice",
                "instructions": (
                    "A voice note was captured from a ring the speaker wears "
                    "and transcribed by automatic speech recognition, which "
                    "can mishear words -- most often the opening filing "
                    "request (\"add a task\" heard as \"at a desk\"). Decide "
                    "what, if anything, the speaker most likely actively "
                    "asked to have filed."
                ),
                "criteria": PEBBLE_DISPOSITION_CRITERIA,
            },
            "item": {
                "type": "choice",
                "instructions": (
                    "Which fragment of the voice note names the item the "
                    "speaker actually asked to have filed (a request misheard "
                    "by speech recognition is still a request)? If several "
                    "things follow a request, only the first item asked for "
                    "counts; the rest is thinking aloud."
                ),
                "criteria": item_criteria,
            },
            "work": {
                "type": "choice",
                "instructions": (
                    "If the speaker asked an AI agent to do something, which "
                    "fragment describes the work itself (not the request "
                    "wording or the agent name)?"
                ),
                "criteria": work_criteria,
            },
            "executor": {
                "type": "choice",
                "instructions": "Which AI agent, if any, did the speaker ask to do the work?",
                "criteria": _executor_criteria(),
            },
            "structure": {
                "type": "choice",
                "instructions": (
                    "Only when the speaker asked for a to-do: did they ask "
                    "for one item, several separate to-dos each explicitly "
                    "requested, or one parent to-do with sub-tasks?"
                ),
                "criteria": {
                    "single": "One item was asked for.",
                    "separate": "Several separate to-dos were each explicitly asked for.",
                    "project": "One parent to-do with sub-tasks was asked for.",
                },
            },
            "parent": {
                "type": "choice",
                "instructions": (
                    "If the speaker asked for a parent to-do with sub-tasks, "
                    "which fragment names the parent or project itself?"
                ),
                "criteria": parent_criteria,
            },
        }
        for i, fragment in enumerate(fragments):
            questions[f"req_s{i}"] = {
                "type": "noul",
                "instructions": (
                    f'"{fragment}" was explicitly asked to be filed as its '
                    "own to-do or sub-task."
                ),
            }
        questions["filing_request"] = {
            "type": "noul",
            "instructions": (
                "The speaker is asking for something to be recorded as a "
                "to-do or reminder -- even when speech recognition misheard "
                "the request wording as similar-sounding words -- not just "
                "thinking aloud, describing, musing, or recounting what "
                "someone else asked."
            ),
        }
        questions["assignee"] = {
            "type": "choice",
            "instructions": (
                "Who did the speaker ask for this task to be assigned to? Only "
                "the speaker's own request counts, including one whose "
                "request wording speech recognition misheard. The speaker "
                "claiming the task for themselves (\"I'll take it\", \"that's "
                "mine\", \"I'm doing it\", \"my to-do\") means the speaker. An "
                "assignment that is negated, hypothetical or wished-for, or "
                "reported as what someone else said, is not a request, and "
                "neither is merely mentioning a person or agent."
            ),
            "criteria": _assignee_criteria(),
        }
        title_options: dict[str, dict[str, str]] = {}
        for i, fragment in enumerate(fragments[:_TITLE_QUESTION_FRAGMENTS]):
            candidates = _title_candidates(fragment)
            if len(candidates) > 1:
                title_options[f"s{i}"] = {f"t{j}": span for j, span in enumerate(candidates)}
                questions[f"title_s{i}"] = {
                    "type": "choice",
                    "instructions": (
                        f'Which wording of "{fragment}" states only the to-do '
                        "itself, as the thing to be done? Leave out any request "
                        "to file or list it, who it is assigned to or who will "
                        "do it, and filler words. Speech recognition can "
                        'mishear the request: in "make a cask to wash the '
                        'windows" the speaker said "make a task to", so the '
                        'to-do is "wash the windows".'
                    ),
                    "criteria": title_options[f"s{i}"],
                }
        try:
            answers = await self._client.aask(_jev_state(final_text), questions)
        except JevError:
            logger.warning("Jev Pebble classification failed; filing log-only")
            return []
        self.last_answers = answers
        try:
            actions = _jev_plan_from_answers(
                answers, final_text, recorded_at, item_criteria, work_criteria, title_options
            )
        except (KeyError, TypeError, AttributeError, ValueError):
            logger.warning("Jev Pebble classification returned a malformed answer; filing log-only")
            return []
        for action in actions:
            agents = [tag for tag in action.get("tags", ()) if tag in _VALID_EXECUTORS]
            if action.get("kind") == "schedule" and action.get("executor") in _VALID_EXECUTORS:
                agents.append(action["executor"])
            if agents:
                confirmation = await self._confirm_agent(final_text, agents[0], action["title"])
                if confirmation is not None:
                    action["agent_confirmation"] = confirmation
        return actions

    async def _confirm_agent(self, final_text: str, agent: str, title: str) -> Optional[float]:
        """Jev's probability that the speaker is directly instructing
        `agent` to do `title` -- a second, targeted call made only when the
        first proposes an agent. `None` when the call fails or the answer is
        malformed, which `validate_plan` treats as unconfirmed."""
        statement = (
            f"In this voice note the speaker themself asks for this to be done by "
            f"{_EXECUTOR_DISPLAY_NAMES.get(agent, agent)} -- telling it to do it, or "
            f"assigning or handing the task to it: '{title}' (not negating it, not "
            "imagining it, not reporting what someone else said; speech "
            "recognition may have misheard the request wording around it)"
        )
        try:
            answers = await self._client.aask(
                _jev_state(final_text),
                {"agent_instructed": {"type": "noul", "instructions": statement}},
            )
            noul = answers["agent_instructed"]["noul"]
        except (JevError, KeyError, TypeError):
            logger.warning("Jev Pebble agent confirmation failed; filing unassigned")
            return None
        if isinstance(noul, bool) or not isinstance(noul, (int, float)):
            return None
        return float(noul)


# The spoken filing request in front of a task ("make a task to", "add a
# to-do for", "make a project to", "make tasks to", ...) -- wording about
# the task, not part of it.
_TASK_REQUEST_PREFIX_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+)?(?:make|create|add|file|open|put\s+in|set\s+up|start)\s+"
    r"(?:me\s+)?(?:a\s+|an\s+)?(?:new\s+)?(?:tasks?|to-?dos?|todos?|reminders?|projects?)\s+"
    r"(?:to|for|about|that\s+(?:i\s+)?(?:need\s+to|should)?)\s*",
    re.IGNORECASE,
)

# A leading "subtasks"/"sub-tasks" marker left in front of a child fragment
# ("Sub-tasks: clear the shelves") -- wording about the sub-task, not part
# of it. Segmentation (`_JEV_SPLIT_RE`) already consumes this phrase when it
# falls on a fragment boundary; this covers a fragment that still starts
# with it.
_LEADING_SUBTASK_RE = re.compile(r"^\s*sub-?tasks?\s*:?\s*", re.IGNORECASE)


def _strip_task_request(item: str) -> str:
    """Drop the filing request from a task fragment, keeping the task itself.

    The result is still a literal span of the transcript (only its first
    letter is capitalized, and evidence checks are case-insensitive).
    Returns the fragment unchanged when nothing meaningful would remain.
    """
    working = _TASK_REQUEST_PREFIX_RE.sub("", item, count=1)
    working = _LEADING_SUBTASK_RE.sub("", working, count=1)
    stripped = working.strip()
    if stripped == item.strip() or not _scope_terms(stripped):
        return item
    return stripped[0].upper() + stripped[1:]


def _jev_self_assignment(item: str, final_text: str) -> Optional[tuple[list[str], str]]:
    """Return (["me"], sentence) when the speaker explicitly self-assigned
    `item` ("... and assign it to me"), else `None`.

    The fallback when Jev's assignee judgment is missing or unusable. The
    evidence is the one sentence holding both the item and the assignment;
    `validate_plan` re-proves it before the tag survives.
    """
    for match in re.finditer(r"[^.!?;\n]+", final_text):
        sentence = match.group(0).strip()
        if item.casefold() in sentence.casefold() and "me" in _explicit_tags(sentence):
            return ["me"], sentence
    return None


def _capitalized(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


def _jev_assignee(answers: dict[str, Any]) -> Optional[tuple[str, float]]:
    """Jev's assignee judgment as `(choice, confidence)`.

    `None` -- meaning "fall back to the transcript-based assignment" -- when
    the answer is missing, malformed, not a known choice, or below
    `_JEV_ASSIGNEE_FLOOR`.
    """
    answer = answers.get("assignee")
    if not isinstance(answer, dict):
        return None
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    if (choice not in ("none", *_VALID_TASK_ASSIGNEES)
            or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not _JEV_ASSIGNEE_FLOOR <= confidence <= 1):
        return None
    return choice, float(confidence)


def _jev_title(
    answers: dict[str, Any], key: Optional[str], title_options: dict[str, dict[str, str]],
) -> Optional[str]:
    """Jev's chosen title candidate for fragment `key`, capitalized, or
    `None` when there was no title question for it or the answer is
    missing, malformed, or below `_JEV_TITLE_FLOOR`."""
    options = title_options.get(key) if key else None
    if not options:
        return None
    answer = answers.get(f"title_{key}")
    if not isinstance(answer, dict):
        return None
    choice = answer.get("choice")
    span = options.get(choice) if isinstance(choice, str) else None
    confidence = answer.get("confidence")
    if (span is None or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float)) or confidence < _JEV_TITLE_FLOOR):
        return None
    return _capitalized(span)


def _jev_self_assignment_fields(
    item: str, final_text: str, assignee: Optional[tuple[str, float]],
) -> dict[str, Any]:
    """Raw-action fields assigning a plain task to the speaker, if any.

    A confident Jev assignee judgment decides: `me` tags the task, recording
    Jev as the assignment's source, and any other choice leaves it
    unassigned. Without one, the transcript's explicit "assign it to me"
    wording (`_jev_self_assignment`) decides.
    """
    if assignee is not None:
        choice, confidence = assignee
        if choice == "me":
            return {"tags": ["me"], "assignee_source": "jev", "assignee_confidence": confidence}
        return {}
    found = _jev_self_assignment(item, final_text)
    if found is None:
        return {}
    tags, sentence = found
    return {"tags": tags, "delegation_evidence": sentence}


def _jev_plain_task(
    item: str, final_text: str, *, title: Optional[str] = None,
    assignee: Optional[tuple[str, float]] = None,
) -> dict[str, Any]:
    """A task for the speaker titled with Jev's chosen title, or else
    without the spoken filing request, carrying `#me` when they assigned it
    to themselves (see `_jev_self_assignment_fields`).
    """
    title = title or _strip_task_request(item)
    action: dict[str, Any] = {"kind": "task", "index": 0, "title": title, "action_evidence": title}
    action.update(_jev_self_assignment_fields(item, final_text, assignee))
    return action


def _jev_agent_task(
    work: str, final_text: str, executor: str, *, title: Optional[str],
    assignee_confidence: Optional[float],
) -> dict[str, Any]:
    """A task delegated to `executor`, titled with Jev's chosen title or
    else the work fragment.

    With `assignee_confidence` the executor came from Jev's assignee
    judgment and `validate_plan` checks it against `_jev_task_assignment`;
    without it, it came from the `executor` question and faces the literal
    delegation-evidence gate with the whole transcript as evidence.
    """
    title = title or work
    action: dict[str, Any] = {
        "kind": "task", "index": 0, "title": title, "action_evidence": title, "tags": [executor],
    }
    if assignee_confidence is None:
        action["delegation_evidence"] = final_text.strip(" .")
    else:
        action.update(assignee_source="jev", assignee_confidence=assignee_confidence)
    return action


# Named confidence floors for the multi-item Jev shapes (see
# `_jev_structured_tasks`): below either, the request is treated as if
# Jev hadn't offered that shape at all.
_STRUCTURE_CONFIDENCE_FLOOR = 0.5
_REQUESTED_FRAGMENT_FLOOR = 0.7


def _requested_fragment_indexes(answers: dict[str, Any], item_criteria: dict[str, str]) -> list[int]:
    """Fragment indexes, in transcript order, whose `req_s<i>` answer meets
    `_REQUESTED_FRAGMENT_FLOOR` for "explicitly asked to be filed"."""
    indexes = []
    for key in item_criteria:
        if key == "none":
            continue
        req = answers.get(f"req_{key}") or {}
        noul = req.get("noul")
        if isinstance(noul, (int, float)) and noul >= _REQUESTED_FRAGMENT_FLOOR:
            indexes.append(int(key[1:]))
    return sorted(indexes)


def _jev_multi_task_action(
    item: str, final_text: str, *, index: int, allow_me: bool, parent_index: Optional[int] = None,
    title: Optional[str] = None, assignee: Optional[tuple[str, float]] = None,
) -> dict[str, Any]:
    """One task action for a list or project item.

    Jev's chosen title, or else a literal transcript span with its filing
    request stripped, its first letter capitalized regardless of whether
    anything was stripped (a mid-sentence fragment like "order the
    synthetic filters" otherwise keeps the original text's lowercase leading
    letter). Only the first action in a list, or the parent in a project,
    may carry `#me` -- `allow_me=False` skips self-assignment entirely for
    every other item.
    """
    title = title or _capitalized(_strip_task_request(item))
    action: dict[str, Any] = {"kind": "task", "index": index, "title": title, "action_evidence": title}
    if parent_index is not None:
        action["parent_index"] = parent_index
    if allow_me:
        action.update(_jev_self_assignment_fields(item, final_text, assignee))
    return action


# validate_plan's own cap; the parent (when there is one) is always kept, so
# a project's children are capped one lower.
_MAX_PLAN_ACTIONS = 8


def _jev_structured_tasks(
    answers: dict[str, Any], final_text: str, item_criteria: dict[str, str],
    title_options: dict[str, dict[str, str]], assignee: Optional[tuple[str, float]],
) -> list[dict[str, Any]]:
    """Turn a `task`-disposition Jev answer into one, several, or a
    parent-plus-children set of raw task actions, per the `structure`
    answer.

    Falls back to the single-task shape (`_jev_plain_task`) when `structure`
    is missing, below `_STRUCTURE_CONFIDENCE_FLOOR`, or not a recognized
    choice -- this is the strong default. A `project` answer whose named
    parent isn't itself a requested fragment falls back to `separate`.
    """
    fragments = {key: value for key, value in item_criteria.items() if key != "none"}
    structure_answer = answers.get("structure") or {}
    structure_confidence = structure_answer.get("confidence")
    structure_choice = structure_answer.get("choice")
    if (not isinstance(structure_confidence, (int, float))
            or structure_confidence < _STRUCTURE_CONFIDENCE_FLOOR
            or structure_choice not in ("separate", "project")):
        structure_choice = "single"

    item_choice = (answers.get("item") or {}).get("choice")
    single_item = (
        item_criteria.get(item_choice) if item_choice and item_choice != "none" else final_text.strip(" .")
    )
    if structure_choice == "single":
        return [_jev_plain_task(
            single_item, final_text,
            title=_jev_title(answers, item_choice, title_options), assignee=assignee,
        )]

    requested = _requested_fragment_indexes(answers, item_criteria)
    requested_items = [(i, fragments[f"s{i}"]) for i in requested]

    def task(i: int, item: str, **kwargs: Any) -> dict[str, Any]:
        return _jev_multi_task_action(
            item, final_text, title=_jev_title(answers, f"s{i}", title_options),
            assignee=assignee, **kwargs,
        )

    if structure_choice == "project":
        parent_choice = (answers.get("parent") or {}).get("choice")
        parent_i: Optional[int] = None
        if parent_choice and parent_choice != "none":
            try:
                candidate = int(parent_choice[1:])
            except (TypeError, ValueError, IndexError):
                candidate = None
            if candidate is not None and any(i == candidate for i, _ in requested_items):
                parent_i = candidate
        if parent_i is not None:
            children = [(i, frag) for i, frag in requested_items if i != parent_i][: _MAX_PLAN_ACTIONS - 1]
            actions = [task(parent_i, fragments[f"s{parent_i}"], index=0, allow_me=True)]
            for offset, (child_i, child_item) in enumerate(children, start=1):
                actions.append(task(child_i, child_item, index=offset, allow_me=False, parent_index=0))
            return actions
        # No valid parent named: fall back to filing the requested fragments
        # as separate to-dos rather than dropping the request entirely.

    if not requested_items:
        return []
    limited = requested_items[:_MAX_PLAN_ACTIONS]
    return [
        task(i, frag_item, index=position, allow_me=(position == 0))
        for position, (i, frag_item) in enumerate(limited)
    ]


def _jev_plan_from_answers(
    answers: dict[str, Any], final_text: str, recorded_at: str,
    item_criteria: dict[str, str], work_criteria: dict[str, str],
    title_options: Optional[dict[str, dict[str, str]]] = None,
) -> list[dict[str, Any]]:
    """Turn one Jev answers dict into a raw action list.

    Log-only (an empty list) when `_jev_disposition` finds no filing
    disposition, for a recurring cadence a single-instant
    time parse can't represent, and for a delegation with no recognized
    executor -- never a silently reassigned or invented action. Raises
    `KeyError`/`TypeError`/`AttributeError`/`ValueError` on a malformed
    answers shape; the caller treats that the same as a failed Jev call.

    A confident assignee judgment (`_jev_assignee`) decides who a task is
    for: an agent turns a to-do into a delegated task, the speaker tags it
    `#me`, and nobody leaves it unassigned (or log-only, for a delegation).
    A missing or unusable one falls back to the `executor` answer and the
    transcript's explicit self-assignment wording. Titles are Jev's chosen
    literal cut of the fragment (`_jev_title`) when confident, else the
    fragment with its filing request stripped.
    """
    title_options = title_options or {}
    disp, rescued = _jev_disposition(answers)
    if disp is None:
        return []

    item_choice = (answers.get("item") or {}).get("choice")
    item = item_criteria.get(item_choice) if item_choice and item_choice != "none" else final_text.strip(" .")
    work_choice = (answers.get("work") or {}).get("choice")
    work = work_criteria.get(work_choice)
    work_key = work_choice if work else item_choice
    work = work or item
    executor_choice = (answers.get("executor") or {}).get("choice")
    executor = executor_choice if executor_choice in AGENT_EXECUTOR_TAGS else None
    assignee = _jev_assignee(answers)
    assignee_confidence: Optional[float] = None
    delegation_evidence = final_text.strip(" .")

    if rescued:
        # A rescued capture is only ever the speaker's own or an unassigned
        # to-do: an agent assignee judgment is disregarded.
        if assignee is not None and assignee[0] in _VALID_EXECUTORS:
            assignee = ("none", assignee[1])
        return _jev_structured_tasks(answers, final_text, item_criteria, title_options, assignee)

    if disp == "task":
        if assignee is not None and assignee[0] in _VALID_EXECUTORS:
            return [_jev_agent_task(
                _strip_task_request(item), final_text, assignee[0],
                title=_jev_title(answers, item_choice, title_options),
                assignee_confidence=assignee[1],
            )]
        return _jev_structured_tasks(answers, final_text, item_criteria, title_options, assignee)

    if disp in ("notify_schedule", "agent_schedule") and _RECURRENCE_RE.search(final_text):
        return []

    if disp == "notify_schedule":
        recorded_local = _utc(recorded_at).astimezone(ZoneInfo(settings.timezone))
        when = parse_contextual_time(final_text, recorded_local)
        if when is None:
            return [_jev_plain_task(
                item, final_text, title=_jev_title(answers, item_choice, title_options),
                assignee=assignee,
            )]
        reminder = _jev_title(answers, item_choice, title_options) or item
        return [{
            "kind": "schedule", "index": 0, "title": reminder, "schedule_type": "once",
            "schedule_value": when.isoformat(), "timezone": settings.timezone,
            "action": "notify", "message": reminder,
        }]

    # disp is "delegated_task" or "agent_schedule".
    if assignee is not None:
        choice, assignee_confidence = assignee
        # Jev read a delegation yet judged that nobody -- or the speaker --
        # was assigned: the filing request stands as the speaker's own
        # to-do. With no agent named at all it stays log-only, like any
        # delegation without an executor.
        if choice == "me" or (choice == "none" and executor is not None):
            return [_jev_plain_task(
                item, final_text, title=_jev_title(answers, item_choice, title_options),
                assignee=assignee,
            )]
        executor = choice if choice in _VALID_EXECUTORS else None
    if executor is None:
        return []
    title = _jev_title(answers, work_key, title_options)

    if disp == "delegated_task":
        return [_jev_agent_task(
            work, final_text, executor, title=title, assignee_confidence=assignee_confidence,
        )]

    # disp == "agent_schedule"
    recorded_local = _utc(recorded_at).astimezone(ZoneInfo(settings.timezone))
    when = parse_contextual_time(final_text, recorded_local)
    if when is None:
        return [_jev_agent_task(
            work, final_text, executor, title=title, assignee_confidence=assignee_confidence,
        )]
    work = title or work
    return [{
        "kind": "schedule", "index": 0, "title": work, "schedule_type": "once",
        "schedule_value": when.isoformat(), "timezone": settings.timezone,
        "action": "agent", "executor": executor, "message": work,
        "delegation_evidence": delegation_evidence, "action_evidence": work,
    }]


def _default_pebble_classifier() -> Any:
    """Select the Pebble classifier per `LIFEOS_PEBBLE_CLASSIFIER`.

    'jev' without a configured key falls back to the LLM classifier with a
    warning rather than failing captures outright.
    """
    if settings.pebble_classifier == "jev":
        if jev_configured():
            return JevPebbleClassifier()
        logger.warning(
            "LIFEOS_PEBBLE_CLASSIFIER=jev but no TypeSafe API key is configured; "
            "falling back to the LLM Pebble classifier"
        )
    return PebbleJournalClassifier()


def _validated_classifier_actions(
    response_text: Any, final_text: str, recorded_at: str
) -> list[dict[str, Any]]:
    """Parse one model candidate and prove it passes the authority gate."""
    if not isinstance(response_text, str):
        raise PebbleCaptureError("classifier returned no action list")
    parsed = extract_json(response_text)
    if not isinstance(parsed, dict):
        raise PebbleCaptureError("classifier returned no action list")
    actions = parsed.get("actions")
    if not isinstance(actions, list):
        raise PebbleCaptureError("classifier returned no action list")
    # A Jev-sourced assignment is only ever proposed by `JevPebbleClassifier`;
    # a generative model's claim to one is discarded so its tags face the
    # literal delegation-evidence gate.
    for raw_action in actions:
        if isinstance(raw_action, dict):
            raw_action.pop("assignee_source", None)
            raw_action.pop("assignee_confidence", None)
            raw_action.pop("agent_confirmation", None)
    validated = validate_plan(
        actions, transcript=final_text, recorded_at=recorded_at, jev_classified=False,
    )
    # ``validate_plan`` can drop a task whose action_evidence duplicates an
    # earlier action's, so a raw action's position in ``actions`` is not
    # reliably aligned with its validated counterpart's position in
    # ``validated``.  Pair by the model-assigned ``index`` instead of by
    # list position.
    validated_by_index = {action.index: action for action in validated}
    for raw_action in actions:
        if not isinstance(raw_action, dict) or raw_action.get("kind") != "task":
            continue
        action = validated_by_index.get(raw_action.get("index"))
        if action is None:
            # Dropped for reusing an action_evidence span an earlier action
            # already spent: one governed span backs at most one filed
            # action.
            continue
        claimed_assignees = {
            value.lstrip("#").lower()
            for value in raw_action.get("tags", [])
            if isinstance(value, str)
            and value.lstrip("#").lower() in _VALID_TASK_ASSIGNEES
        }
        if claimed_assignees.difference(action.tags):
            # ``validate_plan`` deliberately strips an unsupported routing tag
            # for direct callers. The classifier must instead correct or omit
            # the entire proposed effect: reported speech is log-only, not an
            # unassigned task derived from somebody else's instruction.
            raise PebbleCaptureError("classifier proposed unauthorized task delegation")
    return actions


class PebbleCaptureConsumer:
    """Classify then apply finalized result payloads through canonical stores only."""

    def __init__(
        self, ledger: CaptureLedger, task_manager: TaskManager, scheduler_store: SchedulerStore,
        classifier: Optional[Any] = None, *, apply: bool = False,
    ):
        self.ledger = ledger
        self.task_manager = task_manager
        self.scheduler_store = scheduler_store
        self.classifier = classifier or _default_pebble_classifier()
        self.apply = apply

    def _software_project_fields(self, title: str, tags: list[str]) -> Optional[dict[str, str]]:
        """Append the `software` tag (in place, on `tags`) and return a
        `{"project": ...}` fields dict when Jev judges a filed task's title
        as software work with high confidence.

        Only asks when the Jev Pebble classifier is actually in use --
        this never adds a Jev call to the `llm` classifier's path. Any
        failure (Jev unconfigured, the call raising, a malformed judgment)
        leaves `tags` untouched and returns no fields; it never blocks
        filing the task itself.
        """
        if not jev_configured() or not isinstance(self.classifier, JevPebbleClassifier):
            return None
        try:
            judgment = judge_task(title)
            if judgment is None:
                return None
            software_work = judgment.software_work
            if (
                software_work is None
                or software_work.noul is None
                or software_work.noul < 0.7
            ):
                return None
            if "software" not in tags:
                tags.append("software")
            location = judgment.location
            if (
                location is not None
                and isinstance(location.choice, str)
                and location.confidence >= 0.6
                and location.choice not in {"vault", "home"}
                and not any(bad in location.choice for bad in ("\n", "\r", "]", "<!--"))
            ):
                return {"project": location.choice}
            return None
        except Exception as exc:  # noqa: BLE001 - a judgment failure must never block filing
            logger.warning(
                "Pebble software/project judgment failed: %s", type(exc).__name__
            )
            return None

    def _find_effect_object(
        self, action: PlannedAction, operation_key: str, object_id: Optional[str]
    ) -> Optional[Any]:
        """Reconcile a stale claim from Markdown without creating anything."""
        if action.kind == "task":
            self.task_manager.rebuild_index()
            return ((self.task_manager.get(object_id) if object_id else None)
                    or self.task_manager.find_by_operation(operation_key))
        if action.kind == "schedule":
            self.scheduler_store.rebuild_index()
            return ((self.scheduler_store.get(object_id) if object_id else None)
                    or self.scheduler_store.find_by_operation(operation_key))
        if object_id:
            self.task_manager.rebuild_index()
            return self.task_manager.get(object_id)
        return None

    async def process(self, payload: dict[str, Any]) -> str:
        identity, revision, final_text = ready_result(payload)
        recorded_at = str(payload["recorded_at_utc"])
        # A completed replay never invokes local inference again.
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        revision_state = self.ledger.revision_state(identity, revision, digest)
        if revision_state in {"revision_changed", "conflict"}:
            return revision_state
        existing = self.ledger.effect(identity, -1)
        if existing and existing["state"] == "applied":
            return "complete"
        if revision_state == "same":
            actions = self.ledger.load_plan(identity)
        else:
            raw_actions = await self.classifier.classify(final_text, recorded_at)
            actions = validate_plan(
                raw_actions, transcript=final_text, recorded_at=recorded_at,
                jev_classified=isinstance(self.classifier, JevPebbleClassifier),
            )
        selected = self.ledger.select_plan(identity, revision, digest, actions)
        if selected in {"revision_changed", "conflict"}:
            return selected
        # A concurrent classifier may have won select_plan. Effects always
        # consume the stored decision, never this invocation's fresh proposal.
        actions = self.ledger.load_plan(identity)
        if not self.apply:
            return "dry_run"
        # A dry-run plan may sit in the ledger until after its one-time
        # reminder has elapsed.  Re-check persisted plans immediately before
        # effects so replay cannot create a permanently dead schedule and
        # falsely mark the capture complete.
        for action in actions:
            if (action.kind == "schedule" and action.schedule_type == "once"
                    and _once_instant(
                        action.schedule_value, ZoneInfo(action.timezone)
                    ) <= _now_utc()):
                return "schedule_elapsed"
        for action in actions:
            parent_object_id: Optional[str] = None
            if action.kind == "task" and action.parent_index is not None:
                parent_effect = self.ledger.effect(identity, action.parent_index)
                if not parent_effect or parent_effect["state"] != "applied" or not parent_effect["object_id"]:
                    # The parent isn't applied yet -- claimed by another
                    # worker within its lease, or not yet reached in this
                    # pass. A child cannot attach to a task that doesn't
                    # exist yet, so hold without creating it.
                    return "in_progress"
                parent_object_id = parent_effect["object_id"]
            existing_claim = self.ledger.effect(identity, action.index)
            if existing_claim and existing_claim["state"] == "applying":
                if time.time() - existing_claim["claimed_at"] < _EFFECT_LEASE_SECONDS:
                    continue
                key = action.operation_key(identity)
                recovered = self._find_effect_object(
                    action, key, existing_claim["object_id"]
                )
                if recovered is None:
                    # The object may have been deleted by the operator after
                    # a Markdown commit but before the ledger acknowledgement.
                    # Recreating it would undo that edit, so hold for review.
                    return "ambiguous_outcome"
                self.ledger.record_effect(
                    identity, action, action.kind, recovered.id,
                    existing_claim["generation"],
                )
                continue
            generation = self.ledger.claim_effect(identity, action)
            if generation is None:
                continue
            key = action.operation_key(identity)
            if action.kind == "task":
                tags = list(action.tags)
                fields = self._software_project_fields(action.title, tags)
                if parent_object_id is not None:
                    fields = {**(fields or {}), "parent_id": parent_object_id}
                try:
                    task, _ = self.task_manager.create_or_find_by_operation(
                        key, description=action.title, due_date=action.due_date or None,
                        tags=tags, fields=fields,
                    )
                except Exception:
                    self.ledger.clear_uncommitted_claim(identity, action, generation)
                    raise
                self.ledger.note_effect_object(identity, action, "task", task.id, generation)
                self.ledger.record_effect(identity, action, "task", task.id, generation)
            elif action.kind == "schedule":
                try:
                    entry, _ = self.scheduler_store.create_or_find_by_operation(
                        key, name=action.title, schedule_type=action.schedule_type,
                        schedule_value=action.schedule_value, action=action.action,
                        executor=action.executor, timezone=action.timezone,
                        message_type="static", message_content=action.message,
                    )
                except Exception:
                    self.ledger.clear_uncommitted_claim(identity, action, generation)
                    raise
                self.ledger.note_effect_object(
                    identity, action, "schedule", entry.id, generation
                )
                self.ledger.record_effect(identity, action, "schedule", entry.id, generation)
            else:
                try:
                    card = add_card(
                        action.title,
                        notes="Answer or resolve this operator-only decision filed from a Pebble capture.",
                        key=key,
                        _log_content=False,
                    )
                except Exception:
                    self.ledger.clear_uncommitted_claim(identity, action, generation)
                    raise
                self.ledger.note_effect_object(identity, action, "human", card.id, generation)
                self.ledger.record_effect(identity, action, "human", card.id, generation)
        if not self.ledger.all_actions_applied(identity, actions):
            return "in_progress"
        receipt = PlannedAction("task", "receipt", -1)
        generation = self.ledger.claim_effect(identity, receipt)
        if generation is not None:
            if self.ledger.record_effect(identity, receipt, "receipt", "complete", generation):
                self.ledger.mark_complete(identity)
        completed = self.ledger.effect(identity, -1)
        return "complete" if completed and completed["state"] == "applied" else "in_progress"


def process_sync(consumer: PebbleCaptureConsumer, payload: dict[str, Any]) -> str:
    """Synchronous watcher entry point; a failed local model leaves it pending."""
    return asyncio.run(consumer.process(payload))
