"""Focused, server-free tests for board's pure JavaScript state decisions."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Candidate verification runs lanes on a deliberately narrow PATH, which does
# not reach every layout that ships node (a hosted runner puts it under
# /usr/local/bin, nvm under the user's own prefix). Resolve the interpreter
# from the usual install roots too so this coverage runs wherever node exists.
_NODE_SEARCH_ROOTS = ("/usr/local/bin", "/usr/bin", "/bin", "/opt/homebrew/bin")


def _node_binary() -> str:
    found = shutil.which("node")
    if found:
        return found
    for root in _NODE_SEARCH_ROOTS:
        candidate = Path(root) / "node"
        if candidate.is_file():
            return str(candidate)
    for candidate in sorted(Path("/opt/hostedtoolcache/node").glob("*/*/bin/node"), reverse=True):
        if candidate.is_file():
            return str(candidate)
    raise AssertionError("node is required to exercise the board's pure JS modules")


def _import_test(module: str, assertions: str) -> None:
    script = f"""
      import {{ readFileSync }} from 'node:fs';
      const m = await import('data:text/javascript,' + encodeURIComponent(
        readFileSync({json.dumps(module)}, 'utf8')));
      {assertions}
    """
    result = subprocess.run(
        [_node_binary(), "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_touch_gesture_reserves_horizontal_drag_and_preserves_native_scroll():
    module = str(Path("web/agents/board_gesture.js"))
    _import_test(
        module,
        """
        const touch = { pointerType: 'touch', kind: 'card' };
        if (m.shouldCancelPointerGesture(touch, 20, 1)) throw new Error('held horizontal drag cancelled');
        if (!m.shouldCancelPointerGesture(touch, 1, 20)) throw new Error('vertical touch scroll was not cancelled');
        if (m.shouldCancelPointerGesture(touch, 1, 1)) throw new Error('touch slop too small');
        """,
    )


def test_pointer_identity_and_desktop_direction_rules():
    module = str(Path("web/agents/board_gesture.js"))
    _import_test(
        module,
        """
        const state = { pointerId: 7, pointerType: 'touch', holdReady: true, kind: 'card' };
        if (m.pointerIsActive(state, { pointerId: 8, isPrimary: true })) throw new Error('foreign pointer accepted');
        if (m.pointerIsActive(state, { pointerId: 7, isPrimary: false })) throw new Error('secondary pointer accepted');
        if (!m.pointerIsActive(state, { pointerId: 7, isPrimary: true })) throw new Error('active pointer rejected');
        if (m.shouldCancelPointerGesture({ pointerType: 'mouse', kind: 'assignee' }, 1, 20)) throw new Error('desktop tray behavior changed');
        if (!m.shouldCancelPointerGesture({ pointerType: 'mouse', kind: 'card' }, 1, 20)) throw new Error('desktop card scroll behavior changed');
        """,
    )


def test_graph_accept_uses_the_full_panel_close_path():
    source = Path("web/agents/graph.js").read_text(encoding="utf-8")
    callback = source.split("onCardAccepted:", 1)[1].split("onLabelSaved:", 1)[0]
    assert "closePanel();" in callback
    assert "panel.close();" not in callback


def test_mixed_card_sort_reverses_direction_with_deterministic_ties():
    module = str(Path("web/agents/board_sort.js"))
    _import_test(
        module,
        """
        const cards = [
          { kind: 'schedule', id: 's1', next_fire_at: '2026-09-12T00:00:00Z' },
          { kind: 'task', id: 't2', created_date: '2026-09-11', updated_at: '2026-09-13T00:00:00Z' },
          { kind: 'task', id: 't1', created_date: '2026-09-10', updated_at: '2026-09-14T00:00:00Z' },
        ];
        const ids = mode => m.sortCards(cards, mode).map(card => card.id).join(',');
        if (ids('created_asc') !== 't1,t2,s1') throw new Error(ids('created_asc'));
        if (ids('created_desc') !== 's1,t2,t1') throw new Error(ids('created_desc'));
        if (ids('modified_asc') !== 's1,t2,t1') throw new Error(ids('modified_asc'));
        if (ids('modified_desc') !== 't1,t2,s1') throw new Error(ids('modified_desc'));
        const tied = [{ kind: 'task', id: 'b', created_date: '2026-09-10' }, { kind: 'task', id: 'a', created_date: '2026-09-10' }];
        if (m.sortCards(tied, 'created_asc').map(card => card.id).join(',') !== 'a,b') throw new Error('asc tie');
        if (m.sortCards(tied, 'created_desc').map(card => card.id).join(',') !== 'b,a') throw new Error('desc tie');
        const storage = { value: null, getItem() { return this.value; }, setItem(_, value) { this.value = value; } };
        const options = new Set(['created_asc', 'created_desc']);
        if (m.loadSortSelection(storage, 'sort', options, 'created_desc') !== 'created_desc') throw new Error('default persistence');
        m.saveSortSelection(storage, 'sort', 'created_asc');
        if (m.loadSortSelection(storage, 'sort', options, 'created_desc') !== 'created_asc') throw new Error('saved sort');
        storage.value = 'unsupported';
        if (m.loadSortSelection(storage, 'sort', options, 'created_desc') !== 'created_desc') throw new Error('invalid persisted sort');
        """,
    )


def test_undo_failure_refreshes_without_replacing_original_error():
    module = str(Path("web/agents/action_refresh.js"))
    _import_test(
        module,
        """
        let refreshed = 0;
        await m.refreshAfterFailure(async () => { refreshed += 1; throw new Error('refresh failed'); });
        if (refreshed !== 1) throw new Error('refresh was not attempted');
        let untouched = 0;
        await m.refreshAfterFailure(null);
        if (untouched !== 0) throw new Error('null callback invoked');
        """,
    )
