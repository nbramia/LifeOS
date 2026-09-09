"""Focused ownership/authorization tests for tests/synthetic_qa_corpus.py.

These exercise ``_owned_candidate_paths``' manifest-agreement gate and
``_remove_owned_files``' seeded-paths-only cleanup directly — both are pure
filesystem/env logic once a manifest object exists on disk, so none of this
needs a live server, a real Chroma process, or network access. The one
exception (``_require_chroma``'s heartbeat GET) is stubbed only where a test
needs to reach past it to exercise later behavior; no assertion in this file
is weakened by that stub.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.test_instance import InstanceManifest
from tests.synthetic_qa_corpus import (
    SYNTHETIC_RECORDS,
    SyntheticCorpus,
    SyntheticRecord,
    _owned_candidate_paths,
    _remove_owned_files,
    seed_synthetic_qa_corpus,
)

pytestmark = pytest.mark.unit

Failed = pytest.fail.Exception


def _write_manifest(instance_dir: Path, **overrides) -> InstanceManifest:
    instance_dir.mkdir(parents=True, exist_ok=True)
    defaults = dict(
        token="tok-123",
        candidate_id="cand-123",
        source_root=str(instance_dir),
        snapshot_root=str(instance_dir),
        instance_dir=str(instance_dir),
        api_port=1,
        base_url="http://127.0.0.1:9999",
        vault_path=str(instance_dir / "vault"),
        chroma_path=str(instance_dir / "chroma"),
        data_dir=str(instance_dir / "data"),
        log_path=str(instance_dir / "log.txt"),
        llm_stub_url=None,
        parent_pid=os.getpid(),
        parent_start_time=0.0,
        child_pid=None,
        child_start_time=None,
        created_at=0.0,
        chroma_url="http://127.0.0.1:9998",
    )
    defaults.update(overrides)
    manifest = InstanceManifest(**defaults)
    manifest.save_atomic(instance_dir / "manifest.json")
    (instance_dir / ".owner").write_text(manifest.token)
    return manifest


@pytest.fixture
def owned_env(tmp_path, monkeypatch):
    """A fully-agreeing owned instance: env vars, manifest, and sentinel all
    describe the exact same candidate/paths/URLs."""
    instance_dir = tmp_path / "lifeos-test-instance-abc123"
    vault_path = instance_dir / "vault"
    chroma_path = instance_dir / "chroma"
    vault_path.mkdir(parents=True)
    chroma_path.mkdir(parents=True)
    manifest = _write_manifest(instance_dir, vault_path=str(vault_path), chroma_path=str(chroma_path))

    monkeypatch.setenv("LIFEOS_TEST_INSTANCE", "1")
    monkeypatch.setenv("LIFEOS_VAULT_PATH", str(vault_path))
    monkeypatch.setenv("LIFEOS_CHROMA_PATH", str(chroma_path))
    monkeypatch.setenv("LIFEOS_CHROMA_URL", manifest.chroma_url)
    monkeypatch.setenv("LIFEOS_TEST_CANDIDATE_ID", manifest.candidate_id)
    monkeypatch.setenv("LIFEOS_TEST_TOKEN", manifest.token)
    return manifest


# ---------------------------------------------------------------------------
# _owned_candidate_paths: manifest agreement is the actual authorization
# gate, not the structural (prefix/co-location/loopback) checks alone.
# ---------------------------------------------------------------------------

def test_owned_candidate_paths_accepts_full_agreement(owned_env):
    vault, chroma, chroma_url = _owned_candidate_paths(owned_env.base_url)
    assert vault == Path(owned_env.vault_path).resolve()
    assert chroma == Path(owned_env.chroma_path).resolve()
    assert chroma_url == owned_env.chroma_url.rstrip("/")


def test_owned_candidate_paths_rejects_base_url_mismatch(owned_env):
    """candidate_base_url authenticates the API, but on its own says
    nothing about which vault/Chroma the fixture is about to touch — a
    different (also-real) candidate's URL must still be refused."""
    with pytest.raises(Failed, match="base_url"):
        _owned_candidate_paths("http://127.0.0.1:1111")


def test_owned_candidate_paths_rejects_vault_path_mismatch(owned_env, monkeypatch):
    """A directory that structurally looks owned (right prefix, co-located
    with chroma) but isn't what the owned manifest actually records as the
    vault must still be refused — the prefix/co-location checks alone
    cannot catch this."""
    other_vault = Path(owned_env.instance_dir) / "vault-not-in-manifest"
    other_vault.mkdir()
    monkeypatch.setenv("LIFEOS_VAULT_PATH", str(other_vault))
    with pytest.raises(Failed, match="vault_path"):
        _owned_candidate_paths(owned_env.base_url)


def test_owned_candidate_paths_rejects_chroma_path_mismatch(owned_env, monkeypatch):
    other_chroma = Path(owned_env.instance_dir) / "chroma-not-in-manifest"
    other_chroma.mkdir()
    monkeypatch.setenv("LIFEOS_CHROMA_PATH", str(other_chroma))
    with pytest.raises(Failed, match="chroma_path"):
        _owned_candidate_paths(owned_env.base_url)


def test_owned_candidate_paths_rejects_chroma_url_mismatch(owned_env, monkeypatch):
    monkeypatch.setenv("LIFEOS_CHROMA_URL", "http://127.0.0.1:9997")  # a different owned-looking port
    with pytest.raises(Failed, match="chroma_url"):
        _owned_candidate_paths(owned_env.base_url)


def test_owned_candidate_paths_rejects_candidate_id_mismatch(owned_env, monkeypatch):
    monkeypatch.setenv("LIFEOS_TEST_CANDIDATE_ID", "a-different-candidate")
    with pytest.raises(Failed, match="candidate_id"):
        _owned_candidate_paths(owned_env.base_url)


def test_owned_candidate_paths_rejects_token_mismatch(owned_env, monkeypatch):
    """Distinct from the sentinel check: the sentinel on disk still proves
    this invocation owns the directory, but the environment's claimed token
    must also agree with what the manifest itself records."""
    monkeypatch.setenv("LIFEOS_TEST_TOKEN", "a-different-token")
    with pytest.raises(Failed, match="token"):
        _owned_candidate_paths(owned_env.base_url)


def test_owned_candidate_paths_rejects_missing_manifest(tmp_path, monkeypatch):
    instance_dir = tmp_path / "lifeos-test-instance-nomanifest"
    vault_path = instance_dir / "vault"
    chroma_path = instance_dir / "chroma"
    vault_path.mkdir(parents=True)
    chroma_path.mkdir(parents=True)
    monkeypatch.setenv("LIFEOS_TEST_INSTANCE", "1")
    monkeypatch.setenv("LIFEOS_VAULT_PATH", str(vault_path))
    monkeypatch.setenv("LIFEOS_CHROMA_PATH", str(chroma_path))
    monkeypatch.setenv("LIFEOS_CHROMA_URL", "http://127.0.0.1:9998")
    monkeypatch.setenv("LIFEOS_TEST_CANDIDATE_ID", "cand-x")
    monkeypatch.setenv("LIFEOS_TEST_TOKEN", "tok-x")
    with pytest.raises(Failed, match="unreadable"):
        _owned_candidate_paths("http://127.0.0.1:9999")


def test_owned_candidate_paths_rejects_forged_sentinel(owned_env):
    """Even with every field otherwise agreeing, a sentinel that doesn't
    match the manifest's own token means this directory cannot be proven
    to have been created by the real runner (e.g. a recycled directory)."""
    (Path(owned_env.instance_dir) / ".owner").write_text("a-different-token")
    with pytest.raises(Failed, match="ownership"):
        _owned_candidate_paths(owned_env.base_url)


def test_owned_candidate_paths_rejects_missing_candidate_id_env(owned_env, monkeypatch):
    monkeypatch.delenv("LIFEOS_TEST_CANDIDATE_ID", raising=False)
    with pytest.raises(Failed, match="LIFEOS_TEST_CANDIDATE_ID"):
        _owned_candidate_paths(owned_env.base_url)


def test_owned_candidate_paths_rejects_missing_token_env(owned_env, monkeypatch):
    monkeypatch.delenv("LIFEOS_TEST_TOKEN", raising=False)
    with pytest.raises(Failed, match="LIFEOS_TEST_TOKEN"):
        _owned_candidate_paths(owned_env.base_url)


# ---------------------------------------------------------------------------
# _remove_owned_files: cleanup deletes only paths THIS invocation seeded.
# ---------------------------------------------------------------------------

def test_remove_owned_files_deletes_only_seeded_paths_not_a_preexisting_identical_note(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    records = (
        SyntheticRecord("a.md", "2026-01-01", "Work", "seeded content\n"),
        SyntheticRecord("b.md", "2026-01-02", "Work", "identical content\n"),
    )
    corpus = SyntheticCorpus(vault_path=vault, records=records)
    seeded_path, preexisting_path = corpus.file_paths
    seeded_path.write_text(records[0].content, encoding="utf-8")
    # Never written by this invocation, but byte-identical to what it would
    # have written -- e.g. left over from an interrupted prior run.
    preexisting_path.write_text(records[1].content, encoding="utf-8")

    _remove_owned_files(corpus, [seeded_path])

    assert not seeded_path.exists(), "the actually-seeded file must be removed"
    assert preexisting_path.exists(), "a file this invocation never wrote must survive even with identical content"


def test_remove_owned_files_no_op_when_nothing_was_seeded(tmp_path):
    """The abort-before-any-write case: an empty seeded_paths list must
    delete nothing, even if a preexisting file with matching name/content
    already exists in the vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    records = (SyntheticRecord("a.md", "2026-01-01", "Work", "content\n"),)
    corpus = SyntheticCorpus(vault_path=vault, records=records)
    preexisting = corpus.file_paths[0]
    preexisting.write_text(records[0].content, encoding="utf-8")

    _remove_owned_files(corpus, [])

    assert preexisting.exists(), "nothing may be deleted when this invocation seeded nothing"


def test_remove_owned_files_removes_directory_only_for_a_deleted_path(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    records = (SyntheticRecord("Work/a.md", "2026-01-01", "Work", "content\n"),)
    corpus = SyntheticCorpus(vault_path=vault, records=records)
    path = corpus.file_paths[0]
    path.parent.mkdir(parents=True)
    path.write_text(records[0].content, encoding="utf-8")

    _remove_owned_files(corpus, [path])

    assert not path.exists()
    assert not path.parent.exists(), "the now-empty directory this invocation created must be removed"


def test_remove_owned_files_keeps_content_check_even_for_a_seeded_path(tmp_path):
    """Even a path this invocation seeded is only deleted if its content
    still matches what was written — a defense-in-depth safety net on top
    of, not a substitute for, the seeded-paths ownership proof."""
    vault = tmp_path / "vault"
    vault.mkdir()
    records = (SyntheticRecord("a.md", "2026-01-01", "Work", "original content\n"),)
    corpus = SyntheticCorpus(vault_path=vault, records=records)
    path = corpus.file_paths[0]
    path.write_text("content changed by someone else\n", encoding="utf-8")

    _remove_owned_files(corpus, [path])

    assert path.exists(), "a modified file must not be blindly deleted even if seeded"


# ---------------------------------------------------------------------------
# seed_synthetic_qa_corpus: the full entry point, proving the gate actually
# runs before any network/index/write/delete activity.
# ---------------------------------------------------------------------------

def test_seed_synthetic_qa_corpus_writes_nothing_on_manifest_mismatch(owned_env):
    """A mismatched candidate_base_url must abort before any file is
    written to the vault, before Chroma is contacted, and before the
    indexer or the search smoke-check ever runs."""
    vault = Path(owned_env.vault_path)
    with pytest.raises(Failed, match="base_url"):
        with seed_synthetic_qa_corpus("http://127.0.0.1:1111"):
            pytest.fail("must never enter the seeded context")

    assert list(vault.iterdir()) == [], "no file may be written when the manifest disagrees"


def test_seed_synthetic_qa_corpus_collision_cleans_up_only_this_runs_own_writes(owned_env, monkeypatch):
    """If seeding aborts partway through because a later record's file
    already exists, cleanup must remove only the earlier records THIS
    invocation actually wrote — never a preexisting file with identical
    content it never wrote at all, which the old content-only cleanup
    check would have wrongly deleted."""
    monkeypatch.setattr(
        "tests.synthetic_qa_corpus.httpx.get",
        lambda *a, **k: SimpleNamespace(status_code=200),
    )

    vault = Path(owned_env.vault_path)
    first_record, second_record = SYNTHETIC_RECORDS[0], SYNTHETIC_RECORDS[1]
    first_path = vault / first_record.relative_path
    second_path = vault / second_record.relative_path
    second_path.parent.mkdir(parents=True, exist_ok=True)
    second_path.write_text(second_record.content, encoding="utf-8")

    with pytest.raises(Failed, match="refusing to overwrite"):
        with seed_synthetic_qa_corpus(owned_env.base_url):
            pytest.fail("must never enter the seeded context")

    assert not first_path.exists(), "the record actually written before the collision must be cleaned up"
    assert second_path.exists(), "the untouched preexisting colliding file must survive cleanup"
    assert second_path.read_text(encoding="utf-8") == second_record.content
