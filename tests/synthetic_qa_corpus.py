"""Owned-candidate corpus for the server-backed QA lane.

The fixture deliberately fails closed unless the caller is running inside the
owned test-instance environment.  It indexes through ``IndexerService`` so
both Chroma and BM25 are populated; a search-only fixture would leave the
hybrid path untested.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest


def _relative_date(days_ago: int) -> str:
    """A YYYY-MM-DD date ``days_ago`` days before now.

    Computed at call time rather than baked in as a calendar-date literal,
    so the corpus's recency signal (and the 365-day decay in
    ``calculate_recency_boost``) stays meaningful indefinitely instead of
    quietly expiring once real time passes whatever date this file was
    last written on.
    """
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _relative_date_compact(days_ago: int) -> str:
    """YYYYMMDD form of ``_relative_date``, for the one record that exists
    to test filename-embedded compact-date extraction."""
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")


# Age, in days, of each synthetic record — the actual dates are computed
# relative to "now" (see _relative_date) rather than hardcoded. Spacing
# mirrors the original fixture's intent: five records within the same
# "recent" window (see SYNTHETIC_RECENT_CUTOFF_DAYS) and one unambiguously
# past the 365-day recency-boost decay.
_DAYS_AGO_CAMPAIGN_OPS = 0
_DAYS_AGO_MEETING_DISCUSSION = 26
_DAYS_AGO_MEETING_SYNC_CALL = 56
_DAYS_AGO_DAILY_JOURNAL = 89
_DAYS_AGO_ACTION_ITEMS = 123
_DAYS_AGO_LEGACY_ARCHIVE = 1000

SYNTHETIC_RECENT_CUTOFF_DAYS = 180

SYNTHETIC_RECENT_ACTIONS_TRIGGER = "[synthetic-qa-recent-actions]"
# The recent-actions trigger's search_vault query ("action items tasks
# todo") ranks the campaign-operations record first (see the corpus's own
# recency-fix rationale below), so its date is what the LLM stub's
# extraction regex actually finds in the real tool result.
SYNTHETIC_RECENT_ACTIONS_DATE = _relative_date(_DAYS_AGO_CAMPAIGN_OPS)
SYNTHETIC_OLD_ACTIONS_DATE = _relative_date(_DAYS_AGO_LEGACY_ARCHIVE)


@dataclass(frozen=True)
class SyntheticRecord:
    """One deterministic markdown note and its expected indexed metadata."""

    relative_path: str
    modified_date: str
    note_type: str
    content: str

    @property
    def file_name(self) -> str:
        return Path(self.relative_path).name


@dataclass(frozen=True)
class SyntheticCorpus:
    """Paths and records owned by one candidate instance."""

    vault_path: Path
    records: tuple[SyntheticRecord, ...]

    @property
    def file_paths(self) -> tuple[Path, ...]:
        return tuple(self.vault_path / r.relative_path for r in self.records)

    @property
    def recent_file_names(self) -> frozenset[str]:
        cutoff = _relative_date(SYNTHETIC_RECENT_CUTOFF_DAYS)
        # YYYY-MM-DD strings sort lexicographically, so this is exactly
        # "modified within the last SYNTHETIC_RECENT_CUTOFF_DAYS days".
        return frozenset(
            r.file_name for r in self.records if r.modified_date >= cutoff
        )


def _body(label: str, *sentences: str) -> str:
    """Build a short, one-chunk markdown note with a stable marker."""

    return "\n\n".join((f"# {label}", *sentences)) + "\n"


SYNTHETIC_RECORDS: tuple[SyntheticRecord, ...] = (
    SyntheticRecord(
        f"ML/{_relative_date(_DAYS_AGO_CAMPAIGN_OPS)}-campaign-operations.md",
        _relative_date(_DAYS_AGO_CAMPAIGN_OPS),
        "ML",
        _body(
            "Synthetic campaign operations",
            "This synthetic ML note records campaign operations work after a meeting notes discussion.",
            "Action items tasks todo: verify the synthetic campaign checklist.",
            "This is synthetic test query content with a controlled relevance marker.",
        ),
    ),
    SyntheticRecord(
        f"Work/{_relative_date(_DAYS_AGO_MEETING_DISCUSSION)}-meeting-discussion.md",
        _relative_date(_DAYS_AGO_MEETING_DISCUSSION),
        "Work",
        _body(
            "Synthetic meeting notes",
            "Synthetic meeting notes discussion covers a calendar schedule appointment.",
            "Action items tasks todo: review the synthetic agenda.",
            "This is synthetic test query content.",
        ),
    ),
    SyntheticRecord(
        f"Work/Meeting {_relative_date_compact(_DAYS_AGO_MEETING_SYNC_CALL)}.md",
        _relative_date(_DAYS_AGO_MEETING_SYNC_CALL),
        "Work",
        _body(
            "Synthetic sync call",
            "Synthetic meeting notes discussion records a meeting sync call and a calendar schedule appointment.",
            "Action items tasks todo: send the synthetic follow-up.",
            "This is synthetic test query content.",
        ),
    ),
    SyntheticRecord(
        f"Personal/{_relative_date(_DAYS_AGO_DAILY_JOURNAL)}-daily-notes-journal.md",
        _relative_date(_DAYS_AGO_DAILY_JOURNAL),
        "Personal",
        _body(
            "Synthetic daily journal",
            "Daily notes journal records a meeting notes discussion about a synthetic appointment.",
            "Action items tasks todo: catch up on the synthetic daily journal backlog.",
            "This is synthetic test query content.",
        ),
    ),
    SyntheticRecord(
        f"LifeOS/{_relative_date(_DAYS_AGO_ACTION_ITEMS)}-action-items.md",
        _relative_date(_DAYS_AGO_ACTION_ITEMS),
        "LifeOS",
        _body(
            "Synthetic action items",
            "Action items tasks todo continue after a meeting notes discussion.",
            "This is synthetic test query content.",
        ),
    ),
    SyntheticRecord(
        f"Personal/{_relative_date(_DAYS_AGO_LEGACY_ARCHIVE)}-legacy-action-items.md",
        _relative_date(_DAYS_AGO_LEGACY_ARCHIVE),
        "Personal",
        _body(
            "Synthetic legacy action items",
            "Action items tasks todo are retained in this old synthetic archive.",
        ),
    ),
)

# Named records other QA assertions check by filename/date — named here so
# callers never need to reconstruct or hardcode a (relative, non-obvious)
# filename/date themselves.
SYNTHETIC_ML_RECORD = SYNTHETIC_RECORDS[0]
SYNTHETIC_MEETING_DISCUSSION_RECORD = SYNTHETIC_RECORDS[1]
SYNTHETIC_MEETING_SYNC_CALL_RECORD = SYNTHETIC_RECORDS[2]
SYNTHETIC_DAILY_JOURNAL_RECORD = SYNTHETIC_RECORDS[3]
SYNTHETIC_LEGACY_RECORD = SYNTHETIC_RECORDS[-1]


def _load_owned_manifest(instance_root: Path):
    """Load and ownership-verify the manifest for ``instance_root``.

    Reuses ``scripts/test_instance.py``'s own ownership proof — a private
    per-directory sentinel token written at instance-creation time — rather
    than re-deriving a weaker one. This is the same check ``test-instance
    stop``/``verify`` apply before ever acting on a manifest, so a
    directory that merely carries the right name prefix but was never
    created by the real runner (or was recycled by a different process)
    cannot pass.
    """
    from scripts.test_instance import InstanceManifest, UnownedManifestError, _validate_manifest_ownership

    manifest_path = instance_root / "manifest.json"
    try:
        manifest = InstanceManifest.load(manifest_path)
    except (OSError, ValueError, TypeError) as exc:
        pytest.fail(f"owned candidate manifest at {manifest_path} is unreadable: {exc}")
    try:
        _validate_manifest_ownership(manifest_path, manifest)
    except UnownedManifestError as exc:
        pytest.fail(f"candidate manifest ownership check failed: {exc}")
    return manifest


def _owned_candidate_paths(candidate_base_url: str) -> tuple[Path, Path, str]:
    """Return validated candidate vault/Chroma paths and Chroma URL.

    The structural checks below (name-prefix, co-location, loopback host,
    ephemeral port) are a cheap fail-fast layer only — on their own they do
    not bind these paths to any specific candidate, since a stale or
    mismatched environment could coincidentally satisfy all of them while
    actually pointing at a *different* owned instance's vault (one whose
    API happens to pass ``candidate_base_url``'s separate identity check).
    The manifest agreement check afterward is the actual authorization
    gate: it requires the SAME manifest.json — proven owned by this exact
    directory's sentinel token — to describe this exact candidate
    id/token, this exact API base URL, and these exact vault/Chroma paths
    and Chroma URL, before any network call, index operation, or file
    write happens below.
    """

    if os.environ.get("LIFEOS_TEST_INSTANCE") != "1":
        pytest.fail("synthetic QA corpus requires LIFEOS_TEST_INSTANCE=1")

    vault_raw = os.environ.get("LIFEOS_VAULT_PATH", "").strip()
    chroma_raw = os.environ.get("LIFEOS_CHROMA_PATH", "").strip()
    chroma_url = os.environ.get("LIFEOS_CHROMA_URL", "").strip()
    candidate_id = os.environ.get("LIFEOS_TEST_CANDIDATE_ID", "").strip()
    candidate_token = os.environ.get("LIFEOS_TEST_TOKEN", "").strip()
    if not vault_raw or not chroma_raw or not chroma_url or not candidate_id or not candidate_token:
        pytest.fail(
            "synthetic QA corpus requires candidate LIFEOS_VAULT_PATH, "
            "LIFEOS_CHROMA_PATH, LIFEOS_CHROMA_URL, LIFEOS_TEST_CANDIDATE_ID, "
            "and LIFEOS_TEST_TOKEN"
        )

    vault_path = Path(vault_raw).resolve()
    chroma_path = Path(chroma_raw).resolve()
    instance_root = vault_path.parent

    from scripts.test_instance import INSTANCE_ROOT_PREFIX

    if not instance_root.name.startswith(INSTANCE_ROOT_PREFIX):
        pytest.fail("LIFEOS_VAULT_PATH is not under an owned test-instance root")
    if chroma_path.parent != instance_root:
        pytest.fail("LIFEOS_CHROMA_PATH is outside the owned test-instance root")

    parsed = urlsplit(chroma_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        pytest.fail("LIFEOS_CHROMA_URL must target an owned loopback service")
    if parsed.port is None or parsed.port <= 1 or parsed.port in {8000, 8001}:
        pytest.fail("LIFEOS_CHROMA_URL must use an owned ephemeral port")

    manifest = _load_owned_manifest(instance_root)
    expected_base_url = candidate_base_url.rstrip("/")
    mismatches = [
        name
        for name, actual, expected in (
            ("candidate_id", manifest.candidate_id, candidate_id),
            ("token", manifest.token, candidate_token),
            ("base_url", (manifest.base_url or "").rstrip("/"), expected_base_url),
            ("vault_path", str(Path(manifest.vault_path).resolve()), str(vault_path)),
            ("chroma_path", str(Path(manifest.chroma_path).resolve()), str(chroma_path)),
            ("chroma_url", (manifest.chroma_url or "").rstrip("/"), chroma_url.rstrip("/")),
        )
        if actual != expected
    ]
    if mismatches:
        pytest.fail(
            "owned candidate manifest does not agree with the runner's environment "
            f"on: {', '.join(mismatches)} — refusing to index, write, or delete "
            "against a candidate the manifest cannot prove owns these exact paths"
        )

    return vault_path, chroma_path, chroma_url.rstrip("/")


def _require_chroma(chroma_url: str) -> None:
    """Fail clearly when the owned Chroma child is not ready."""

    try:
        response = httpx.get(f"{chroma_url}/api/v2/heartbeat", timeout=5.0)
    except httpx.HTTPError as exc:
        pytest.fail(f"owned candidate Chroma is unreachable: {type(exc).__name__}")
    if response.status_code != 200:
        pytest.fail(f"owned candidate Chroma heartbeat returned {response.status_code}")


def _remove_owned_files(corpus: SyntheticCorpus, seeded_paths: list[Path]) -> None:
    """Remove only files this invocation actually wrote, never a whole vault
    tree and never a file this invocation never created.

    ``seeded_paths`` — the list this same invocation appended to right
    after each successful write — is the only real proof of authorship.
    Content equality alone cannot distinguish a file this run just wrote
    from a preexisting note with identical content (left over from an
    interrupted prior run, or a genuine coincidental duplicate) that this
    run never touched: if seeding aborted partway through (a collision on
    an earlier record), re-deriving "which paths are ours" from the full
    corpus instead of ``seeded_paths`` would delete that untouched
    preexisting file. The content check below is kept only as an
    additional safety net for paths we do know we wrote.
    """
    record_by_path = dict(zip(corpus.file_paths, corpus.records))
    touched_dirs: set[Path] = set()
    for path in seeded_paths:
        record = record_by_path[path]
        try:
            if path.read_text(encoding="utf-8") == record.content:
                path.unlink()
                touched_dirs.add(path.parent)
        except FileNotFoundError:
            pass
        except OSError:
            # Cleanup is best effort; the owned instance removes its private
            # root after the test process exits.
            pass

    # Remove only empty directories that held a file we actually deleted
    # above. Never recurse through the candidate vault or remove a
    # directory this invocation didn't create content in.
    for directory in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


@contextmanager
def seed_synthetic_qa_corpus(candidate_base_url):
    """Seed six synthetic notes into the identified candidate only.

    ``candidate_base_url`` performs the runner's candidate identity/token
    check ahead of any fixture writes, and is also required to
    exactly match the owned instance's own manifest before any write,
    index, or network activity happens (see ``_owned_candidate_paths``).
    The URL is intentionally used for a post-seed smoke search so the
    server-backed route is part of setup.
    """

    # Accessing the fixture performs the runner's identity/token validation.
    vault_path, _chroma_path, chroma_url = _owned_candidate_paths(candidate_base_url)
    _require_chroma(chroma_url)
    corpus = SyntheticCorpus(vault_path=vault_path, records=SYNTHETIC_RECORDS)
    indexer = None
    seeded_paths: list[Path] = []

    try:
        for record, path in zip(corpus.records, corpus.file_paths):
            if path.exists():
                pytest.fail(f"refusing to overwrite candidate file {record.relative_path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(record.content, encoding="utf-8")
            seeded_paths.append(path)

        # Deferred import keeps collection cheap and ensures the app reads the
        # candidate-only settings supplied by the runner at fixture execution.
        from api.services.indexer import IndexerService

        indexer = IndexerService(vault_path=str(vault_path))
        for path in seeded_paths:
            indexer.index_file(
                str(path),
                skip_stats_refresh=True,
                skip_summaries=True,
            )

        # This marker is unique to this fixture and proves the API's real
        # hybrid route can see both indexed stores before any QA assertion runs.
        smoke = httpx.post(
            f"{candidate_base_url}/api/search",
            json={"query": "synthetic test query", "top_k": 5},
            timeout=30.0,
        )
        if smoke.status_code != 200:
            pytest.fail(f"candidate search smoke check returned {smoke.status_code}")
        results = smoke.json().get("results", [])
        if not results:
            pytest.fail("candidate search smoke check returned no seeded results")
        if not any(
            result.get("file_name") == SYNTHETIC_ML_RECORD.file_name
            for result in results
        ):
            pytest.fail("candidate search smoke check missed the seeded ML note")
        for field in ("score", "semantic_score", "recency_score"):
            if field not in results[0]:
                pytest.fail(f"candidate search smoke result lacks {field}")

        yield corpus
    finally:
        if indexer is not None:
            for path in seeded_paths:
                try:
                    indexer.delete_file(str(path))
                except Exception:
                    pass
            indexer.stop()
        _remove_owned_files(corpus, seeded_paths)
