# /agents "Go To" — WezTerm setup

> **Status:** Complete
> **Last Updated:** 2026-09-03
> **Audience:** Operators

The /agents page's **Go To** button (and double-clicking an active node) jumps wezterm focus to the pane running the selected Claude Code session — even when several panes are working in the same project directory at once.

Setup is one block in `~/.claude/settings.json` plus making sure `lsof` is installed.

`scripts/install-agent-hooks.sh` + `scripts/lifeos-agent-hook.sh` (below, [§4](#4-cross-machine-session-registration)) supersede the manual hook-config steps below and also register sessions from other machines with `/agents` — not just WezTerm pane binding on this one. `claude-session-pane.sh` / `codex-session-pane.sh` and the manual `~/.claude/settings.json` block in step 2 still work unchanged for an existing install; run the installer once to get both pane binding and cross-machine registration from a single hook going forward.

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

If the toast says "Couldn't locate pane" — meaning the session isn't running where we can reach it, wezterm itself can't be queried, or no cached mapping exists — the most likely causes are:

- The SessionStart hook isn't installed yet for sessions started *before* the hook landed. Restart the `claude` invocation; the hook will fire and bind on the new session.
- WezTerm has been restarted since the session began *and* `claude` is no longer running. The Go To cache invalidates automatically when wezterm's pid changes, but the probe still needs a live `claude` process holding the session's transcript file open. Restart `claude` so the SessionStart hook re-binds, or click **Resume** to open a fresh pane.
- WezTerm is unreachable (`wezterm cli list` errors, the gui-sock socket is gone, or the binary isn't on PATH).
- `lsof` isn't on PATH (the probe fallback uses it).
- The session is in a non-wezterm terminal — Go To only works for wezterm.

---

## 4. Cross-machine session registration

`/agents` can also show Claude Code and Codex sessions running on a laptop, a second desktop — any machine that can reach the API over Tailscale. This uses a different, newer script that installs itself for both CLIs at once:

```bash
./scripts/install-agent-hooks.sh
```

Run once **on each machine** you want registered (including the one hosting the API, if you want its own CLI sessions to report a `host` too). It's idempotent — safe to re-run after an update, it only adds what's missing and never touches an existing entry from Orca, atuin, or anything else already in your hook config.

The installer prints, but does not create, the two things registration needs:

1. **On the API host:** set `LIFEOS_AGENT_HOOK_TOKEN` in `.env` (e.g. `openssl rand -hex 32`) and restart the API. Empty (the default) disables the endpoint entirely.
2. **On every machine posting events** (the API host included, if you want its own sessions to carry `host`): create `~/.config/lifeos/agent-hook.env` (override the path with `$LIFEOS_AGENT_HOOK_ENV`):

   ```
   LIFEOS_API_URL=http://<api-host>:8000
   LIFEOS_AGENT_HOOK_TOKEN=<the same token>
   ```

Until that file (or the equivalent environment variables) exists, `lifeos-agent-hook.sh` exits silently without posting anything — a machine you haven't set up yet just doesn't show up, it doesn't error.

A session registered this way shows a `host` badge in the side panel, plus its git branch and last prompt preview when available. Its status is event-driven (accurate `running`/`idle`/`ended`), not inferred from file age. **Resume** and **Go To** only work for sessions on the machine hosting the API — clicking either on a remote-host session returns an error naming the host instead of trying (and failing) a local wezterm probe.

---

## Related Documents

### Specifications
- [Agent Viz — Product](../specs/product/agent-viz.md#operator-controls--resume-and-go-to) — Operator-facing controls overview
- [Agent Viz — Technical](../specs/technical/agent-viz.md#claude-code-resume--go-to) — Endpoint shapes, probe algorithm, security boundaries

### Code References
- [`api/services/cc_pane_locate.py`](../../api/services/cc_pane_locate.py) — Probe implementation
- [`scripts/claude-session-pane.sh`](../../scripts/claude-session-pane.sh) — Legacy WezTerm-only hook script (this machine only)
- [`scripts/lifeos-agent-hook.sh`](../../scripts/lifeos-agent-hook.sh) — Cross-machine session registration hook (§4)
- [`scripts/install-agent-hooks.sh`](../../scripts/install-agent-hooks.sh) — Idempotent installer for the hook above
