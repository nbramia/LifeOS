"""Tests for the host-tool write guard (api/services/host_write_guard.py), the
approval command check (host_tools.check_approval_argv) and the approval
gate's limits (host_approval.py).

All data is synthetic. Tests run the real TaskManager and SchedulerStore on
tmp files; no command is ever executed.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import PurePosixPath

import pytest

from api.services import agent_board as ab
from api.services import host_approval as ha
from api.services import host_tools
from api.services import host_write_guard as g
from api.services import task_manager as tm
from api.services.host_tools import HostToolError, HostToolsConfig

pytestmark = pytest.mark.unit

TASKS = PurePosixPath("LifeOS/Tasks/Inbox.md")
SCHED = PurePosixPath("LifeOS/Scheduler/Inbox.md")
WRITE_DIRS = "Assistant,LifeOS/Tasks/Inbox.md,LifeOS/Scheduler/Inbox.md"
PICKUP = set(ab.AGENT_PICKUP_TAGS)
EXCLUDED = {"agent-running", "agent-blocked", "agent-completed", "agent-failed", "agent-budget-exceeded"}
STATUSES = {"todo", "urgent"}


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw.get("cwd")))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")


@pytest.fixture
def roots(tmp_path):
    vault = tmp_path / "Synthetic Vault"
    (vault / "Assistant").mkdir(parents=True)
    (vault / "Assistant" / "x.txt").write_text("synthetic\n")
    (vault / "LifeOS" / "Tasks").mkdir(parents=True)
    (vault / "LifeOS" / "Scheduler").mkdir(parents=True)
    lifeos = tmp_path / "lifeos"
    (lifeos / "logs").mkdir(parents=True)
    return {"vault": vault, "scratch": tmp_path / "scratch", "lifeos": lifeos, "logs": lifeos / "logs"}


@pytest.fixture
def config(roots, tmp_path):
    return HostToolsConfig(roots=roots, run_as="lifeos-tools", vault_autocommit=False,
                           audit_path=tmp_path / "audit.jsonl", runner=FakeRunner(),
                           vault_write_dirs=host_tools.parse_vault_write_dirs(WRITE_DIRS))


@pytest.fixture
def run_as_ok(monkeypatch):
    monkeypatch.setattr(host_tools, "check_run_as", lambda user: user)


def write(config, rel, content, **extra):
    return host_tools.fs_write(config, {"root": "vault", "path": str(rel), "content": content, **extra})


def picked_up_after(roots, tmp_path, old: str, new: str) -> list:
    """Write `new` straight to a tmp task file (no guard), reindex it with the
    real task store, and return what the worker would pick up. Shows that a
    proposed write the guard refuses would otherwise start agent work."""
    p = roots["vault"] / TASKS
    p.write_text(old)
    mgr = task_manager(roots, tmp_path)
    mgr.reindex_file(str(p))
    assert worker_candidates(mgr) == []
    p.write_text(new)
    mgr.reindex_file(str(p))
    return worker_candidates(mgr)


def task_manager(roots, tmp_path):
    return tm.TaskManager(vault_path=roots["vault"], index_path=tmp_path / "tasks_index.json",
                          live_session_checker=lambda *a: False,
                          live_coordinator_checker=lambda *a: False)


def worker_candidates(mgr):
    """The worker's pickup filter, applied to the task index."""
    out = []
    for t in mgr._tasks.values():
        tags = {x.lstrip("#").lower() for x in t.tags}
        if t.status in STATUSES and tags & PICKUP and not tags & EXCLUDED:
            out.append(t)
    return out


# ── task files: tags are checked per task ────────────────────────────────

def test_a_tag_moved_from_a_note_onto_a_new_task_is_refused(config, roots, tmp_path):
    p = roots["vault"] / TASKS
    original = "- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n    > buy from a #local shop\n"
    proposed = ("- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n"
                "    > buy from a shop\n"
                "- [ ] TODO Synthetic new job #local\n")
    assert [t.description for t in picked_up_after(roots, tmp_path, original, proposed)] == ["Synthetic new job"]
    p.write_text(original)
    with pytest.raises(HostToolError, match="local"):
        write(config, TASKS, proposed)
    assert p.read_text() == original


def test_a_tag_moved_from_a_heading_onto_a_new_task_is_refused():
    old = "# Notes on #claude\n- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n"
    new = "# Notes\n- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n- [ ] TODO Synthetic new #claude\n"
    assert "claude" in g.check_vault_write(TASKS, old, new)


def test_a_tag_moved_from_a_task_onto_a_non_task_line_is_refused():
    old = "- [ ] TODO Synthetic errand #me <!-- id:aaaa1111 -->\n"
    new = "#me\n- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n"
    assert g.check_vault_write(TASKS, old, new) is not None


def test_an_nfkc_spelling_cannot_be_swapped_for_the_real_tag():
    assert g.check_vault_write(TASKS, "note #ｈｕｍａｎ\n", "- [ ] TODO Synthetic new #human\n") is not None
    old = "- [ ] TODO Synthetic errand #ｈｕｍａｎ <!-- id:aaaa1111 -->\n"
    assert g.check_vault_write(TASKS, old, old.replace("#ｈｕｍａｎ", "#human")) is not None


def test_unfencing_a_fenced_engine_task_is_refused(config, roots, tmp_path):
    original = "# Inbox\n```\n- [ ] TODO Synthetic example #claude\n```\n"
    proposed = "# Inbox\n- [ ] TODO Synthetic new instructions #claude\n"
    assert len(picked_up_after(roots, tmp_path, original, proposed)) == 1
    p = roots["vault"] / TASKS
    p.write_text(original)
    with pytest.raises(HostToolError, match="claude"):
        write(config, TASKS, proposed)
    assert p.read_text() == original


def test_fenced_examples_can_still_be_edited_without_gaining_tags():
    old = "# Inbox\n```\n- [ ] TODO Synthetic example #claude\n```\n"
    assert g.check_vault_write(TASKS, old, old.replace("example", "sample")) is None
    assert g.check_vault_write(TASKS, old, old + "```\n#claude\n```\n") is not None


def test_a_task_without_an_id_yet_that_carries_a_tag_is_left_alone():
    old = "- [ ] TODO Synthetic protected #human\n"
    assert g.check_vault_write(TASKS, old, "- [ ] TODO Synthetic changed #human\n") is not None
    assert g.check_vault_write(TASKS, old, "#human\n") is not None
    assert g.check_vault_write(TASKS, old, "") is not None
    # unchanged, wherever it sits in the file, it is fine
    assert g.check_vault_write(TASKS, old, "# Inbox\n" + old + "- [ ] TODO Synthetic plain\n") is None


def test_plain_tasks_without_an_id_can_still_be_edited():
    old = "- [ ] TODO Synthetic plain\n"
    assert g.check_vault_write(TASKS, old, "- [ ] TODO Synthetic plain, edited\n") is None


def test_a_routing_word_in_notes_prose_leaves_the_task_editable():
    old = "- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n    > Buy from a #local shop\n"
    assert g.check_vault_write(TASKS, old, old.replace("Synthetic errand", "Synthetic edited")) is None
    assert g.check_vault_write(TASKS, old, old.replace("- [ ]", "- [x]")) is None
    assert g.check_vault_write(TASKS, old, old.replace("Buy from a #local shop", "Buy from a shop")) is None
    assert g.check_vault_write(TASKS, old, old + "    > second line of the note\n") is None


def test_notes_cannot_gain_a_routing_tag():
    old = "- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n    > a note\n"
    assert "claude" in g.check_vault_write(TASKS, old, old.replace("a note", "ask #claude"))
    note = "- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n    > Buy from a #local shop\n"
    assert "claude" in g.check_vault_write(TASKS, note, note.replace("#local", "#claude"))


def test_notes_never_route_a_task_in_the_store(roots, tmp_path):
    """Why a tag in the notes is prose: the store reads routing from the line."""
    note = "- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n    > Buy from a #local shop\n"
    assert picked_up_after(roots, tmp_path, "# Inbox\n", note) == []


def test_only_markdown_files_in_the_tasks_folder_are_guarded(config, roots):
    write(config, PurePosixPath("Assistant/x.txt"), "x\n")  # ensure writes work at all
    assert g.check_vault_write(PurePosixPath("LifeOS/Tasks/example.txt"), "", "- [ ] Example #claude\n") is None
    assert g.check_vault_write(PurePosixPath("LifeOS/Tasks/example.md"), "", "- [ ] Example #claude\n")


# ── task files: ids ──────────────────────────────────────────────────────

def test_a_decoy_repeating_an_owned_tasks_id_is_refused(config, roots, tmp_path):
    p = roots["vault"] / TASKS
    owned = "- [/] Synthetic running job #claude #agent-running <!-- id:aaaa1111 -->\n"
    p.write_text(owned)
    with pytest.raises(HostToolError, match="more than once"):
        write(config, TASKS, "- [ ] TODO Synthetic decoy <!-- id:aaaa1111 -->\n" + owned)
    mgr = task_manager(roots, tmp_path)
    mgr.reindex_file(str(p))
    assert "claude" in mgr.get("aaaa1111").tags


def test_an_owned_task_is_found_the_way_the_store_finds_it():
    """With duplicates already in the file, the first block owns the id."""
    old = ("- [ ] TODO Synthetic protected #human <!-- id:aaaa1111 -->\n"
           "- [ ] TODO Synthetic decoy <!-- id:aaaa1111 -->\n")
    assert g.check_vault_write(TASKS, old, old.replace("protected", "changed")) is not None
    removed = "- [ ] TODO Synthetic decoy <!-- id:aaaa1111 -->\n#human\n"
    assert g.check_vault_write(TASKS, old, removed) is not None
    # dropping the store's re-id'd duplicate is an ordinary edit
    assert g.check_vault_write(TASKS, old, old.splitlines()[0] + "\n") is None


# ── task files: fields ───────────────────────────────────────────────────

@pytest.mark.parametrize("field", [
    "operation_key:: s0000001:2026-12-01T14:00:00+00:00", "working_dir:: /tmp/x", "host:: synthetic-host",
    "model:: synthetic-model", "persona_id:: synthetic", "assigned_by:: board", "effort:: high",
    "budget:: 5", "occurrence_key:: k", "parent_id:: aaaa1111",
])
def test_fields_cannot_be_added_to_new_or_existing_tasks(field):
    old = "# Inbox\n- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n"
    key = field.split("::")[0]
    added = old.replace(" <!-- id:aaaa1111", f" [{field}] <!-- id:aaaa1111")
    assert key in g.check_vault_write(TASKS, old, added)
    assert key in g.check_vault_write(TASKS, old, old + f"- [ ] TODO Synthetic new [{field}]\n")
    assert key in g.check_vault_write(TASKS, old, old + f"- [ ] TODO Synthetic new [{field}] <!-- id:bbbb2222 -->\n")


def test_a_repeated_field_key_cannot_override_an_existing_value(roots, tmp_path):
    old = "- [ ] TODO Synthetic errand [host:: synthetic-good] <!-- id:aaaa1111 -->\n"
    new = old.replace("[host:: synthetic-good]", "[host:: synthetic-bad] [host:: synthetic-good]")
    assert "host" in g.check_vault_write(TASKS, old, new)
    swapped = old.replace("[host:: synthetic-good]", "[host:: synthetic-good] [host:: synthetic-bad]")
    assert "host" in g.check_vault_write(TASKS, old, swapped)
    p = roots["vault"] / TASKS
    p.write_text(swapped)  # unguarded: the store keeps the last value
    mgr = task_manager(roots, tmp_path)
    mgr.reindex_file(str(p))
    assert mgr.get("aaaa1111").fields["host"] == "synthetic-bad"


def test_existing_fields_cannot_be_changed_or_dropped_but_ride_along_with_edits():
    old = "- [ ] TODO Synthetic errand [host:: synthetic-a] <!-- id:aaaa1111 -->\n"
    assert "host" in g.check_vault_write(TASKS, old, old.replace("synthetic-a", "synthetic-b"))
    assert "host" in g.check_vault_write(TASKS, old, old.replace(" [host:: synthetic-a]", ""))
    assert g.check_vault_write(TASKS, old, old.replace("errand", "errand, edited")) is None
    assert g.check_vault_write(TASKS, old, old.replace("errand", "errand [due:: 2026-12-01]")) is None


def test_an_operation_key_squat_is_refused(config, roots, tmp_path):
    from api.services.agent_worker.session_store import SessionStore

    key = SessionStore.occurrence_key_for("s0000001", "2026-12-01T14:00:00+00:00")
    p = roots["vault"] / TASKS
    proposed = f"# Inbox\n- [ ] TODO Synthetic squatter [operation_key:: {key}]\n"
    p.write_text(proposed)  # unguarded: the squatter would take the occurrence
    mgr = task_manager(roots, tmp_path)
    mgr.reindex_file(str(p))
    _, created = mgr.create_or_find_by_operation(key, description="synthetic prompt", tags=["local"])
    assert created is False
    p.write_text("# Inbox\n")
    with pytest.raises(HostToolError, match="operation_key"):
        write(config, TASKS, proposed)
    assert p.read_text() == "# Inbox\n"


def test_execution_fields_on_an_unowned_task_are_refused(config, roots):
    p = roots["vault"] / TASKS
    p.write_text("- [ ] TODO Synthetic errand <!-- id:aaaa1111 -->\n")
    with pytest.raises(HostToolError):
        write(config, TASKS,
              "- [ ] TODO Synthetic errand [host:: synthetic-host] [working_dir:: /tmp/x] "
              "[persona_id:: synthetic] [model:: synthetic-model] [assigned_by:: board] <!-- id:aaaa1111 -->\n"
              "    > synthetic instructions\n")


# ── schedule inbox ───────────────────────────────────────────────────────

OKAY = "- [ ] Synthetic reminder [cron:: 0 9 * * *] [action:: notify] <!-- id:s0000001 -->\n    > hello\n"
EVIL = ("- [ ] Synthetic job [cron:: * * * * *] [action:: agent] #local <!-- id:s0000001 -->\n"
        "    > synthetic prompt\n")


def test_a_schedule_id_cannot_repeat(config, roots):
    p = roots["vault"] / SCHED
    p.write_text(OKAY)
    for content in (EVIL + OKAY, OKAY + OKAY, OKAY.replace("hello", "bye") + OKAY):
        with pytest.raises(HostToolError, match="more than once"):
            write(config, SCHED, content)
    assert p.read_text() == OKAY


@pytest.mark.parametrize("old", [
    "- [ ] Synthetic daily [cron:: 0 9 * * *] [action:: agent] #local\n    > synthetic prompt\n",
    "- [ ] Synthetic daily [cron:: 0 9 * * *] [action:: notify]\n    > hello\n",
])
def test_a_schedule_without_an_id_cannot_be_removed(old):
    assert "removed" in g.check_vault_write(SCHED, old, "")
    assert "removed" in g.check_vault_write(SCHED, old + OKAY, OKAY)
    assert g.check_vault_write(SCHED, old, "# Schedules\n" + old + OKAY) is None


def test_a_static_schedule_without_an_id_can_be_edited_while_it_stays_static():
    old = "- [ ] Synthetic reminder [cron:: 0 9 * * *] [action:: notify]\n    > hello\n"
    assert g.check_vault_write(SCHED, old, old.replace("Synthetic reminder", "Synthetic daily reminder")) is None
    assert g.check_vault_write(SCHED, old, old.replace("hello", "stretch")) is None
    for bad in ("[action:: agent] #local", "[action:: prompt]", "[action:: notify] [mtype:: prompt]",
                "[action:: notify] [endpoint:: POST /api/x]", "[action:: notify] [working_dir:: /tmp]"):
        assert "static" in g.check_vault_write(SCHED, old, old.replace("[action:: notify]", bad))


def test_an_executable_schedule_without_an_id_stays_unchanged():
    old = "- [ ] Synthetic daily [cron:: 0 9 * * *] [action:: agent] #local\n    > synthetic prompt\n"
    assert g.check_vault_write(SCHED, old, old.replace("synthetic prompt", "another prompt")) is not None


# ── approval: commands that are never sent ───────────────────────────────

@pytest.mark.parametrize("argv", [
    ["vim", "-es", "-c", "!id", "-c", "q"],
    ["vi", "Assistant/x.txt"],
    ["sqlite3", "Assistant/x.txt", ".shell id"],
    ["gdb", "-batch", "-ex", "shell id"],
    ["crontab", "Assistant/x.txt"],
    ["at", "now"],
    ["batch"],
    ["less", "Assistant/x.txt"],
    ["man", "ls"],
    ["nano", "Assistant/x.txt"],
    ["emacs", "Assistant/x.txt"],
    ["openssl", "s_client", "-connect", "synthetic.example:443"],
    ["dig", "synthetic.example"],
    ["docker", "ps"],
    ["systemctl", "--user", "start", "x"],
    ["python3.12", "-V"],
    ["timeout", "1", "id"],
    ["git-upload-pack", "."],
    ["uptime", "--exec=id"],
    ["sort", "--compress-prog=id", "Assistant/x.txt"],
    ["uptime", "--exe=id"],
])
def test_exec_scheduler_editor_and_network_programs_are_never_sent(config, roots, argv):
    assert host_tools.check_approval_argv(config, argv, roots["vault"].resolve()) is not None


def test_a_command_is_matched_by_the_program_it_resolves_to(config, roots, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real = bin_dir / "vim.basic"
    real.write_text("")
    (bin_dir / "edit-alias").symlink_to(real)
    monkeypatch.setattr(host_tools, "_RESOLVE_PATH", str(bin_dir))
    why = host_tools.check_approval_argv(config, ["edit-alias", "Assistant/x.txt"], roots["vault"].resolve())
    assert why and "never run through approval" in why


@pytest.mark.parametrize("argv", [
    ["git", "config", "--global", "filter.syn.clean", "id -u"],
    ["git", "config", "--global", "core.attributesFile", "Assistant/x.txt"],
    ["git", "-c", "core.pager=id", "log"],
    ["git", "archive", "--remote=git://synthetic.example/v.git", "HEAD"],
    ["git", "archive", "--remote=.", "--exec=id", "HEAD"],
    ["git", "fetch-pack", "--upload-pack=id", "."],
    ["git", "send-pack", "--receive-pack=id", ".", "HEAD"],
    ["git", "imap-send"],
    ["git", "clone", "x"], ["git", "fetch"], ["git", "pull"], ["git", "push"],
    ["git", "ls-remote", "."], ["git", "remote", "-v"], ["git", "submodule", "foreach", "id"],
    ["git", "synthetic-alias"],
    ["git", "rebase", "-x", "id", "HEAD~1"],
    ["git", "grep", "-Oid", "x"],
    ["git", "bisect", "run", "id"],
    ["git", "rebase", "-xid", "HEAD"], ["git", "rebase", "--exec=id", "HEAD"],
    ["git", "rebase", "--exe=id", "HEAD"], ["git", "rebase", "-i", "HEAD~1"],
    ["git", "commit"], ["git", "commit", "--edit", "-m", "x"], ["git", "commit", "-m", "x"],
    ["git", "merge", "x"], ["git", "tag", "-a", "v1"], ["git", "revert", "HEAD"],
    ["git", "cherry-pick", "x"], ["git", "notes", "add"], ["git", "am"],
    ["git", "branch", "--edit-description"], ["git", "add", "-e"], ["git", "add", "-ue"],
    ["git", "archive", "--exe=id", "--remot=.", "HEAD"], ["git", "grep", "--open-files=id", "x"],
    ["git", "rev-list", "--filter=blob:none", "HEAD"],
])
def test_git_that_reaches_out_or_runs_programs_is_never_sent(config, roots, argv):
    assert host_tools.check_approval_argv(config, argv, roots["vault"].resolve()) is not None


@pytest.mark.parametrize("argv", [
    ["cat", "Assistant/x.txt"], ["wc", "-l", "Assistant/x.txt"], ["uptime"], ["df", "-h"],
    ["git", "branch", "-a"], ["git", "rev-parse", "HEAD"], ["git", "archive", "HEAD"],
    ["git", "add", "-u"], ["git", "grep", "-e", "x"], ["sort", "--reverse", "Assistant/x.txt"],
])
def test_ordinary_commands_can_still_be_sent(config, roots, argv):
    assert host_tools.check_approval_argv(config, argv, roots["vault"].resolve()) is None


# ── approval: gate limits ────────────────────────────────────────────────

class FakeSend:
    def __init__(self):
        self.n = 100
        self.sent = []

    def __call__(self, text):
        self.n += 1
        self.sent.append(text)
        return [self.n]


def _gate(tmp_path):
    return ha.ApprovalGate(timeout_seconds=3600, chat_id="c1", store=ha.ApprovalStore(tmp_path / "ap.db"),
                           send=FakeSend(), clock=lambda: 1_000_000.0)


def test_concurrent_requests_never_pass_the_pending_limit(tmp_path):
    gate = _gate(tmp_path)
    barrier = threading.Barrier(6)
    out = []

    def ask(i):
        barrier.wait(timeout=5)
        out.append(gate.check(caller="oauth:x", command_hash=f"h{i}", details="d").outcome)

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out.count("pending") == ha.MAX_PENDING_PER_CALLER
    assert gate.store.count_pending("oauth:x", now=1_000_000.0) == ha.MAX_PENDING_PER_CALLER


def test_the_store_refuses_past_the_limit_in_one_transaction(tmp_path):
    store = ha.ApprovalStore(tmp_path / "ap.db")
    ids = [store.create(caller="c", command_hash=f"h{i}", chat_id="c1", expires_at=None,
                        now=1.0, max_pending=2) for i in range(3)]
    assert ids[0] and ids[1] and ids[2] is None


def test_an_approval_is_bound_to_the_resolved_directory(config, roots, tmp_path, run_as_ok):
    gate = _gate(tmp_path)
    config.approval_enabled, config.approvals = True, gate
    argv = ["cp", "Assistant/x.txt", "Assistant/y.txt"]
    assert host_tools.shell(config, {"argv": argv, "cwd_root": "vault"}, caller="oauth:x")["rule"] == "pending-approval"
    assert gate.store.decide_by_reply(gate.send.n, "APPROVE", chat_id="c1", now=1_000_000.0) is not None
    original, other = config.roots["vault"], tmp_path / "other vault"
    (other / "Assistant").mkdir(parents=True)
    (other / "Assistant" / "x.txt").write_text("synthetic\n")
    config.roots["vault"] = other
    assert host_tools.shell(config, {"argv": argv, "cwd_root": "vault"}, caller="oauth:x")["rule"] == "pending-approval"
    assert config.runner.calls == []
    config.roots["vault"] = original
    assert host_tools.shell(config, {"argv": argv, "cwd_root": "vault"}, caller="oauth:x")["rule"] == "approved"


def test_approved_git_runs_with_editor_and_pager_turned_off(config, roots, tmp_path, run_as_ok):
    gate = _gate(tmp_path)
    config.approval_enabled, config.approvals = True, gate
    argv = ["git", "branch", "-a"]
    host_tools.shell(config, {"argv": argv, "cwd_root": "vault"}, caller="oauth:x")
    gate.store.decide_by_reply(gate.send.n, "APPROVE", chat_id="c1", now=1_000_000.0)
    assert host_tools.shell(config, {"argv": argv, "cwd_root": "vault"}, caller="oauth:x")["rule"] == "approved"
    command = config.runner.calls[-1][0]
    run = command[command.index("--") + 1:]
    assert run[0] == "git" and run[-2:] == ["branch", "-a"]
    assert "core.editor=false" in run and "sequence.editor=false" in run and "core.pager=cat" in run


# ── file handling ────────────────────────────────────────────────────────

def test_binary_content_anywhere_in_the_read_is_refused(config, roots):
    (roots["vault"] / "Assistant" / "b.txt").write_bytes(b"a" * 8192 + b"\x00\xff")
    with pytest.raises(HostToolError, match="binary"):
        host_tools.fs_read(config, {"root": "vault", "path": "Assistant/b.txt"})


def test_append_returns_the_whole_files_hash_and_writes_atomically(config, roots, monkeypatch):
    p = PurePosixPath("Assistant/n.md")
    write(config, p, "one\n")
    written = []
    real = host_tools.atomic_write_text
    monkeypatch.setattr(host_tools, "atomic_write_text", lambda path, text: (written.append(text), real(path, text)))
    r = write(config, p, "two\n", mode="append")
    assert written == ["one\ntwo\n"]
    assert (roots["vault"] / p).read_text() == "one\ntwo\n"
    write(config, p, "three\n", expected_sha256=r["sha256"])


def test_append_to_a_file_that_is_not_utf8_is_refused(config, roots):
    path = roots["vault"] / "Assistant" / "latin.txt"
    path.write_bytes(b"caf\xe9\n")
    with pytest.raises(HostToolError, match="not UTF-8"):
        write(config, PurePosixPath("Assistant/latin.txt"), "x\n", mode="append")
    assert path.read_bytes() == b"caf\xe9\n"

