"""Tests for the vault taxonomy loader and the bootstrap script."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from api.services.vault_taxonomy import (
    DEFAULT_TAXONOMY_PATH,
    TaxonomyError,
    load_taxonomy,
    reset_taxonomy_cache,
)

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import taxonomy_bootstrap  # noqa: E402

pytestmark = pytest.mark.unit

BASE = """
version: "1"
doc_types: [journal, reference]
domains: [work, health]
project_sources: [tasks_projects]
topics:
  - name: work/hiring
    description: Recruiting.
  - name: health/fitness
    description: Workouts.
"""


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_taxonomy_cache()
    yield
    reset_taxonomy_cache()


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_committed_taxonomy_is_valid_and_generic():
    tax = load_taxonomy(DEFAULT_TAXONOMY_PATH)
    assert len(tax.doc_types) == 12
    assert len(tax.domains) == 8
    assert 40 <= len(tax.topics) <= 50
    assert all(name.split("/")[0] in tax.domains for name in tax.topics)
    assert tax.project_sources
    assert len(tax.vocab_version) == 12


def test_valid_load(tmp_path):
    tax = load_taxonomy(_write(tmp_path, "t.yaml", BASE))
    assert tax.version == "1"
    assert tax.doc_types == ("journal", "reference")
    assert tax.topics["work/hiring"] == "Recruiting."
    assert tax.project_sources == ("tasks_projects",)


def test_override_add_replace_remove(tmp_path):
    base = _write(tmp_path, "t.yaml", BASE)
    local = _write(tmp_path, "local.yaml", """
doc_types: [field_report]
domains: [hobbies]
topics:
  - name: hobbies/gardening
    description: Plants.
  - name: work/hiring
    description: Replaced.
remove:
  topics: [health/fitness]
  doc_types: [journal]
""")
    tax = load_taxonomy(base, local)
    assert tax.doc_types == ("reference", "field_report")
    assert "hobbies" in tax.domains
    assert tax.topics["hobbies/gardening"] == "Plants."
    assert tax.topics["work/hiring"] == "Replaced."
    assert "health/fitness" not in tax.topics


def test_override_removing_a_used_domain_fails(tmp_path):
    base = _write(tmp_path, "t.yaml", BASE)
    local = _write(tmp_path, "local.yaml", "remove:\n  domains: [health]\n")
    with pytest.raises(TaxonomyError, match="not a declared domain"):
        load_taxonomy(base, local)


@pytest.mark.parametrize("facet,line", [
    ("doc_types", "doc_types: [journal, journal]"),
    ("domains", "domains: [work, work]"),
])
def test_duplicate_values_rejected(tmp_path, facet, line):
    text = BASE.replace(next(ln for ln in BASE.splitlines() if ln.startswith(facet)), line)
    with pytest.raises(TaxonomyError, match=f"duplicate values in '{facet}'"):
        load_taxonomy(_write(tmp_path, "t.yaml", text))


def test_duplicate_topics_rejected(tmp_path):
    text = BASE + "  - name: work/hiring\n    description: Again.\n"
    with pytest.raises(TaxonomyError, match="duplicate values in 'topics'"):
        load_taxonomy(_write(tmp_path, "t.yaml", text))


@pytest.mark.parametrize("name", ["hiring", "work/", "work/a/b"])
def test_invalid_topic_form_rejected(tmp_path, name):
    text = BASE + f"  - name: {name}\n    description: Bad.\n"
    with pytest.raises(TaxonomyError, match="parent/child"):
        load_taxonomy(_write(tmp_path, "t.yaml", text))


def test_unknown_parent_rejected(tmp_path):
    text = BASE + "  - name: finance/budget\n    description: Bad.\n"
    with pytest.raises(TaxonomyError, match="not a declared domain"):
        load_taxonomy(_write(tmp_path, "t.yaml", text))


def test_vocab_version_stability(tmp_path):
    v1 = load_taxonomy(_write(tmp_path, "a.yaml", BASE)).vocab_version
    reordered = """
project_sources:   [tasks_projects]
topics:
  - description: Workouts.
    name: health/fitness
  - description: Recruiting.
    name: work/hiring


domains: [health, work]
doc_types: [reference, journal]
version: "1"
"""
    assert load_taxonomy(_write(tmp_path, "b.yaml", reordered)).vocab_version == v1
    changed = BASE.replace("Recruiting.", "Recruiting people.")
    assert load_taxonomy(_write(tmp_path, "c.yaml", changed)).vocab_version != v1
    added = BASE.replace("[journal, reference]", "[journal, reference, log]")
    assert load_taxonomy(_write(tmp_path, "d.yaml", added)).vocab_version != v1


def test_override_changes_version_and_cache_resets(tmp_path):
    base = _write(tmp_path, "t.yaml", BASE)
    plain = load_taxonomy(base).vocab_version
    local = _write(tmp_path, "local.yaml", "doc_types: [log]\n")
    assert load_taxonomy(base, local).vocab_version != plain
    base.write_text(BASE.replace("Workouts.", "Training."), encoding="utf-8")
    assert load_taxonomy(base).vocab_version == plain  # cached
    reset_taxonomy_cache()
    assert load_taxonomy(base).vocab_version != plain


class _StubClient:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    def create(self, messages, max_tokens):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(text=self.text)


def _make_vault(root):
    (root / "Work").mkdir(parents=True)
    (root / "Personal").mkdir()
    (root / ".obsidian").mkdir()
    for i in range(6):
        (root / "Work" / f"note{i}.md").write_text(
            "---\ntags: [example-tag, planning]\n---\nQuarterly planning body.", encoding="utf-8")
    (root / "Personal" / "day.md").write_text("Plain journal entry.", encoding="utf-8")
    (root / ".obsidian" / "hidden.md").write_text("ignore me", encoding="utf-8")
    return root


def test_bootstrap_with_stub_llm(tmp_path):
    vault = _make_vault(tmp_path / "vault")
    reply = """```yaml
topics:
  - name: work/planning
    description: Planning notes.
  - name: bogus_domain/thing
    description: Unknown parent.
  - name: nodash
    description: Malformed.
```"""
    client = _StubClient(reply)
    out = tmp_path / "data" / "taxonomy_proposal.yaml"
    result = taxonomy_bootstrap.run(vault, 3, client=client, output=out)
    assert result["topics"] == 1
    assert sorted(result["dropped"]) == ["bogus_domain/thing", "nodash"]
    assert yaml.safe_load(out.read_text())["topics"] == [
        {"name": "work/planning", "description": "Planning notes."}
    ]
    prompt = client.prompts[0]
    assert "example-tag" in prompt and "Work" in prompt and "Personal" in prompt
    assert "hidden" not in prompt
    # Stratified: every top-level folder is represented.
    assert result["folders"] == 2 and result["sampled"] >= 2


def test_bootstrap_dry_run_makes_no_call_and_writes_nothing(tmp_path):
    vault = _make_vault(tmp_path / "vault")
    out = tmp_path / "proposal.yaml"
    client = _StubClient("unused")
    result = taxonomy_bootstrap.run(vault, 200, dry_run=True, client=client, output=out)
    assert result["sampled"] == 7
    assert result["prompt_tokens_estimate"] > 0
    assert not client.prompts and not out.exists()


def test_bootstrap_unusable_reply_writes_nothing(tmp_path):
    vault = _make_vault(tmp_path / "vault")
    out = tmp_path / "proposal.yaml"
    with pytest.raises(SystemExit):
        taxonomy_bootstrap.run(vault, 5, client=_StubClient("not yaml: [["), output=out)
    assert not out.exists()
