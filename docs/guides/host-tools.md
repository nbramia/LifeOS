# Host Tools

**Status:** Complete
**Last Updated:** 2026-11-01
**Audience:** Operator

Host tools give an MCP client scoped access to the LifeOS host: read and list files under named roots, write to the vault or a scratch directory, run a short allowlist of commands as a separate low-privilege account, and read LifeOS service logs. They are off by default. They exist so a connected app can check on and maintain LifeOS without holding a full shell.

---

## What a client can do

| Tool | Does | Limits |
|---|---|---|
| `lifeos_fs_list` | Lists one directory | Roots `vault`, `scratch`, `lifeos`, `logs`; 500 entries |
| `lifeos_fs_read` | Reads one text file | Same roots; 512 KB; binary files refused |
| `lifeos_fs_write` | Writes one text file (`overwrite`, `create` or `append`) | Roots `vault` and `scratch` only; 1 MB; a vault write is committed when the vault is inside a git repository |
| `lifeos_shell` | Runs one allowlisted command, argv only, no shell | Runs as `LIFEOS_HOST_TOOLS_RUN_AS`; 30 s; 64 KB of output per stream |
| `lifeos_logs` | Returns the last lines of a LifeOS service's log, optionally filtered | Named services only; 1,000 lines |

The roots are:

- `vault`: `LIFEOS_VAULT_PATH`. Readable in full. Writable in full, or only at the entries listed in `LIFEOS_HOST_TOOLS_VAULT_WRITE_DIRS` (comma-separated, relative to the vault root). An entry is a folder (everything inside it is writable) or a single file (only that file), for example `Assistant,LifeOS/Tasks/Inbox.md,LifeOS/Scheduler/Inbox.md`. The limit applies to every host-tool caller; a refused write names the allowed entries.
- `scratch`: `LIFEOS_HOST_TOOLS_SCRATCH_DIR` (default `scratch/` in the LifeOS checkout, created on first use). Readable and writable. The directory is private to the LifeOS account; on first use in each process the service adds a read ACL entry (and a default entry, so new files inherit it) for the `LIFEOS_HOST_TOOLS_RUN_AS` account only, so commands can run from it. This needs `setfacl`; without it, commands run from scratch fail with "Permission denied" and a warning is logged.
- `lifeos`: the LifeOS checkout. Read-only; code changes go through pull requests.
- `logs`: the checkout's `logs/` directory. Read-only.

Paths are a root name plus a relative path. Absolute paths, `~` and `..` are refused, and the resolved path must stay inside its root, so a symlink pointing out of a root is refused. A write refuses any symlink on its path. Everywhere under a root, the denylist refuses `.git`, `.ssh`, `.gnupg` and similar directories, `.env*` and `*.env` files, keys and certificates (`.pem`, `.key`, `.crt`, `.cer`, `.p12`, `.pfx`), pickles, SQLite databases and credential or token JSON files; the checkout's `data/` directory is refused outright. Listings hide denied entries.

A vault write is committed with the message `Host tools: update <path>`, committing only that path (taken literally, never as a pattern), so other uncommitted vault changes stay out of the commit. The commit runs with git hooks and the filesystem monitor disabled. It is not pushed. Set `LIFEOS_HOST_TOOLS_VAULT_AUTOCOMMIT=false` to leave writes uncommitted.

If the vault on the host is not inside a git repository (for example, a copy kept in step by a sync tool, with git living on another machine), a write is saved but not committed, and the result says so in `commit_skipped`. Whatever syncs that copy carries the write; commit it where the repository lives.

### Task and schedule files

A write to a task file (a `.md` file directly in `LifeOS/Tasks/`) or to the scheduler inbox (`LifeOS/Scheduler/Inbox.md`) is checked against the file's current contents before it lands, whatever `LIFEOS_HOST_TOOLS_VAULT_WRITE_DIRS` allows. For a task file the write is refused if it:

- adds any engine, consent, protected-lifecycle or `#human` tag, counted in the raw text and its NFKC form;
- removes a task's `<!-- id:... -->` comment, or changes the routing tags or `parent_id` of an existing task;
- changes any line of a task that an engine has claimed or that waits on the operator (`agent`, an engine or consent tag, `agent-running`, `agent-blocked`, `agent-completed`, `#human` and similar);
- adds a new task with a `parent_id`.

For the scheduler inbox it is refused if it removes a schedule, changes one that is not a static notification, or adds or changes one that is anything but a static notification (no endpoint, executor, persona, model or working directory). A static notification fires a fixed message and runs nothing.

These checks keep a raw file edit from doing what the task API refuses. For task edits prefer `lifeos_task_update`, which applies the same rules per task and merges with concurrent changes ([Task Management](../specs/product/task-management.md#external-agents)).

### Concurrent edits

`lifeos_fs_read` returns a `sha256` of the file when it returned the whole file. Pass it back to `lifeos_fs_write` as `expected_sha256`: if the file changed in between (an Obsidian Sync change, another agent), the write is refused with "the file changed since it was read" and nothing is written. Read the file again and redo the edit. A truncated read has no `sha256`, since the edit could not have seen the whole file.

A new file is written with mode `0644`. An overwritten file keeps its mode. Writes go through a temporary file that is renamed into place, so extended ACL entries set on the old file are not carried over: give the vault a default ACL for the run-as account (step 3) so rewritten notes stay readable to it.

### The command allowlist

| Command | Allowed arguments |
|---|---|
| `git status` | `--short`, `--branch`, `--porcelain`, `-uno`, paths |
| `git log`, `git show` | `--oneline`, `--stat`, `--name-only`, `--name-status`, `-p`, `--graph`, `-nN`, `--since=`, `--until=` and similar read-only flags, refs, paths |
| `git diff` | `--stat`, `--name-only`, `--name-status`, `--cached`, `--staged`, `-UN`, refs, paths |
| `systemctl status <unit>` | `lifeos-api`, `lifeos-mcp-http`, `lifeos-agent-worker`, `lifeos-chromadb`, `lifeos-llm`, `lifeos-sync` |
| `systemctl restart <unit>` | `lifeos-api`, `lifeos-mcp-http`, `lifeos-agent-worker` (one at a time) |
| `ls` | Common listing flags (not `-R`); relative paths inside a root |
| `tail -nN <file>` (or `-n N`) | Relative paths inside a root; never `-f` |

Every non-flag git argument (including those after `--`) is treated as a path and must be relative, inside a readable root and off the denylist; for `<rev>:<path>` the path part is checked. Pathspec magic and `--no-index` are not accepted. Git always runs with literal pathspecs, no pager, no filesystem monitor and no hooks, and `git diff`, `log` and `show` skip external diff drivers and textconv filters. Every other command, and every other flag, is refused. The allowlist, the roots, the denylist and the caps are defined together at the top of `api/services/host_tools.py`.

### Approval for other commands

Off by default: a command outside the allowlist is refused. Turn on approval with one setting, `LIFEOS_HOST_TOOLS_SHELL_APPROVAL=true` (step 8 below), and a command outside the allowlist is sent to you on Telegram instead:

1. The caller gets a `pending-approval` result straight away, with a short request id. Nothing runs.
2. The primary bot sends a plain-text message to `TELEGRAM_CHAT_ID` with the request id, when it was asked, when it expires, the caller, the account it runs as, the directory, the exact argv (shown as shell words, never shortened) and the first 16 characters of the command hash. A command too long to show in one message is refused instead.
3. Reply `APPROVE` to that message to allow it, or anything else to refuse it. Only a reply in the configured chat, to a request sent to that chat, counts. The bot answers with the request's age. A bare `APPROVE` that is not a reply approves nothing: while a request is waiting, the bot answers with the waiting request ids instead of treating it as a chat message. A reply to a request that was already decided, ran or expired gets a short answer and changes nothing.
4. The command runs the next time the same caller asks for the same argv in the same directory as the same account. The approval is spent the moment it runs, so it runs once; a second identical call starts a new request. The bot tells you when it ran, with its exit code and how long ago it was asked and approved.

An approval is bound to a SHA-256 hash of the caller, the run-as account, the directory and the argv, so it cannot be used for any other command or by any other caller. Approving does not run anything by itself: if the caller never asks again (its conversation ended, say), the approval expires unused, and that is logged too. Requests and unused approvals expire after `LIFEOS_HOST_TOOLS_APPROVAL_TIMEOUT_SECONDS` (default 86400, one day); set it to `0` to turn expiry off. A caller can have at most three requests waiting at once.

An approved command still:

- runs as `LIFEOS_HOST_TOOLS_RUN_AS`, with the same 30-second and output limits;
- passes the path checks: every argument that looks like a path (and the value after any `=`) must be relative, stay inside a readable root and miss the denylist;
- never names a shell, an interpreter, a command that runs other commands, a privilege tool, or a network tool (`bash`, `python`, `env`, `xargs`, `find`, `sed`, `awk`, `make`, `tar`, `systemd-run`, `sudo`, `curl`, `ssh`, `rsync` and others), a git option before the subcommand (`-c`, `-C`, `--git-dir`), or a git subcommand that reaches another host (`push`, `pull`, `fetch`, `clone`, `remote` and others). The full list is `NEVER_APPROVED_COMMANDS` in `api/services/host_tools.py`. These are refused without asking you, because they would let a command step around the path checks or send data off the host.

The path checks look at arguments, not at what a program does with them, so the low-privilege account and its file permissions remain the real boundary.

Approval is only as strong as the Telegram account behind `TELEGRAM_CHAT_ID`. Use a private chat with the bot, not a group (anyone in a group could reply `APPROVE`), and keep two-step verification on. Read the argv before approving: a request made by a session that read untrusted mail or web pages can look routine and do something else. Widen the allowlist in code for anything you want to run regularly.

## Who can call them

- **Local Claude Code (stdio)** and **tailnet clients using the full-access bearer token** see the host tools whenever they are enabled.
- **OAuth-connected apps** (Claude, ChatGPT) see them only when their OAuth client id is listed in `LIFEOS_HOST_TOOLS_OAUTH_CLIENTS`. The grant adds the five host tools and nothing else; the rest of the connected-app tier is unchanged. Find a client id with `scripts/mcp_oauth.py list`, or in the `caller=oauth:<client_id>` field of an `mcp audit:` line in `logs/mcp-http.log`.
- **The agent worker** never gets them; it has its own file and shell tools.

## Audit

Every host tool call is appended to `data/host_tools_audit.jsonl`: the time, the caller (`local-stdio`, `full-access-token` or `oauth:<client_id>`), the tool and the decision. Shell records carry the full argv, the working directory, the rule that allowed it (or why it was refused), the exit code and the duration. Approval adds records for each request (`approval-pending`, `approval-denied`, `approval-refused`), each reply (`operator-approved`, `operator-denied`), each run (`approved`, with the request id, the approver and the request's age) and each expiry (`approval-expired-pending`, or `approval-expired-approved` for an approval never used). Write records carry the path, size, SHA-256 and commit; file content is never recorded. On the HTTP transport, writes and shell calls are also logged at `WARNING` in the `mcp audit:` line, with `host_write=True`. A shell call that ran under a read-only rule (`ls`, `tail`, `git status`, `git log`, `git diff`, `git show`, `systemctl status`) is logged at `INFO` with `host_write=False`. Refused calls, approval requests, approved commands and `systemctl restart` stay at `WARNING`.

A shell command is recorded with the decision `started` before it runs and again, with its exit code, when it finishes, so a command that restarts the MCP service itself still leaves a record. If the `started` record cannot be written, the command does not run.

Telegram bot tokens are redacted before anything is returned from a file read (in any root), shell output, or a log line. Audit records redact the same tokens in argv, reason and detail before they are written.

## Set up

This setup is for a Linux host: it uses `useradd`, `setfacl`, `sudo` and systemd.

The steps below create the low-privilege account and give it only what the allowlist needs. Run them on the LifeOS host as an admin. They use shell variables so nothing host-specific is written into a file you might share; set them first.

```bash
TOOLS_USER=lifeos-tools                      # the low-privilege account
LIFEOS_DIR=/path/to/LifeOS                   # your LifeOS checkout
LIFEOS_USER=$(stat -c %U "$LIFEOS_DIR")      # the account LifeOS runs as
VAULT_DIR="/path/to/your vault"              # same as LIFEOS_VAULT_PATH; keep the quotes if it has spaces
```

1. Create the account, with no login shell and no password:

   ```bash
   sudo useradd --system --create-home --shell /usr/sbin/nologin "$TOOLS_USER"
   ```

2. Let the LifeOS account run commands as it, and nothing else:

   ```bash
   echo "$LIFEOS_USER ALL=($TOOLS_USER) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/lifeos-host-tools
   sudo chmod 440 /etc/sudoers.d/lifeos-host-tools && sudo visudo -c
   ```

   This grants the LifeOS account the low-privilege account's rights, which are what the next steps give it. It does not grant root.

3. Give it read access to the checkout and the vault, with no access to the checkout's `data/` and `.env`:

   ```bash
   sudo setfacl -R -m u:"$TOOLS_USER":rX "$LIFEOS_DIR" "$VAULT_DIR"
   sudo setfacl -R -x u:"$TOOLS_USER" "$LIFEOS_DIR/data" "$LIFEOS_DIR/.env"
   sudo setfacl -m u:"$TOOLS_USER":--- "$LIFEOS_DIR/data" "$LIFEOS_DIR/.env"
   ```

   These entries grant read access to everything else in both trees and withhold only `data/` and `.env`. Add the same `u:"$TOOLS_USER":---` entry for any other secret file in the checkout or vault (for example OAuth token files under `config/`). The tool-level denylist matches names; these file permissions are what the account itself can read. New files inherit nothing from these entries; rerun the first `setfacl` after large changes, or add a default ACL on the vault so new and rewritten files inherit read access:

   ```bash
   sudo setfacl -R -d -m u:"$TOOLS_USER":rX "$VAULT_DIR"
   ```

4. Let it run git in those repositories (git otherwise refuses a repository owned by another account):

   ```bash
   sudo -u "$TOOLS_USER" git config --global --add safe.directory "$LIFEOS_DIR"
   sudo -u "$TOOLS_USER" git config --global --add safe.directory "$VAULT_DIR"
   ```

5. Let it restart the three restartable units, in the same form `scripts/setup-systemd.sh` uses for the LifeOS account:

   ```bash
   echo "$TOOLS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart lifeos-api.service, /usr/bin/systemctl restart lifeos-mcp-http.service, /usr/bin/systemctl restart lifeos-agent-worker.service" \
     | sudo tee /etc/sudoers.d/lifeos-host-tools-restart
   sudo chmod 440 /etc/sudoers.d/lifeos-host-tools-restart && sudo visudo -c
   ```

   Skip this step to keep restarts out of reach; `systemctl restart` then fails with a sudo error.

6. Check it from the LifeOS account:

   ```bash
   sudo -n -u "$TOOLS_USER" -- git -C "$LIFEOS_DIR" status --short   # works
   sudo -n -u "$TOOLS_USER" -- cat "$LIFEOS_DIR/.env"                 # Permission denied
   ```

7. Add to `.env`, then restart the MCP transport (`sudo systemctl restart lifeos-mcp-http`):

   ```bash
   LIFEOS_HOST_TOOLS_ENABLED=true
   LIFEOS_HOST_TOOLS_RUN_AS=lifeos-tools
   # Optional: grant a connected app (client id from scripts/mcp_oauth.py list)
   # LIFEOS_HOST_TOOLS_OAUTH_CLIENTS=<client_id>
   # Optional: limit vault writes to these folders or files (empty: the whole vault)
   # LIFEOS_HOST_TOOLS_VAULT_WRITE_DIRS=Assistant,LifeOS/Tasks/Inbox.md,LifeOS/Scheduler/Inbox.md
   ```

8. Optional: turn on Telegram approval for commands outside the allowlist. Telegram must already work (`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, see [Telegram Setup](telegram-setup.md)), and `TELEGRAM_CHAT_ID` should be your private chat with the bot. Add to `.env`, then restart both the MCP transport and the API (the API's Telegram listener records your replies):

   ```bash
   LIFEOS_HOST_TOOLS_SHELL_APPROVAL=true
   # Optional: how long a request or unused approval stays open, in seconds (0: never expires)
   # LIFEOS_HOST_TOOLS_APPROVAL_TIMEOUT_SECONDS=86400
   ```

   ```bash
   sudo systemctl restart lifeos-mcp-http lifeos-api
   ```

`lifeos_shell` refuses to run when `LIFEOS_HOST_TOOLS_RUN_AS` is empty, names an account that does not exist, names root, or names the account LifeOS itself runs as. The file and log tools work without it.

## Turn it off

- **One app:** remove its client id from `LIFEOS_HOST_TOOLS_OAUTH_CLIENTS` and restart `lifeos-mcp-http`.
- **Approval only:** set `LIFEOS_HOST_TOOLS_SHELL_APPROVAL=false` and restart `lifeos-mcp-http`. Open requests and unused approvals stop counting at once, because nothing outside the allowlist reaches the gate.
- **Commands only:** empty `LIFEOS_HOST_TOOLS_RUN_AS`, or remove `/etc/sudoers.d/lifeos-host-tools`.
- **Everything:** set `LIFEOS_HOST_TOOLS_ENABLED=false` and restart `lifeos-mcp-http`.

## Residual risks

- **Path races.** Paths are checked, then opened. A local process that swaps a directory for a symlink between the two can redirect one read or write. An MCP caller cannot cause that on its own, because writes refuse symlinks and `.git` paths.
- **Vault content.** The vault is readable in full, minus the denylist, and the denylist matches names, not content. A secret pasted into a note is readable.
- **Git configuration.** Git reads repository configuration as the low-privilege account. Hooks, the pager, the filesystem monitor, external diff and textconv are disabled per call, but a clean or smudge filter configured in a repository still runs during `git status`. Host tools cannot write `.git`, so only someone who can already write the repository can set one.
- **Approval** (when turned on) runs whatever was approved, with the low-privilege account's rights. With expiry turned off, an approval nobody used stays usable by that caller for that exact command until it is used or approval is turned off.

## Related Documents

- [Connected Apps](mcp-connected-apps.md) — Connecting Claude and ChatGPT, and the tailnet bearer route
- [MCP OAuth](../specs/technical/mcp-oauth.md) — The connected-app tool tier the operator grant extends
- [Configuration](configuration.md#host-tools) — `LIFEOS_HOST_TOOLS_*` settings
- [Telegram Setup](telegram-setup.md) — The primary bot that carries approval requests
