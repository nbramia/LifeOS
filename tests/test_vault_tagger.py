"""VaultTagger: gating matrix, topic thresholds, error fallback (synthetic notes, mocked Jev)."""
import json
from unittest.mock import patch

import pytest

from api.services.jev_client import JevClient, JevError
from api.services.vault_tag_store import VaultTagStore
from api.services.vault_tagger import VaultTagger, classify_sensitivity, is_allowlisted
from api.services.vault_taxonomy import load_taxonomy
from config.settings import settings

pytestmark = pytest.mark.unit

TX = load_taxonomy()
TOPIC_A, TOPIC_B, TOPIC_C, TOPIC_D = list(TX.topics)[:4]
DOC = TX.doc_types[0]
DOMAIN = TX.domains[0]


def _choice(probs):
    best = max(probs, key=probs.get)
    return {"choice": best, "probabilities": probs, "confidence": probs[best]}


def _answers(topic_probs=None):
    topic_probs = topic_probs or {TOPIC_A: 0.9, TOPIC_B: 0.1}
    return {
        "doc_type": _choice({DOC: 0.8, TX.doc_types[1]: 0.2}),
        "domain": _choice({DOMAIN: 0.7, TX.domains[1]: 0.3}),
        "topic": _choice(topic_probs),
        "project": _choice({"Alpha": 0.9, "none": 0.1}),
        "actionability": {"score": 1.2},
        "has_decision": {"noul": 0.4},
    }


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Work" / "Alpha").mkdir(parents=True)
    (root / "Notes").mkdir()
    (root / "Therapy").mkdir()
    (root / "Work" / "Alpha" / "plan.md").write_text("---\ntags: [x]\n---\n# Plan\nWe decided to ship.\n")
    (root / "Notes" / "idea.md").write_text("# Idea\nSome text.\n")
    (root / "Therapy" / "session.md").write_text("# Session\nPrivate text.\n")
    (root / "Notes" / "tagged.md").write_text("---\ntags: [therapy]\n---\nBody\n")
    return root


def _set(monkeypatch, mode="on", paths="*", key="k"):
    monkeypatch.setattr(settings, "jev_vault_tagging", mode)
    monkeypatch.setattr(settings, "jev_vault_tag_paths", paths)
    monkeypatch.setattr(settings, "typesafe_api_key", key)


def _tagger(vault, store=None):
    return VaultTagger(store=store, client=JevClient(api_key="k"), taxonomy=TX, vault_root=vault)


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
@pytest.mark.parametrize("allowlisted", [False, True])
@pytest.mark.parametrize("restricted", [False, True])
@pytest.mark.parametrize("key", ["", "k"])
def test_gating_matrix(vault, monkeypatch, mode, allowlisted, restricted, key):
    _set(monkeypatch, mode=mode, paths="Notes,Therapy" if allowlisted else "Elsewhere", key=key)
    rel = "Therapy/session.md" if restricted else "Notes/idea.md"
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(vault / rel)
    should_call = mode != "off" and allowlisted and not restricted and key == "k"
    assert ask.called is should_call
    assert rec.backend == ("jev" if should_call else "code")
    assert rec.sensitivity == ("restricted" if restricted else "private")
    if not should_call:
        assert rec.doc_type is None and rec.topic is None and rec.content_sha256


def test_default_settings_send_nothing(vault):
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(vault / "Notes" / "idea.md")
    assert not ask.called and rec.backend == "code"


def test_allowlist_prefixes_and_star():
    with patch.object(settings, "jev_vault_tag_paths", "Work, Notes/Sub"):
        assert is_allowlisted("Work/Alpha/x.md")
        assert is_allowlisted("Notes/Sub/x.md")
        assert not is_allowlisted("Workshop/x.md")
        assert not is_allowlisted("Notes/x.md")
    with patch.object(settings, "jev_vault_tag_paths", "*"):
        assert is_allowlisted("Anything/x.md")
    with patch.object(settings, "jev_vault_tag_paths", ""):
        assert not is_allowlisted("Work/x.md")


def test_restricted_by_human_tag(vault, monkeypatch):
    _set(monkeypatch)
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(vault / "Notes" / "tagged.md")
    assert not ask.called and rec.sensitivity == "restricted"
    assert classify_sensitivity("Notes/x.md", ["Therapy"]) == "restricted"


def test_jev_facets_and_question_set(vault, monkeypatch):
    _set(monkeypatch)
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(vault / "Work" / "Alpha" / "plan.md")
    state, questions = ask.call_args.args
    assert set(questions) == {"doc_type", "domain", "topic", "project", "actionability", "has_decision"}
    assert "none" in questions["project"]["criteria"] and "Alpha" in questions["project"]["criteria"]
    assert set(state) == {"path", "title", "frontmatter", "headings", "body"}
    assert state["path"] == "Work/Alpha/plan.md" and state["headings"] == ["# Plan"]
    assert rec.doc_type == DOC and rec.domain == DOMAIN and rec.project == "Alpha"
    assert rec.topic == TOPIC_A and rec.topic_conf == 0.9
    assert rec.actionability == 1.2 and rec.has_decision == 0.4 and rec.backend == "jev"
    assert json.loads(rec.topics_json)["topic"][TOPIC_A] == 0.9


def test_body_capped_at_8000_tokens(vault, monkeypatch):
    _set(monkeypatch)
    big = vault / "Notes" / "big.md"
    big.write_text("word " * 30000)
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        _tagger(vault).tag_file(big)
    from api.services.chunker import count_tokens

    assert count_tokens(ask.call_args.args[0]["body"]) <= 8000


def test_secondary_topics_threshold_and_cap(vault, monkeypatch):
    _set(monkeypatch)
    probs = {TOPIC_A: 0.5, TOPIC_B: 0.2, TOPIC_C: 0.16, TOPIC_D: 0.14}
    with patch.object(JevClient, "ask", return_value=_answers(probs)):
        rec = _tagger(vault).tag_file(vault / "Notes" / "idea.md")
    assert json.loads(rec.topics_json)["secondary"] == [TOPIC_B, TOPIC_C]
    probs = {TOPIC_A: 0.6, TOPIC_B: 0.15, TOPIC_C: 0.14, TOPIC_D: 0.11}
    with patch.object(JevClient, "ask", return_value=_answers(probs)):
        rec = _tagger(vault).tag_file(vault / "Notes" / "idea.md")
    assert json.loads(rec.topics_json)["secondary"] == [TOPIC_B]


def test_low_topic_confidence_stores_parent_only(vault, monkeypatch):
    _set(monkeypatch)
    with patch.object(JevClient, "ask", return_value=_answers({TOPIC_A: 0.59, TOPIC_B: 0.41})):
        rec = _tagger(vault).tag_file(vault / "Notes" / "idea.md")
    assert rec.topic == TOPIC_A.split("/")[0] and rec.topic_conf == 0.59
    with patch.object(JevClient, "ask", return_value=_answers({TOPIC_A: 0.6, TOPIC_B: 0.4})):
        rec = _tagger(vault).tag_file(vault / "Notes" / "idea.md")
    assert rec.topic == TOPIC_A


def test_project_none_maps_to_null(vault, monkeypatch):
    _set(monkeypatch)
    answers = _answers()
    answers["project"] = _choice({"none": 0.9, "Alpha": 0.1})
    with patch.object(JevClient, "ask", return_value=answers):
        rec = _tagger(vault).tag_file(vault / "Notes" / "idea.md")
    assert rec.project is None


def test_error_falls_back_to_code_record(vault, monkeypatch):
    _set(monkeypatch)
    with patch.object(JevClient, "ask", side_effect=JevError("status 500")):
        rec = _tagger(vault).tag_file(vault / "Notes" / "idea.md")
    assert rec.backend == "code" and rec.doc_type is None and rec.file_path == "Notes/idea.md"


def test_error_returns_previous_record(vault, monkeypatch, tmp_path):
    _set(monkeypatch)
    store = VaultTagStore(str(tmp_path / "tags.db"))
    with patch.object(JevClient, "ask", return_value=_answers()):
        first = _tagger(vault, store).tag_file(vault / "Notes" / "idea.md")
    store.upsert(first)
    (vault / "Notes" / "idea.md").write_text("# Idea\nChanged.\n")
    with patch.object(JevClient, "ask", side_effect=RuntimeError("boom")):
        rec = _tagger(vault, store).tag_file(vault / "Notes" / "idea.md")
    assert rec == first and rec.backend == "jev"


def test_unreadable_file_never_raises(vault, monkeypatch):
    _set(monkeypatch)
    rec = _tagger(vault).tag_file(vault / "Notes" / "gone.md")
    assert rec.backend == "code"


def _lifelog(vault):
    (vault / "Lifelogs").mkdir()
    (vault / "Lifelogs" / "day.md").write_text("# Day\nText.\n")
    return vault / "Lifelogs" / "day.md"


def test_default_restricted_paths_include_lifelogs(vault, monkeypatch):
    _set(monkeypatch)
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(_lifelog(vault))
    assert not ask.called and rec.sensitivity == "restricted"


def test_empty_restricted_paths_sends_allowlisted_lifelogs(vault, monkeypatch):
    _set(monkeypatch)
    monkeypatch.setattr(settings, "jev_vault_restricted_paths", "")
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(_lifelog(vault))
    assert ask.called and rec.backend == "jev" and rec.sensitivity == "private"


def test_tag_restriction_survives_empty_restricted_paths(vault, monkeypatch):
    _set(monkeypatch)
    monkeypatch.setattr(settings, "jev_vault_restricted_paths", "")
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(vault / "Notes" / "tagged.md")
    assert not ask.called and rec.sensitivity == "restricted"


def test_restricted_prefix_entry_matches_by_path(monkeypatch):
    monkeypatch.setattr(settings, "jev_vault_restricted_paths", "Personal/Diary")
    assert classify_sensitivity("Personal/Diary/a.md", []) == "restricted"
    assert classify_sensitivity("Personal/Other/a.md", []) == "private"
    assert classify_sensitivity("Diary/a.md", []) == "private"


def _send(vault, monkeypatch, rel):
    _set(monkeypatch)
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(vault / rel)
    return ask, rec


def test_symlink_to_outside_vault_is_never_sent(vault, monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# Secret\nText.\n")
    (vault / "Work" / "external.md").symlink_to(outside / "secret.md")
    ask, rec = _send(vault, monkeypatch, "Work/external.md")
    assert not ask.called and rec.backend == "code" and rec.sensitivity == "restricted"


def test_symlink_into_restricted_folder_is_restricted(vault, monkeypatch):
    (vault / "Work" / "alias.md").symlink_to(vault / "Therapy" / "session.md")
    ask, rec = _send(vault, monkeypatch, "Work/alias.md")
    assert not ask.called and rec.sensitivity == "restricted"


def test_restricted_lexical_path_to_plain_target_is_restricted(vault, monkeypatch):
    (vault / "Therapy" / "alias.md").symlink_to(vault / "Notes" / "idea.md")
    ask, rec = _send(vault, monkeypatch, "Therapy/alias.md")
    assert not ask.called and rec.sensitivity == "restricted"


def test_self_referencing_symlink_never_raises(vault, monkeypatch):
    (vault / "Notes" / "loop.md").symlink_to(vault / "Notes" / "loop.md")
    ask, rec = _send(vault, monkeypatch, "Notes/loop.md")
    assert not ask.called and rec.backend == "code"


def test_malformed_frontmatter_is_restricted(vault, monkeypatch):
    (vault / "Notes" / "bad.md").write_text("---\ntags: [private]\nkey: [unclosed\n---\nBody\n")
    ask, rec = _send(vault, monkeypatch, "Notes/bad.md")
    assert not ask.called and rec.sensitivity == "restricted"


def test_unclosed_frontmatter_fence_is_restricted(vault, monkeypatch):
    (vault / "Notes" / "open.md").write_text("---\ntags: [private]\n\nBody with no closing fence\n")
    ask, rec = _send(vault, monkeypatch, "Notes/open.md")
    assert not ask.called and rec.sensitivity == "restricted"


def test_bom_before_frontmatter_still_reads_tags(vault, monkeypatch):
    (vault / "Notes" / "bom.md").write_text("\ufeff---\ntags: [private]\n---\nBody\n", encoding="utf-8")
    ask, rec = _send(vault, monkeypatch, "Notes/bom.md")
    assert not ask.called and rec.sensitivity == "restricted"


@pytest.mark.parametrize("body", [
    "Body with #Therapy tag\n",
    "Body #private/session here\n",
    "#finance\n",
])
def test_inline_restricted_tags(vault, monkeypatch, body):
    (vault / "Notes" / "inline.md").write_text("# Title\n" + body)
    ask, rec = _send(vault, monkeypatch, "Notes/inline.md")
    assert not ask.called and rec.sensitivity == "restricted"


def test_nested_frontmatter_tag_is_restricted(vault, monkeypatch):
    (vault / "Notes" / "nested.md").write_text("---\ntags: [Private/Session]\n---\nBody\n")
    ask, rec = _send(vault, monkeypatch, "Notes/nested.md")
    assert not ask.called and rec.sensitivity == "restricted"


def test_headings_and_code_blocks_are_not_tags(vault, monkeypatch):
    (vault / "Notes" / "ok.md").write_text("# Private\n## Therapy notes\n```\n#private\n```\nx#therapy\n")
    ask, rec = _send(vault, monkeypatch, "Notes/ok.md")
    assert ask.called and rec.sensitivity == "private"


def test_body_cap_without_tokenizer_is_bytes(vault, monkeypatch):
    from api.services import chunker

    monkeypatch.setattr(chunker, "TOKENIZER", None)
    (vault / "Notes" / "cjk.md").write_text("漢" * 32000)
    _set(monkeypatch)
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        _tagger(vault).tag_file(vault / "Notes" / "cjk.md")
    body = ask.call_args.args[0]["body"]
    assert 0 < len(body.encode("utf-8")) <= 8000


def test_allowlist_applies_to_symlink_target(vault, monkeypatch):
    _set(monkeypatch, paths="Work")
    (vault / "Work" / "alias.md").symlink_to(vault / "Notes" / "idea.md")
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(vault / "Work" / "alias.md")
    assert not ask.called and rec.backend == "code"


def test_dotdot_segments_are_never_sent(vault, monkeypatch):
    _set(monkeypatch, paths="Work,Notes")
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(vault / "Work" / ".." / "Notes" / "idea.md")
    assert not ask.called and rec.backend == "code"
    assert not is_allowlisted("Work/../Notes/idea.md")


@pytest.mark.parametrize("entry", ["./Work/Private", "Work//Private", " 'work/private/' ", "WORK/PRIVATE"])
def test_restricted_entry_is_normalized(monkeypatch, entry):
    monkeypatch.setattr(settings, "jev_vault_restricted_paths", entry)
    assert classify_sensitivity("Work/Private/session.md", []) == "restricted"
    assert classify_sensitivity("Work/Privateer/session.md", []) == "private"
    assert classify_sensitivity("Other/Work/Private/session.md", []) == "private"


@pytest.mark.parametrize("value", ["/", "''", ",,", " , "])
def test_restricted_value_without_valid_entry_falls_back_to_default(monkeypatch, value):
    monkeypatch.setattr(settings, "jev_vault_restricted_paths", value)
    assert classify_sensitivity("Lifelogs/day.md", []) == "restricted"


def test_whitespace_only_restricted_value_disables_path_restriction(monkeypatch):
    monkeypatch.setattr(settings, "jev_vault_restricted_paths", "   ")
    assert classify_sensitivity("Lifelogs/day.md", []) == "private"


def test_dot_dot_dot_frontmatter_closer_is_restricted(vault, monkeypatch):
    (vault / "Notes" / "dots.md").write_text("---\ntags: [private]\n...\nbody\n")
    ask, rec = _send(vault, monkeypatch, "Notes/dots.md")
    assert not ask.called and rec.sensitivity == "restricted"


def test_parser_dropping_a_raw_tags_key_is_restricted(vault, monkeypatch):
    import frontmatter

    (vault / "Notes" / "drop.md").write_text("---\ntags: [private]\n---\nbody\n")
    with patch("api.services.vault_tagger.frontmatter.loads", return_value=frontmatter.Post("body")):
        ask, rec = _send(vault, monkeypatch, "Notes/drop.md")
    assert not ask.called and rec.sensitivity == "restricted"


def test_empty_tags_key_with_matching_parse_is_not_restricted(vault, monkeypatch):
    (vault / "Notes" / "fine.md").write_text("---\ntitle: x\ntags: [work]\n---\nbody\n")
    ask, rec = _send(vault, monkeypatch, "Notes/fine.md")
    assert ask.called and rec.sensitivity == "private"


def _note(vault, name, tag, inline=False):
    text = f"# N\nbody #{tag}\n" if inline else f"---\ntags: [{tag}]\n---\nbody\n"
    (vault / "Notes" / name).write_text(text)
    return vault / "Notes" / name


@pytest.mark.parametrize("tag", ["finance", "therapy"])
@pytest.mark.parametrize("inline", [False, True])
def test_restricted_tags_setting_sends_notes_outside_the_list(vault, monkeypatch, tag, inline):
    _set(monkeypatch)
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", "private,confidential")
    note = _note(vault, "t.md", tag, inline)
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(note)
    assert ask.called and rec.backend == "jev" and rec.sensitivity == "private"


def test_restricted_tags_setting_keeps_nested_children_restricted(vault, monkeypatch):
    _set(monkeypatch)
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", "private,confidential")
    note = _note(vault, "s.md", "private/session")
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(note)
    assert not ask.called and rec.sensitivity == "restricted"


def test_restricted_tags_are_case_insensitive_and_normalized(monkeypatch):
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", " 'Private/' , ./Work ")
    assert classify_sensitivity("Notes/a.md", ["PRIVATE"]) == "restricted"
    assert classify_sensitivity("Notes/a.md", ["work/x"]) == "restricted"
    assert classify_sensitivity("Notes/a.md", ["finance"]) == "private"


def test_empty_restricted_tags_disables_tag_restriction(monkeypatch):
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", "  ")
    assert classify_sensitivity("Notes/a.md", ["therapy", "private"]) == "private"


@pytest.mark.parametrize("value", [",,", "''", " , ", "/"])
def test_restricted_tags_without_valid_entry_use_default_and_warn(monkeypatch, caplog, value):
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", value)
    with caplog.at_level("WARNING"):
        assert classify_sensitivity("Notes/a.md", ["finance"]) == "restricted"
    assert sum("RESTRICTED_TAGS" in r.message for r in caplog.records) == 1


def test_unset_restricted_tags_default_matches_built_in_list(monkeypatch):
    for tag in ("therapy", "private", "finance", "confidential", "private/session"):
        assert classify_sensitivity("Notes/a.md", [tag]) == "restricted"


@pytest.mark.parametrize("note_tag", ["straße", "STRASSE", "Straße/child"])
@pytest.mark.parametrize("inline", [False, True])
def test_restricted_tag_folding_is_shared_by_setting_and_note_tags(vault, monkeypatch, note_tag, inline):
    _set(monkeypatch)
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", "straße")
    note = _note(vault, "fold.md", note_tag, inline)
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(note)
        assert not _tagger(vault).would_send(note)
    assert not ask.called and rec.sensitivity == "restricted"


NON_ASCII_START_TAGS = ["1-1", "_private", "équipe"]


@pytest.mark.parametrize("tag", NON_ASCII_START_TAGS)
@pytest.mark.parametrize("inline", [False, True])
def test_restricted_tag_not_starting_with_ascii_letter_blocks_both_forms(vault, monkeypatch, tag, inline):
    _set(monkeypatch)
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", tag)
    note = _note(vault, "odd.md", tag, inline)
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        tagger = _tagger(vault)
        assert not tagger.would_send(note)
        rec = tagger.tag_file(note)
    assert not ask.called and rec.backend == "code" and rec.sensitivity == "restricted"


def test_purely_numeric_hash_is_not_a_tag(vault, monkeypatch):
    _set(monkeypatch)
    monkeypatch.setattr(settings, "jev_vault_restricted_tags", "1-1,123")
    note = vault / "Notes" / "num.md"
    note.write_text("# N\nissue #123 and `#1-1` and page#1-1 here\n")
    with patch.object(JevClient, "ask", return_value=_answers()) as ask:
        rec = _tagger(vault).tag_file(note)
    assert ask.called and rec.sensitivity == "private"
