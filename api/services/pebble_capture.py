"""Bounded, local-only filing for finalized Pebble capture results.

The archive is producer-owned quoted data.  This module never edits it and
never feeds its text into a general tool loop: a local structured classifier
may select a small plan, and this applicator can only file Inbox tasks,
Scheduler Inbox entries, or a deduplicated human-queue card.  SQLite is a
receipt ledger, not a second content store; Markdown remains authoritative for
the objects it creates.
"""
from __future__ import annotations

import asyncio
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
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from croniter import croniter

from api.services.human_queue import add_card
from api.services.agent_board import AGENT_EXECUTOR_TAGS, AGENT_PICKUP_TAGS, ASSIGNEE_TAGS
from api.services.journal_filing_policy import classifier_prompt
from api.services.llm_client import LocalLLMClient, extract_json
from api.services.scheduler_store import SchedulerStore
from api.services.task_manager import TaskManager
from config.settings import settings

logger = logging.getLogger(__name__)

_VALID_EXECUTORS = frozenset(AGENT_EXECUTOR_TAGS)
_VALID_TASK_ASSIGNEES = frozenset((*ASSIGNEE_TAGS, *AGENT_EXECUTOR_TAGS))
_ROUTING_TAGS = frozenset((*_VALID_TASK_ASSIGNEES, *AGENT_PICKUP_TAGS))
_VALID_KINDS = {"task", "schedule", "human"}
_EFFECT_LEASE_SECONDS = 60
_INLINE_AUTHORITY_RE = re.compile(r"\[\s*\w+\s*::|#[\w-]+")


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

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

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
            r"^\s*(?:(?:yesterday|today|earlier)\s+)?(?:"
            r"i\s+(?:heard|remember|recall|wrote)|remember\b|"
            r"(?:my\s+)?(?:notes?|reminder)\s+(?:say|says|said|reads?|read)|"
            r"[\w-]+\s+(?:said|says|reported|told\s+me)|(?:write|wrote)\s+down\b)",
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
            r"^\s*(?:please\s+)?let\s+#?([\w-]+)\s+[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
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
        and executor in _explicit_tags(evidence)
        and _explicit_action_evidence(transcript, evidence, action_evidence)
    )


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
        r"^\s*(?:please\s+)?let\s+#?([\w-]+)\s+[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
        r"\b#?([\w-]+)\s*(?:,\s*(?:please\s+)?|please\s+)(?!i\b|we\b|they\b|he\b|she\b)[\w-]+\s+(?:this|it|that|the\b|[\w-]+)",
        r"\b(?:assign|delegate|route)\s+(?:this|it|that|the\s+task)?\s*to\s+#?([\w-]+)\b",
    )
    for clause in _positive_clauses(text):
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


def validate_plan(raw: Iterable[dict[str, Any]], *, transcript: str, recorded_at: str) -> list[PlannedAction]:
    """Convert untrusted model JSON into an explicitly bounded action list."""
    if not isinstance(raw, list) or len(raw) > 8:
        raise PebbleCaptureError("classifier actions must be a list of at most eight items")
    allowed_tags = _explicit_tags(transcript)
    result: list[PlannedAction] = []
    seen_indexes: set[int] = set()
    used_delegations: set[str] = set()
    used_action_evidence: set[str] = set()
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
        tags: list[str] = []
        for raw_tag in raw_tags:
            if not isinstance(raw_tag, str):
                continue
            tag = raw_tag.lstrip("#").lower()
            if tag in _VALID_TASK_ASSIGNEES:
                evidence_key = evidence.casefold()
                action_key = action_evidence.casefold()
                if (evidence_key not in used_delegations
                        and action_key not in used_action_evidence
                        and _explicit_task_delegation(
                            transcript, tag, evidence, action_evidence
                        )):
                    tags.append(tag)
                    used_delegations.add(evidence_key)
                    used_action_evidence.add(action_key)
            elif tag not in _ROUTING_TAGS and tag in allowed_tags:
                tags.append(tag)
        normalized_tags = tuple(dict.fromkeys(tags))
        action = PlannedAction(
            kind=kind, title=title, index=index, tags=normalized_tags,
            delegation_evidence=evidence, action_evidence=action_evidence,
        )
        if kind == "task":
            due = raw_action.get("due_date", "")
            if due:
                try:
                    date.fromisoformat(due)
                except (TypeError, ValueError) as exc:
                    raise PebbleCaptureError("task due date is invalid") from exc
            if any(tag in _VALID_TASK_ASSIGNEES for tag in normalized_tags):
                title = _safe_markdown_text(
                    action_evidence, field="action_evidence", limit=500
                )
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
                        or action_key in used_action_evidence
                        or not _explicit_scheduled_delegation(
                            transcript, executor, evidence, action_evidence
                        )):
                    raise PebbleCaptureError("agent schedule lacks explicit valid delegation")
                used_delegations.add(evidence_key)
                used_action_evidence.add(action_key)
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
            )
        else:
            key = raw_action.get("human_key") or f"pebble:{index}"
            evidence = raw_action.get("decision_evidence") or ""
            if not isinstance(key, str) or not isinstance(evidence, str) or not _explicit_operator_decision(transcript, evidence):
                raise PebbleCaptureError("human action lacks an explicit operator-only decision")
            action = replace(action, human_key=key, decision_evidence=evidence)
        result.append(action)
    return result


class LocalOnlyJournalClassifier:
    """Tool-free classifier pinned to the local llama server; it never retries remotely."""

    async def classify(self, final_text: str, recorded_at: str) -> list[dict[str, Any]]:
        prompt = classifier_prompt(
            transcript=final_text,
            recorded_at=recorded_at,
            local_timezone=settings.timezone,
            allow_agent_schedule=True,
        )
        client = LocalLLMClient(
            base_url=_loopback_llm_url(settings.local_llm_url),
            timeout=30,
            trust_env=False,
        )
        response = await client.acreate(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0,
            enable_thinking=False,
        )
        parsed = extract_json(response.text or "")
        actions = parsed.get("actions")
        if not isinstance(actions, list):
            raise PebbleCaptureError("local classifier returned no action list")
        return actions


class PebbleCaptureConsumer:
    """Classify then apply finalized result payloads through canonical stores only."""

    def __init__(
        self, ledger: CaptureLedger, task_manager: TaskManager, scheduler_store: SchedulerStore,
        classifier: Optional[LocalOnlyJournalClassifier] = None, *, apply: bool = False,
    ):
        self.ledger = ledger
        self.task_manager = task_manager
        self.scheduler_store = scheduler_store
        self.classifier = classifier or LocalOnlyJournalClassifier()
        self.apply = apply

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
            actions = validate_plan(raw_actions, transcript=final_text, recorded_at=recorded_at)
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
                try:
                    task, _ = self.task_manager.create_or_find_by_operation(
                        key, description=action.title, due_date=action.due_date or None,
                        tags=list(action.tags),
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
