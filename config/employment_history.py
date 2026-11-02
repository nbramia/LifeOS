"""
Owner's employment history, for reading work notes in the context of their date.

Loaded from config/employment_history.json (gitignored; see
config/employment_history.example.json). A missing or malformed file yields an
empty history, which every caller treats as "no employer signal".

Each entry holds the employer's name, email domains, tenure, and vault folders.
Dates are YYYY-MM or YYYY-MM-DD; a null end means current. An entry with no
usable start date still answers for its vault folders but never for a date.
"""
from __future__ import annotations

import calendar
import json
import logging
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "employment_history.json"


def _parse_day(value: object, *, end_of_month: bool = False) -> Optional[date]:
    """Parse YYYY-MM or YYYY-MM-DD; None for anything else."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split("-")
    try:
        numbers = [int(part) for part in parts]
        if len(numbers) == 2:
            year, month = numbers
            day = calendar.monthrange(year, month)[1] if end_of_month else 1
            return date(year, month, day)
        if len(numbers) == 3:
            return date(*numbers)
    except (ValueError, calendar.IllegalMonthError):
        return None
    return None


def _folder_prefix(folder: str) -> str:
    return folder.strip().strip("/").casefold() + "/"


@dataclass(frozen=True)
class Employer:
    """One employer the owner worked for."""

    name: str
    email_domains: frozenset[str] = frozenset()
    start: Optional[date] = None
    end: Optional[date] = None  # None while still employed there
    vault_folders: tuple[str, ...] = ()

    def active_on(self, day: date) -> bool:
        if self.start is None or day < self.start:
            return False
        return self.end is None or day <= self.end

    def owns_path(self, rel_path: str) -> bool:
        folded = rel_path.replace("\\", "/").casefold()
        return any(folded.startswith(_folder_prefix(folder)) for folder in self.vault_folders)


@dataclass(frozen=True)
class EmploymentHistory:
    """Every employer on record; empty when none is configured."""

    employers: tuple[Employer, ...] = ()

    def for_path(self, rel_path: str) -> tuple[Employer, ...]:
        """Employers whose vault folders hold this note."""
        return tuple(e for e in self.employers if e.owns_path(rel_path))

    def on_date(self, day: date) -> tuple[Employer, ...]:
        """Employers current on this day. Overlapping tenures all count."""
        return tuple(e for e in self.employers if e.active_on(day))

    @property
    def email_domains(self) -> frozenset[str]:
        return frozenset(domain for e in self.employers for domain in e.email_domains)


def parse_employment_history(raw: object) -> EmploymentHistory:
    """Build a history from parsed JSON, dropping entries without a name."""
    entries = raw.get("employers") if isinstance(raw, dict) else None
    employers = []
    for entry in entries or []:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            logger.warning("Skipping an employment history entry with no name")
            continue
        employers.append(Employer(
            name=str(entry["name"]).strip(),
            email_domains=frozenset(
                str(domain).strip().casefold() for domain in entry.get("email_domains") or []
            ),
            start=_parse_day(entry.get("start")),
            end=_parse_day(entry.get("end"), end_of_month=True),
            vault_folders=tuple(str(folder) for folder in entry.get("vault_folders") or []),
        ))
    return EmploymentHistory(tuple(employers))


def load_employment_history(path: Optional[Path] = None) -> EmploymentHistory:
    """Read the history from disk. Missing or malformed reads as empty."""
    config_path = Path(path) if path else CONFIG_PATH
    if not config_path.exists():
        return EmploymentHistory()
    try:
        with open(config_path) as f:
            return parse_employment_history(json.load(f))
    except (OSError, ValueError) as e:
        logger.warning(f"Failed to load employment history: {e}")
        return EmploymentHistory()


@lru_cache(maxsize=1)
def get_employment_history() -> EmploymentHistory:
    """The configured history, read once per process."""
    return load_employment_history()
