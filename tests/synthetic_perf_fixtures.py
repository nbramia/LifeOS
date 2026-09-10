"""Candidate-owned synthetic fixtures for the perf benchmark's task/person
categories.

Seeds one synthetic task through the running candidate API's own
``POST /api/tasks`` route — not a second, separately-constructed
``TaskManager`` — because ``TaskManager``'s on-disk query-index cache is
loaded into memory once by the server's ``get_task_manager()`` singleton;
a write through any other instance would never appear in that singleton's
in-memory task list, no matter which files it touched. Going through the
real route exercises the exact singleton ``manage_tasks`` reads from and
avoids a second, redundant redirect of the index-cache path.

Seeds one synthetic person directly via ``PersonEntityStore``, redirected
off the immutable source snapshot into this instance's own external data
directory by ``scripts/_test_instance_bootstrap.py``'s
``_install_perf_provider_fixtures``. Unlike tasks, this is safe to write
directly: the server only constructs its own ``PersonEntityStore``/
``EntityResolver`` singletons lazily, on the first ``person_info`` tool
call, so as long as this fixture seeds before that first call, the
singleton's first read is a fresh one straight from the (already-written)
sqlite row — there is no separate in-memory cache to go stale.

Reuses ``synthetic_qa_corpus.py``'s manifest-agreement gate rather than
re-deriving it, and removes only the exact records this invocation
created.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.synthetic_qa_corpus import _load_owned_manifest, _owned_candidate_paths

SYNTHETIC_TASK_TRIGGER = "[synthetic-perf-tasks]"
# Task descriptions can't contain "]" (TaskManager._validate_text_fields
# rejects it — it would truncate an inline `[key:: value]` field on
# reindex), so the marker embedded in the description itself is
# bracket-free. It must match the literal scripts/test_instance.py's
# _StubLLMHandler checks for in the real manage_tasks tool result.
SYNTHETIC_TASK_MARKER = "synthetic-perf-tasks-marker"
SYNTHETIC_TASK_DESCRIPTION = f"{SYNTHETIC_TASK_MARKER}: verify benchmark checklist"
SYNTHETIC_PERSON_NAME = "Perf Bench Contact"
SYNTHETIC_PERSON_COMPANY = "Synthetic Benchmarks Inc"
SYNTHETIC_PERSON_EMAIL = "perf-bench-contact@example.com"


@dataclass(frozen=True)
class SyntheticPerfFixtures:
    task_id: str
    person_id: str


@contextmanager
def seed_synthetic_perf_fixtures(candidate_base_url: str):
    """Seed one synthetic task and one synthetic person into the identified
    candidate only. The manifest-agreement gate runs first, before any
    write, exactly as it does for the QA vault corpus."""
    vault_path, _chroma_path, _chroma_url = _owned_candidate_paths(candidate_base_url)
    manifest = _load_owned_manifest(vault_path.parent)

    create_resp = httpx.post(
        f"{candidate_base_url}/api/tasks",
        json={"description": SYNTHETIC_TASK_DESCRIPTION, "context": "Inbox"},
        timeout=30.0,
    )
    if create_resp.status_code != 200:
        pytest.fail(f"failed to seed synthetic perf-benchmark task: {create_resp.status_code} {create_resp.text}")
    task_id = create_resp.json()["id"]

    from api.services.person_entity import PersonEntity, PersonEntityStore

    # Computed independently here (both derive it from manifest.instance_dir)
    # rather than importing the server's already-patched class attribute,
    # since this test process never runs the server's own bootstrap patch.
    crm_db_path = Path(manifest.instance_dir) / "data" / "crm.db"
    person_store = PersonEntityStore(db_path=str(crm_db_path))
    person = PersonEntity(
        canonical_name=SYNTHETIC_PERSON_NAME,
        company=SYNTHETIC_PERSON_COMPANY,
        emails=[SYNTHETIC_PERSON_EMAIL],
        sources=["synthetic-perf-fixture"],
    )
    added = person_store.add(person)
    if added is None:
        httpx.delete(f"{candidate_base_url}/api/tasks/{task_id}", timeout=30.0)
        pytest.fail("refused to seed the synthetic perf-benchmark person (blocklisted?)")

    try:
        yield SyntheticPerfFixtures(task_id=task_id, person_id=added.id)
    finally:
        httpx.delete(f"{candidate_base_url}/api/tasks/{task_id}", timeout=30.0)
        person_store.delete(added.id)
