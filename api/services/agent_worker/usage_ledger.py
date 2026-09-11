"""Canonical, local usage/provenance ledger for agent executions.

The worker's ``SessionStore`` is the durable authority for execution state,
so usage observations live in the same SQLite database.  Provider events are
observations, not identities: ``session_id``/``attempt_id``/``turn_id`` are
the stable LifeOS key and ``event_id`` only makes delivery idempotent.

This module deliberately does not choose an executor or infer a provider.  A
served identity is only written when the caller supplies authoritative
evidence; otherwise it remains NULL (the readout renders that as unknown).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from api.services.agent_worker.session_store import DEFAULT_DB_PATH, SessionStore


MEASURED = "measured"
ESTIMATED = "estimated"
UNKNOWN = "unknown"
QUANTITY_KINDS = frozenset({MEASURED, ESTIMATED, UNKNOWN})
BILLING_CLASSES = frozenset({"subscription", "metered", "local_free", "unknown"})


def _now() -> int:
    return int(time.time())


def _key(session_id: str, attempt_id: str, turn_id: str) -> str:
    return f"{session_id}:{attempt_id}:{turn_id}"


def _source_key(source: str, source_event_id: str | None) -> str:
    if source_event_id:
        return f"{source}:{source_event_id}"
    return source


@dataclass(frozen=True)
class UsageObservation:
    """One provider/executor observation for a stable LifeOS turn."""

    session_id: str
    attempt_id: str
    turn_id: str
    source: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    input_kind: str = UNKNOWN
    output_kind: str = UNKNOWN
    cache_creation_kind: str = UNKNOWN
    cache_read_kind: str = UNKNOWN
    cost_kind: str = UNKNOWN
    billing_class: str = "unknown"
    requested_engine: str | None = None
    requested_model: str | None = None
    requested_provider: str | None = None
    served_engine: str | None = None
    served_model: str | None = None
    served_provider: str | None = None
    evidence_source: str = "unknown"
    source_event_id: str | None = None
    event_id: str | None = None
    observed_at: int | None = None
    cumulative: bool = False
    correction: bool = False
    reservation_usd: float | None = None
    reservation_kind: str = UNKNOWN
    reservation_id: str | None = None
    period_start: str | None = None

    @property
    def usage_key(self) -> str:
        return _key(self.session_id, self.attempt_id, self.turn_id)

    @property
    def stable_event_id(self) -> str:
        if self.event_id:
            return self.event_id
        raw = json.dumps(
            [self.usage_key, self.source, self.source_event_id, self.observed_at],
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerResult:
    usage_key: str
    event_id: str
    duplicate: bool
    corrected: bool
    input_delta: int
    output_delta: int
    cost_delta_usd: float
    reservation_delta_usd: float


_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_observations (
    event_id TEXT PRIMARY KEY,
    usage_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_event_id TEXT,
    observed_at INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cache_read_tokens INTEGER,
    cost_usd REAL,
    input_kind TEXT NOT NULL,
    output_kind TEXT NOT NULL,
    cache_creation_kind TEXT NOT NULL,
    cache_read_kind TEXT NOT NULL,
    cost_kind TEXT NOT NULL,
    billing_class TEXT NOT NULL,
    requested_engine TEXT,
    requested_model TEXT,
    requested_provider TEXT,
    served_engine TEXT,
    served_model TEXT,
    served_provider TEXT,
    evidence_source TEXT NOT NULL,
    cumulative INTEGER NOT NULL DEFAULT 0,
    correction INTEGER NOT NULL DEFAULT 0,
    reservation_usd REAL,
    reservation_kind TEXT NOT NULL,
    reservation_id TEXT,
    period_start TEXT,
    applied_input_delta INTEGER NOT NULL DEFAULT 0,
    applied_output_delta INTEGER NOT NULL DEFAULT 0,
    applied_cost_delta REAL NOT NULL DEFAULT 0,
    applied_reservation_delta REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_observations_key
    ON usage_observations(usage_key, observed_at);

CREATE TABLE IF NOT EXISTS usage_ledger (
    usage_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    billed_cost_usd REAL NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    unknown_cost INTEGER NOT NULL DEFAULT 0,
    reservation_usd REAL NOT NULL DEFAULT 0,
    reservation_id TEXT,
    billing_class TEXT NOT NULL,
    input_kind TEXT NOT NULL,
    output_kind TEXT NOT NULL,
    cost_kind TEXT NOT NULL,
    requested_engine TEXT,
    requested_model TEXT,
    requested_provider TEXT,
    served_engine TEXT,
    served_model TEXT,
    served_provider TEXT,
    evidence_source TEXT NOT NULL,
    first_observed_at INTEGER NOT NULL,
    last_observed_at INTEGER NOT NULL,
    last_snapshot_json TEXT,
    period_start TEXT,
    correction_count INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_reservations (
    reservation_id TEXT PRIMARY KEY,
    usage_key TEXT NOT NULL,
    date TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    released_at INTEGER,
    owner_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_reservations_date
    ON usage_reservations(date, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_reservations_held_usage_key
    ON usage_reservations(usage_key) WHERE status='held';

CREATE TABLE IF NOT EXISTS daily_spend (
    date TEXT PRIMARY KEY,
    total_dollars REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS usage_projection_state (
    usage_key TEXT PRIMARY KEY,
    projected_billed_cost_usd REAL NOT NULL DEFAULT 0,
    projected_input_tokens INTEGER NOT NULL DEFAULT 0,
    projected_output_tokens INTEGER NOT NULL DEFAULT 0,
    projected_at INTEGER NOT NULL
);
"""


class UsageLedger:
    """Idempotent ledger backed by the same DB as ``SessionStore``."""

    def __init__(self, db_path: Path | str | None = None, *, session_store: SessionStore | None = None):
        self.db_path = Path(db_path or (session_store.db_path if session_store else DEFAULT_DB_PATH))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_store = session_store
        with self._connect() as conn:
            conn.executescript(_LEDGER_SCHEMA)
            ledger_columns = {row["name"] for row in conn.execute("PRAGMA table_info(usage_ledger)")}
            if "period_start" not in ledger_columns:
                conn.execute("ALTER TABLE usage_ledger ADD COLUMN period_start TEXT")
            if "reservation_id" not in ledger_columns:
                conn.execute("ALTER TABLE usage_ledger ADD COLUMN reservation_id TEXT")
            observation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(usage_observations)")}
            if "reservation_id" not in observation_columns:
                conn.execute("ALTER TABLE usage_observations ADD COLUMN reservation_id TEXT")
            if "last_snapshot_at" not in ledger_columns:
                conn.execute("ALTER TABLE usage_ledger ADD COLUMN last_snapshot_at INTEGER")
            if "last_snapshot_cost" not in ledger_columns:
                conn.execute("ALTER TABLE usage_ledger ADD COLUMN last_snapshot_cost REAL")
            reservation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(usage_reservations)")}
            if "owner_id" not in reservation_columns:
                conn.execute("ALTER TABLE usage_reservations ADD COLUMN owner_id TEXT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _nonnegative(value: int | float | None, name: str) -> int | float | None:
        if value is None:
            return None
        if value < 0:
            raise ValueError(f"{name} must be non-negative")
        return value

    @staticmethod
    def _validate(observation: UsageObservation) -> None:
        for name in ("input_kind", "output_kind", "cache_creation_kind", "cache_read_kind", "cost_kind", "reservation_kind"):
            if getattr(observation, name) not in QUANTITY_KINDS:
                raise ValueError(f"invalid {name}")
        if observation.billing_class not in BILLING_CLASSES:
            raise ValueError("invalid billing_class")
        for name in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "cost_usd", "reservation_usd"):
            UsageLedger._nonnegative(getattr(observation, name), name)
        if observation.served_model is None and observation.served_provider is not None:
            raise ValueError("served_provider requires served_model")

    @staticmethod
    def _snapshot_value(row: sqlite3.Row | None, name: str) -> int:
        if not row or not row["last_snapshot_json"]:
            return 0
        try:
            return int(json.loads(row["last_snapshot_json"]).get(name) or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0

    def record(self, observation: UsageObservation) -> LedgerResult:
        """Record one observation and atomically apply only its delta.

        Delivery duplicates are no-ops. A second observation for a turn is
        also a no-op unless it is a cumulative snapshot (new delta) or an
        explicit correction with stronger evidence. This is what permits the
        Hermes persister and worker to observe the same turn safely.
        """
        self._validate(observation)
        usage_key = observation.usage_key
        # Provider ids are scoped to their source/turn in practice. Prefixing
        # with the canonical usage key prevents a reused upstream id on a
        # different turn from suppressing a legitimate observation.
        event_id = f"{usage_key}:{observation.stable_event_id}"
        observed_at = observation.observed_at or _now()
        input_tokens = int(observation.input_tokens or 0)
        output_tokens = int(observation.output_tokens or 0)
        cache_creation = int(observation.cache_creation_tokens or 0)
        cache_read = int(observation.cache_read_tokens or 0)
        cost = float(observation.cost_usd or 0.0)
        reservation = float(observation.reservation_usd or 0.0)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._release_stale_holds(conn, date_cls.today().isoformat())
            existing_event = conn.execute(
                "SELECT 1 FROM usage_observations WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing_event:
                # Keep any stale-hold expiry performed under this write lock;
                # duplicate delivery must not undo settlement housekeeping.
                conn.commit()
                return LedgerResult(usage_key, event_id, True, False, 0, 0, 0.0, 0.0)

            current = conn.execute(
                "SELECT * FROM usage_ledger WHERE usage_key = ?", (usage_key,)
            ).fetchone()
            corrected = False
            snapshot_is_newer = True
            input_delta = output_delta = cache_creation_delta = cache_read_delta = 0
            cost_delta = reservation_delta = 0.0
            snapshot_cost = cost if observation.cost_kind == MEASURED else 0.0
            if current is None:
                input_delta, output_delta = input_tokens, output_tokens
                cache_creation_delta, cache_read_delta = cache_creation, cache_read
                cost_delta = cost if observation.cost_kind == MEASURED else 0.0
                reservation_delta = reservation if observation.cost_kind != MEASURED else 0.0
            elif observation.correction and observation.cost_kind == MEASURED and current["cost_kind"] != MEASURED:
                # A measured correction supersedes an earlier unknown or
                # estimated observation even when the provider delivered it
                # as a cumulative snapshot. Handle this before the ordinary
                # cumulative path so the held reservation is released and the
                # canonical row changes from unknown/estimated to measured.
                corrected = True
                cost_delta = cost - float(current["billed_cost_usd"])
                reservation_delta = -float(current["reservation_usd"])
                if observation.cumulative:
                    previous_snapshot_at = int(current["last_snapshot_at"] or 0)
                    snapshot_is_newer = observed_at >= previous_snapshot_at
                    if snapshot_is_newer:
                        prior_in = self._snapshot_value(current, "input_tokens")
                        prior_out = self._snapshot_value(current, "output_tokens")
                        prior_cache_creation = self._snapshot_value(current, "cache_creation_tokens")
                        prior_cache_read = self._snapshot_value(current, "cache_read_tokens")
                        input_delta = max(0, input_tokens - prior_in)
                        output_delta = max(0, output_tokens - prior_out)
                        cache_creation_delta = max(0, cache_creation - prior_cache_creation)
                        cache_read_delta = max(0, cache_read - prior_cache_read)
                        input_tokens = max(input_tokens, prior_in)
                        output_tokens = max(output_tokens, prior_out)
                        cache_creation = max(cache_creation, prior_cache_creation)
                        cache_read = max(cache_read, prior_cache_read)
                        snapshot_cost = cost
                    else:
                        input_delta = output_delta = cache_creation_delta = cache_read_delta = 0
                        input_tokens = max(input_tokens, int(current["input_tokens"]))
                        output_tokens = max(output_tokens, int(current["output_tokens"]))
                        cache_creation = max(cache_creation, int(current["cache_creation_tokens"]))
                        cache_read = max(cache_read, int(current["cache_read_tokens"]))
                        # The measured correction is authoritative even when
                        # it arrived after a newer unknown snapshot. Reset
                        # the measured cost baseline and timestamp to this
                        # correction so a later snapshot bills only its delta.
                        snapshot_cost = cost
                else:
                    input_delta = max(0, input_tokens - int(current["input_tokens"]))
                    output_delta = max(0, output_tokens - int(current["output_tokens"]))
                    cache_creation_delta = max(0, cache_creation - int(current["cache_creation_tokens"]))
                    cache_read_delta = max(0, cache_read - int(current["cache_read_tokens"]))
            elif observation.cumulative:
                previous_snapshot_at = int(current["last_snapshot_at"] or 0)
                snapshot_is_newer = observed_at >= previous_snapshot_at
                if not snapshot_is_newer:
                    # Provider snapshots can be delivered out of order after
                    # a reconnect. Keep the high-water mark and retain the
                    # event only for audit; applying a negative/replayed
                    # delta here would inflate every later snapshot.
                    input_delta = output_delta = cache_creation_delta = cache_read_delta = 0
                    cost_delta = 0.0
                else:
                    prior_in = self._snapshot_value(current, "input_tokens")
                    prior_out = self._snapshot_value(current, "output_tokens")
                    prior_cache_creation = self._snapshot_value(current, "cache_creation_tokens")
                    prior_cache_read = self._snapshot_value(current, "cache_read_tokens")
                    input_delta = max(0, input_tokens - prior_in)
                    output_delta = max(0, output_tokens - prior_out)
                    cache_creation_delta = max(0, cache_creation - prior_cache_creation)
                    cache_read_delta = max(0, cache_read - prior_cache_read)
                    # Equal-timestamp snapshots are valid provider updates,
                    # but individual counters can temporarily move backward.
                    # Merge them into a componentwise high-water mark so a
                    # later snapshot cannot replay already-applied usage.
                    input_tokens = max(input_tokens, prior_in)
                    output_tokens = max(output_tokens, prior_out)
                    cache_creation = max(cache_creation, prior_cache_creation)
                    cache_read = max(cache_read, prior_cache_read)
                    if observation.cost_kind == MEASURED:
                        prior_cost = float(current["last_snapshot_cost"] or 0.0)
                        cost_delta = max(0.0, cost - prior_cost)
                        snapshot_cost = max(cost, prior_cost)
                    else:
                        snapshot_cost = float(current["last_snapshot_cost"] or 0.0)
            elif observation.correction and observation.cost_kind == MEASURED and current["cost_kind"] != MEASURED:
                corrected = True
                cost_delta = cost - float(current["billed_cost_usd"])
                reservation_delta = -float(current["reservation_usd"])
                input_delta = input_tokens - int(current["input_tokens"])
                output_delta = output_tokens - int(current["output_tokens"])
                cache_creation_delta = cache_creation - int(current["cache_creation_tokens"])
                cache_read_delta = cache_read - int(current["cache_read_tokens"])
            else:
                # Same stable turn observed by another adapter: retain the
                # first observation unless the caller explicitly says this is
                # a correction. The event is still retained for audit.
                input_delta = output_delta = cache_creation_delta = cache_read_delta = 0

            billed = float(current["billed_cost_usd"]) if current else 0.0
            estimated = float(current["estimated_cost_usd"]) if current else 0.0
            prior_reservation = float(current["reservation_usd"]) if current else 0.0
            if observation.cost_kind == MEASURED:
                billed += cost_delta
                if corrected:
                    estimated = 0.0
            elif current is None or corrected:
                estimated += cost
            reservation_total = max(0.0, prior_reservation + reservation_delta)
            if current is None and observation.cost_kind != MEASURED:
                reservation_total = reservation

            snapshot = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_tokens": cache_creation,
                "cache_read_tokens": cache_read,
            }
            release_reservation = observation.cost_kind == MEASURED and (
                current is None or corrected
            )
            reservation_id = (
                observation.reservation_id
                or (current["reservation_id"] if current is not None else None)
            )
            if release_reservation:
                clauses = ["status='held'"]
                params: list[Any] = []
                if reservation_id:
                    clauses.append("reservation_id=?")
                    params.append(reservation_id)
                else:
                    clauses.append("usage_key=?")
                    params.append(usage_key)
                conn.execute(
                    "UPDATE usage_reservations SET status='released', released_at=? "
                    f"WHERE {' AND '.join(clauses)}",
                    [_now(), *params],
                )
                reservation_total = 0.0
            applied_unknown = int(observation.cost_kind == UNKNOWN)
            if current is None:
                conn.execute(
                    """INSERT INTO usage_ledger (
                        usage_key, session_id, attempt_id, turn_id,
                        input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                        billed_cost_usd, estimated_cost_usd, unknown_cost, reservation_usd,
                        billing_class, input_kind, output_kind, cost_kind,
                        requested_engine, requested_model, requested_provider,
                        served_engine, served_model, served_provider, evidence_source,
                        first_observed_at, last_observed_at, last_snapshot_json,
                        last_snapshot_at, last_snapshot_cost, reservation_id,
                        period_start, correction_count, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (usage_key, observation.session_id, observation.attempt_id, observation.turn_id,
                     input_tokens, output_tokens, cache_creation, cache_read,
                     max(0.0, billed), max(0.0, estimated), applied_unknown, reservation_total,
                     observation.billing_class, observation.input_kind, observation.output_kind,
                     observation.cost_kind, observation.requested_engine, observation.requested_model,
                     observation.requested_provider, observation.served_engine, observation.served_model,
                     observation.served_provider, observation.evidence_source, observed_at, observed_at,
                     json.dumps(snapshot), observed_at if observation.cumulative else None,
                     snapshot_cost if observation.cumulative and observation.cost_kind == MEASURED else None,
                     reservation_id, observation.period_start, 0, observed_at),
                )
            else:
                conn.execute(
                    """UPDATE usage_ledger SET
                        input_tokens=input_tokens+?, output_tokens=output_tokens+?,
                        cache_creation_tokens=cache_creation_tokens+?, cache_read_tokens=cache_read_tokens+?,
                        billed_cost_usd=?, estimated_cost_usd=?, unknown_cost=CASE WHEN ? THEN 0 ELSE unknown_cost OR ? END,
                        reservation_usd=?, reservation_id=COALESCE(?, reservation_id),
                        last_observed_at=MAX(last_observed_at, ?),
                        last_snapshot_json=CASE WHEN ? THEN ? ELSE last_snapshot_json END,
                        last_snapshot_at=CASE WHEN ? THEN ? ELSE last_snapshot_at END,
                        last_snapshot_cost=CASE WHEN ? THEN ? ELSE last_snapshot_cost END,
                        period_start=COALESCE(period_start, ?), correction_count=correction_count+?, updated_at=?,
                        cost_kind=?, evidence_source=?
                        WHERE usage_key=?""",
                    (input_delta, output_delta, cache_creation_delta, cache_read_delta,
                     max(0.0, billed), max(0.0, estimated), int(corrected), applied_unknown,
                     reservation_total, reservation_id, observed_at,
                     int(observation.cumulative and (snapshot_is_newer or corrected)), json.dumps(snapshot),
                     int(observation.cumulative and (snapshot_is_newer or corrected)),
                     observed_at, int(observation.cumulative and (snapshot_is_newer or corrected)),
                     snapshot_cost if observation.cumulative else 0.0, observation.period_start,
                     int(corrected), observed_at,
                     observation.cost_kind if corrected else current["cost_kind"],
                     observation.evidence_source if corrected else current["evidence_source"], usage_key),
                )

            conn.execute(
                """INSERT INTO usage_observations (
                    event_id, usage_key, session_id, attempt_id, turn_id, source, source_event_id,
                    observed_at, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                    cost_usd, input_kind, output_kind, cache_creation_kind, cache_read_kind, cost_kind,
                    billing_class, requested_engine, requested_model, requested_provider,
                    served_engine, served_model, served_provider, evidence_source, cumulative, correction,
                    reservation_usd, reservation_kind, reservation_id, period_start,
                    applied_input_delta, applied_output_delta, applied_cost_delta, applied_reservation_delta
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, usage_key, observation.session_id, observation.attempt_id, observation.turn_id,
                 observation.source, observation.source_event_id, observed_at, observation.input_tokens,
                 observation.output_tokens, observation.cache_creation_tokens, observation.cache_read_tokens,
                 observation.cost_usd, observation.input_kind, observation.output_kind,
                 observation.cache_creation_kind, observation.cache_read_kind, observation.cost_kind,
                 observation.billing_class, observation.requested_engine, observation.requested_model,
                 observation.requested_provider, observation.served_engine, observation.served_model,
                 observation.served_provider, observation.evidence_source, int(observation.cumulative),
                 int(observation.correction), observation.reservation_usd, observation.reservation_kind,
                 observation.reservation_id,
                 observation.period_start, input_delta, output_delta, cost_delta, reservation_delta),
            )
            self._apply_session_delta(conn, observation.session_id, input_delta, output_delta,
                                      cache_creation_delta, cache_read_delta, cost_delta,
                                      observation.period_start,
                                      observation.attempt_id, observation.turn_id,
                                      observation.cost_kind == UNKNOWN)
            conn.execute("COMMIT")
        return LedgerResult(usage_key, event_id, False, corrected, input_delta, output_delta, cost_delta, reservation_delta)

    @staticmethod
    def _apply_session_delta(conn: sqlite3.Connection, session_id: str, input_delta: int,
                             output_delta: int, cache_creation_delta: int,
                             cache_read_delta: int, cost_delta: float,
                             period_start: str | None = None,
                             attempt_id: str | None = None,
                             turn_id: str | None = None,
                             unpriced: bool = False) -> None:
        """Apply billed/token totals to the owning session when it exists.

        ``session_id`` is not a foreign key because operator/Hermes adapters
        can arrive before a worker row is replicated; the ledger remains the
        authority and replay can reconcile the session later.
        """
        # Session totals are a projection of canonical usage, but only the
        # exact immutable lifecycle tuple may receive a delta. In particular,
        # do not fall back to session_id-only when a stale callback arrives
        # after an attempt was reopened. A pre-turn observation uses the
        # explicit legacy sentinel from identity_for_session to represent the
        # persisted row's NULL turn_id; it still must match the attempt.
        if attempt_id is None or turn_id is None:
            return
        legacy_turn_id = f"legacy:{session_id}:turn"
        if turn_id == legacy_turn_id:
            where = "session_id=? AND attempt_id=? AND turn_id IS NULL"
            identity_params: list[object] = [session_id, attempt_id]
        else:
            where = "session_id=? AND attempt_id=? AND turn_id=?"
            identity_params = [session_id, attempt_id, turn_id]
        params: list[object] = [
            input_delta, output_delta, cache_creation_delta, cache_read_delta,
            cost_delta, _now(), int(unpriced), *identity_params,
        ]
        conn.execute(
            f"""UPDATE sessions SET
                total_input_tokens=total_input_tokens+?, total_output_tokens=total_output_tokens+?,
                total_cache_creation_tokens=total_cache_creation_tokens+?,
                total_cache_read_tokens=total_cache_read_tokens+?,
                total_dollars=total_dollars+?, last_activity_at=?,
                unpriced=unpriced OR ?
                WHERE {where}""",
            tuple(params),
        )
        if cost_delta:
            day = (period_start or date_cls.today().isoformat())[:10]
            conn.execute(
                """INSERT INTO daily_spend(date,total_dollars) VALUES (?,?)
                   ON CONFLICT(date) DO UPDATE SET total_dollars=total_dollars+excluded.total_dollars""",
                (day, cost_delta),
            )

    def reserve(self, usage_key: str, amount_usd: float, *, daily_cap_dollars: float,
                reservation_id: str | None = None, today: date_cls | None = None,
                owner_id: str | None = None) -> bool:
        """Atomically reserve bounded unknown/estimated spend for admission.

        A non-positive cap is always paused, including local and subscription
        routes. Reservations are not billed totals and are released by
        ``release`` or by a measured correction.
        """
        if amount_usd < 0:
            raise ValueError("amount_usd must be non-negative")
        day = (today or date_cls.today()).isoformat()
        reservation_id = reservation_id or f"{usage_key}:{day}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._release_stale_holds(conn, day)
            if daily_cap_dollars <= 0:
                conn.commit()
                return False
            reservation_row = conn.execute(
                "SELECT usage_key, date, amount_usd, status, owner_id FROM usage_reservations "
                "WHERE reservation_id=?", (reservation_id,),
            ).fetchone()
            if reservation_row is not None:
                # Admission retries must not count their own held row against
                # the cap a second time. A released reservation can be
                # reactivated for the same usage key, which is required when
                # a claim loses its race after admission or a prior-day hold
                # expires before the same task retries.
                if reservation_row["status"] == "held":
                    if owner_id is not None and reservation_row["owner_id"] is None:
                        conn.execute(
                            "UPDATE usage_reservations SET owner_id=? WHERE reservation_id=?",
                            (owner_id, reservation_id),
                        )
                    conn.commit()
                    return (
                        reservation_row["usage_key"] == usage_key
                        and reservation_row["date"] == day
                        and float(reservation_row["amount_usd"]) == float(amount_usd)
                    )
                if (
                    reservation_row["status"] != "released"
                    or reservation_row["usage_key"] != usage_key
                    or float(reservation_row["amount_usd"]) != float(amount_usd)
                ):
                    conn.commit()
                    return False
            # ``usage_key`` is the canonical admission identity. A caller may
            # retry with a newly-created reservation id after a timeout, so
            # reservation-id lookup alone would admit the same turn twice.
            # Only an active row participates here; released rows do not block
            # a genuinely new admission.
            held_for_key = conn.execute(
                "SELECT usage_key, date, amount_usd, status FROM usage_reservations "
                "WHERE usage_key=? AND status='held'", (usage_key,),
            ).fetchone()
            if held_for_key is not None:
                conn.commit()
                return (
                    held_for_key["date"] == day
                    and float(held_for_key["amount_usd"]) == float(amount_usd)
                )
            used = conn.execute("SELECT COALESCE(total_dollars,0) FROM daily_spend WHERE date=?", (day,)).fetchone()
            held = conn.execute(
                "SELECT COALESCE(SUM(amount_usd),0) FROM usage_reservations WHERE date=? AND status='held'", (day,)
            ).fetchone()
            if float((used[0] if used else 0) or 0) + float((held[0] if held else 0) or 0) + amount_usd > daily_cap_dollars:
                conn.rollback()
                return False
            if reservation_row is not None:
                conn.execute(
                    "UPDATE usage_reservations SET date=?, status='held', created_at=?, released_at=NULL, "
                    "owner_id=? WHERE reservation_id=? AND status='released'",
                    (day, _now(), owner_id, reservation_id),
                )
                conn.commit()
                return True
            # Keep the unique usage-key constraint as the final arbiter for
            # independent processes that race between the read and insert.
            # INSERT OR IGNORE turns that loser path into a deterministic
            # boolean result instead of leaking IntegrityError to admission.
            inserted = conn.execute(
                """INSERT OR IGNORE INTO usage_reservations(
                       reservation_id,usage_key,date,amount_usd,status,created_at,owner_id
                   ) VALUES (?,?,?,?, 'held', ?, ?)""",
                (reservation_id, usage_key, day, amount_usd, _now(), owner_id),
            ).rowcount
            if inserted != 1:
                winner = conn.execute(
                    "SELECT date, amount_usd FROM usage_reservations "
                    "WHERE usage_key=? AND status='held'", (usage_key,),
                ).fetchone()
                conn.commit()
                return bool(
                    winner is not None
                    and winner["date"] == day
                    and float(winner["amount_usd"]) == float(amount_usd)
                )
            conn.commit()
        return True

    def release(self, reservation_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE usage_reservations SET status='released', released_at=? WHERE reservation_id=? AND status='held'",
                (_now(), reservation_id),
            )
        return cur.rowcount == 1

    def release_owned(self, reservation_id: str, owner_id: str) -> bool:
        """Release only a hold still owned by this claim attempt.

        A losing claimant must not clear a winner's shared task reservation
        after the winner adopts it. Legacy callers without an owner continue
        using ``release`` above.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE usage_reservations SET status='released', released_at=? "
                "WHERE reservation_id=? AND owner_id=? AND status='held'",
                (_now(), reservation_id, owner_id),
            )
        return cur.rowcount == 1

    def adopt_reservation(
        self, usage_key: str, amount_usd: float, *, daily_cap_dollars: float,
        reservation_id: str, owner_id: str, today: date_cls | None = None,
    ) -> bool:
        """Bind a shared admission hold to the winning claim identity.

        This transfer is serialized with loser-owned release. If a loser
        released the hold just before the winner reached this method, retry
        through ``reserve`` so the winner still owns a cap reservation.
        """
        day = (today or date_cls.today()).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._release_stale_holds(conn, day)
            row = conn.execute(
                "SELECT reservation_id FROM usage_reservations "
                "WHERE usage_key=? AND status='held'", (usage_key,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE usage_reservations SET owner_id=? WHERE reservation_id=?",
                    (owner_id, row["reservation_id"]),
                )
                conn.commit()
                return True
            conn.commit()
        return self.reserve(
            usage_key, amount_usd, daily_cap_dollars=daily_cap_dollars,
            reservation_id=reservation_id, owner_id=owner_id, today=today,
        )

    @staticmethod
    def _release_stale_holds(conn: sqlite3.Connection, day: str) -> None:
        """Expire holds from prior days while holding the ledger write lock."""
        conn.execute(
            "UPDATE usage_reservations SET status='released', released_at=? "
            "WHERE status='held' AND date<?",
            (_now(), day),
        )

    def get(self, usage_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM usage_ledger WHERE usage_key=?", (usage_key,)).fetchone()
        return dict(row) if row else None

    def daily_readout(self, *, today: date_cls | None = None, daily_cap_dollars: float | None = None) -> dict[str, Any]:
        day = (today or date_cls.today()).isoformat()
        with self._connect() as conn:
            self._release_stale_holds(conn, day)
            billed = conn.execute("SELECT COALESCE(total_dollars,0) FROM daily_spend WHERE date=?", (day,)).fetchone()
            reserved = conn.execute(
                "SELECT COALESCE(SUM(amount_usd),0) FROM usage_reservations WHERE date=? AND status='held'", (day,)
            ).fetchone()
        return {"date": day, "billed_usd": float(billed[0] if billed else 0), "reserved_usd": float(reserved[0] if reserved else 0),
                "cap_usd": daily_cap_dollars, "paused": daily_cap_dollars is not None and daily_cap_dollars <= 0}

    def replay_projection(self, usage_store: Any) -> int:
        """Replay canonical billed rows into the legacy UsageStore projection."""
        count = 0
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM usage_ledger ORDER BY first_observed_at, usage_key").fetchall()
        for row in rows:
            # Legacy/admin totals intentionally expose estimated subscription
            # usage too. Metered corrections replace estimates in the
            # canonical row, so this remains a single projection per key.
            projected_cost = float(row["billed_cost_usd"] or 0) + float(row["estimated_cost_usd"] or 0)
            # Board/proxy adapters may already have materialized the legacy
            # row with a public conversation id. Preserve that binding when
            # replaying canonical data; the ledger's local session id is not
            # interchangeable with Hermes's conversation id.
            conversation_id = row["session_id"]
            get_conversation_id = getattr(usage_store, "conversation_id_for_usage_key", None)
            if callable(get_conversation_id):
                conversation_id = get_conversation_id(row["usage_key"]) or conversation_id
            usage_store.record_usage(
                model=row["served_model"] or row["requested_model"] or "unknown",
                input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
                cost_usd=projected_cost, conversation_id=conversation_id,
                unpriced=bool(row["unknown_cost"]), usage_key=row["usage_key"],
                requested_engine=row["requested_engine"], requested_model=row["requested_model"],
                served_engine=row["served_engine"], served_model=row["served_model"],
                billing_class=row["billing_class"], evidence_source=row["evidence_source"],
            )
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO usage_projection_state(usage_key,projected_billed_cost_usd,
                       projected_input_tokens,projected_output_tokens,projected_at)
                       VALUES (?,?,?,?,?) ON CONFLICT(usage_key) DO UPDATE SET
                       projected_billed_cost_usd=excluded.projected_billed_cost_usd,
                       projected_input_tokens=excluded.projected_input_tokens,
                       projected_output_tokens=excluded.projected_output_tokens,
                       projected_at=excluded.projected_at""",
                    (row["usage_key"], row["billed_cost_usd"], row["input_tokens"], row["output_tokens"], _now()),
                )
            count += 1
        return count

    def record_cli_usage(
        self,
        session: Any,
        *,
        source: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float | None,
        event_id: str,
        source_event_id: str | None = None,
        cached_input_tokens: int = 0,
    ) -> LedgerResult:
        """Record subscription CLI usage with a separate API-equivalent estimate.

        Codex/Claude Code subscription usage is visible and bounded but is not
        counted as metered dollars. The CLI cannot prove its served model from
        the normalized stream, so served identity remains unknown.
        """
        sid, attempt_id, turn_id = identity_for_session(session)
        spec = getattr(session, "execution_spec", None) or {}
        requested_model = getattr(session, "model", None) or spec.get("model_id")
        return self.record(UsageObservation(
            session_id=sid, attempt_id=attempt_id, turn_id=turn_id, source=source,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=cached_input_tokens,
            input_kind=MEASURED, output_kind=MEASURED,
            cache_read_kind=MEASURED if cached_input_tokens else UNKNOWN,
            cost_usd=estimated_cost_usd, cost_kind=ESTIMATED if estimated_cost_usd is not None else UNKNOWN,
            billing_class="subscription", requested_engine=getattr(session, "routing", None),
            requested_model=requested_model, served_engine=None, served_model=None,
            evidence_source="cli_usage_event", event_id=event_id, source_event_id=source_event_id,
            reservation_usd=estimated_cost_usd, reservation_kind=ESTIMATED if estimated_cost_usd is not None else UNKNOWN,
            reservation_id=reservation_id_for_session(session),
        ))


def identity_for_session(session: Any) -> tuple[str, str, str]:
    """Read persisted lifecycle identity with compatibility handling."""
    spec = getattr(session, "execution_spec", None) or {}
    sid = str(getattr(session, "session_id"))
    return (
        sid,
        str(getattr(session, "attempt_id", None) or spec.get("attempt_id") or f"legacy:{sid}"),
        str(getattr(session, "turn_id", None) or spec.get("turn_id") or f"legacy:{sid}:turn"),
    )


def reservation_id_for_session(session: Any) -> str:
    """Stable admission reservation id shared by every executor adapter."""
    return f"admission:{getattr(session, 'task_id', None) or getattr(session, 'session_id')}"
