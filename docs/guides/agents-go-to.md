# /agents "Go To" — WezTerm setup

> **Status:** Complete
> **Last Updated:** 2026-05-28
> **Audience:** Operators

The /agents page's **Go To** button (and double-clicking an active node) jumps wezterm focus to the pane running the selected Claude Code session — even when several panes are working in the same project directory at once.

Setup is one block in `~/.claude/settings.json` plus making sure `lsof` is installed.

---

## How it works in one paragraph

LifeOS keeps a SQLite table mapping `cc:<session_id> → wezterm pane_id` (`data/cc_wezterm.db`). It gets populated two ways. **Resume** writes a row when it opens a new tab. **The SessionStart hook below** writes a row every time `claude` starts inside a wezterm pane. When the cache misses (e.g. session predates the hook install), `/api/agents/sessions/<id>/focus` falls back to a fast probe: `lsof` finds which process holds the session's transcript file open, and the holder's controlling TTY is matched against `wezterm cli list --format json`'s `tty_name`. Cwd alone cannot disambiguate when multiple panes share a project; the transcript file can.

---

## 1. Verify prerequisites

Both `lsof` (for the probe) and `wezterm` (everywhere) must be on PATH. On a standard Linux desktop both are typically pre-installed.

```bash
command -v lsof wezterm jq curl
```

If anything is missing, install via your package manager. `jq` and `curl` are only needed by the hook script — the probe fallback works without them.

Confirm the `LIFEOS_CC_RESUME_ENABLED` flag is on (Go To is gated by the same flag as Resume):

```bash
grep LIFEOS_CC_RESUME_ENABLED .env
```

Set it to `true` and restart the API if needed (`./scripts/server.sh restart`).

---

## 2. Install the SessionStart hook

Add the following to `~/.claude/settings.json` (create the file if it doesn't exist):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/LifeOS/scripts/claude-session-pane.sh"
          }
        ]
      }
    ]
  }
}
```

Replace the path with the absolute path to your LifeOS checkout. The script:

- No-ops silently when `$WEZTERM_PANE` is unset (non-wezterm terminal).
- Reads the SessionStart JSON payload from stdin (`session_id`, `cwd`, `transcript_path`).
- POSTs `{session_id, pane_id, cwd}` to `http://localhost:8000/api/agents/cc-pane-bind`.
- Times out after 2 seconds — never blocks `claude` startup if the LifeOS API is down.

You can override the API base URL with `LIFEOS_API_URL` if you run on a non-default port.

---

## 3. Try it

Open two wezterm panes in the same project directory, run `claude` in each, then open `/agents` in your browser. Both sessions should show a **Go To** button. Click it (or double-click the node in the graph) — wezterm focuses the right pane. On GNOME Wayland the focus changes inside wezterm but the window won't pop forward (compositor restriction); click the wezterm dock icon to bring it forward, the right pane will already be selected.

If the toast says "Couldn't locate pane", the most likely causes are:

- The SessionStart hook isn't installed yet for sessions started *before* the hook landed. Restart the `claude` invocation; the hook will fire and bind on the new session.
- `lsof` isn't on PATH (the probe fallback uses it).
- The session is in a non-wezterm terminal — Go To only works for wezterm.

---

## Related Documents

### Specifications
- [Agent Viz — Product](../specs/product/agent-viz.md#operator-controls--resume-and-go-to) — Operator-facing controls overview
- [Agent Viz — Technical](../specs/technical/agent-viz.md#claude-code-resume--go-to) — Endpoint shapes, probe algorithm, security boundaries

### Code References
- [`api/services/cc_pane_locate.py`](../../api/services/cc_pane_locate.py) — Probe implementation
- [`scripts/claude-session-pane.sh`](../../scripts/claude-session-pane.sh) — Hook script
