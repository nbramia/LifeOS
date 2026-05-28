"""Tiered backup retention for SQLite database snapshots.

A backup file is kept iff it is the newest one falling into any of these
buckets:

- The N most recent backups (the **daily** tier, ``daily_keep``) — always kept.
- 1 per week-of-age between ``daily_keep`` and ``weekly_horizon_days``.
- 1 per month-of-age between ``weekly_horizon_days`` and ``monthly_horizon_days``.
- 1 per quarter (~3 months) of age beyond ``monthly_horizon_days``.

At the defaults (5 / 35 / 365 day boundaries), steady state is roughly:

  5 daily + 4 weekly + 11 monthly + ~4/year quarterly ≈ 25 files growing
  linearly afterwards. Each backup is a full copy of the source DB.

The helper is intentionally parameterised by ``db_basename`` so other
stores (``crm.db``, ``sync_health.db``, …) can reuse the same retention
policy without copy-pasting the bucket math. Issue #227.

Filename convention
-------------------

The writer (``backup_filename``) and the pruner (``prune``) share a single
naming scheme: ``<basename>.<YYYYMMDD_HHMMSS>.backup``. Files that don't
match this exact pattern are ignored — operators sometimes drop in
hand-rolled names like ``interactions.db.pre-upgrade.backup`` for manual
safekeeping; retention should leave those alone.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"


def backup_filename(db_basename: str, ts: datetime) -> str:
    """Canonical backup filename for ``db_basename`` at ``ts``.

    Example: ``backup_filename("interactions.db", now)`` →
    ``interactions.db.20260528_120000.backup``.

    Use this in concert with ``parse_backup_timestamp`` / ``prune`` so the
    writer and pruner can never silently diverge if the naming scheme is
    later changed (e.g., to add microseconds).
    """
    return f"{db_basename}.{ts.strftime(_BACKUP_TIMESTAMP_FORMAT)}.backup"


def _build_pattern(db_basename: str) -> tuple[str, re.Pattern[str]]:
    """Return the glob + the compiled regex for ``db_basename``.

    Both forms are needed: the glob narrows the directory listing cheaply;
    the regex pins down the exact ``YYYYMMDD_HHMMSS`` shape so that
    non-canonical files are excluded from retention math.
    """
    glob_pattern = f"{db_basename}.*.backup"
    # ``re.escape`` so basenames containing ``.`` (every SQLite file)
    # don't accidentally match other names.
    regex = re.compile(
        rf"^{re.escape(db_basename)}\.(\d{{8}}_\d{{6}})\.backup$"
    )
    return glob_pattern, regex


def parse_backup_timestamp(name: str, db_basename: str) -> Optional[datetime]:
    """Parse the timestamp embedded in a canonical backup filename.

    Returns ``None`` for files that don't match ``db_basename`` exactly,
    or whose timestamp portion isn't parseable. Callers should treat
    ``None`` as "ignore this file" — that's how we leave manual backups
    (``interactions.db.pre-upgrade.backup``) alone.
    """
    _, regex = _build_pattern(db_basename)
    m = regex.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), _BACKUP_TIMESTAMP_FORMAT)
    except ValueError:
        return None


@dataclass(frozen=True)
class RetentionPolicy:
    """Tier boundaries (in days) for ``prune``. All defaults match what
    ``interaction_store.create_backup`` shipped with in PR #225."""

    daily_keep: int = 5
    weekly_horizon_days: int = 35
    monthly_horizon_days: int = 365
    week_days: int = 7
    month_days: int = 30
    quarter_days: int = 90


DEFAULT_POLICY = RetentionPolicy()


def prune(
    backup_dir: Path,
    db_basename: str,
    *,
    policy: RetentionPolicy = DEFAULT_POLICY,
    now: Optional[datetime] = None,
) -> list[Path]:
    """Apply the tiered retention policy to ``backup_dir`` for one store.

    Args:
        backup_dir: Directory holding ``<db_basename>.<ts>.backup`` files.
        db_basename: e.g., ``"interactions.db"`` or ``"crm.db"``. Only
            files matching this basename's canonical pattern are
            considered; anything else stays untouched.
        policy: Tier boundary overrides. Use ``DEFAULT_POLICY`` unless
            you have a specific reason.
        now: Reference timestamp for "age" computations; defaults to
            ``datetime.now()``. Pass an explicit value in tests so bucket
            math stays deterministic.

    Returns:
        The list of paths removed. Empty when the directory is fresh or
        every backup fits within a distinct bucket.
    """
    if now is None:
        now = datetime.now()

    glob_pattern, _ = _build_pattern(db_basename)

    candidates: list[tuple[datetime, Path]] = []
    for path in backup_dir.glob(glob_pattern):
        ts = parse_backup_timestamp(path.name, db_basename)
        if ts is not None:
            candidates.append((ts, path))

    if not candidates:
        return []

    # Newest first.
    candidates.sort(key=lambda x: x[0], reverse=True)

    keep: set[Path] = set()

    # Tier 1: the N most recent — always kept regardless of bucket math.
    for _, path in candidates[: policy.daily_keep]:
        keep.add(path)

    # Tiers 2/3/4: bucket the older ones; keep the newest in each bucket.
    seen_buckets: set[tuple[str, int]] = set()
    for ts, path in candidates[policy.daily_keep:]:
        age_days = (now - ts).total_seconds() / 86400.0
        if age_days < policy.weekly_horizon_days:
            bucket = ("week", int(age_days // policy.week_days))
        elif age_days < policy.monthly_horizon_days:
            bucket = ("month", int(age_days // policy.month_days))
        else:
            bucket = ("quarter", int(age_days // policy.quarter_days))
        if bucket not in seen_buckets:
            seen_buckets.add(bucket)
            keep.add(path)

    removed: list[Path] = []
    for _, path in candidates:
        if path not in keep:
            try:
                path.unlink()
                removed.append(path)
            except OSError as e:
                logger.warning(f"Could not remove old backup {path}: {e}")
    return removed
