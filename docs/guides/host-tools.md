# Host Tools

**Status:** Complete
**Last Updated:** 2026-10-31
**Audience:** Operator

Host tools give an MCP client scoped access to the LifeOS host: read and list files under named roots, write to the vault or a scratch directory, run a short allowlist of commands as a separate low-privilege account, and read LifeOS service logs. They are off by default. They exist so a connected app can check on and maintain LifeOS without holding a full shell.

---

## What a client can do

| Tool | Does | Limits |
|---|---|---|
| `lifeos_fs_list` | Lists one directory | Roots `vault`, `scratch`, `lifeos`, `logs`; 500 entries |
| `lifeos_fs_read` | Reads one text file | Same roots; 512 KB; binary files refused |
| `lifeos_fs_write` | Writes one text file (`overwrite`, `create` or `append`) | Roots `vault` and `scratch` only; 1 MB; a vault write is committed |
| `lifeos_shell` | Runs one allowlisted command, argv only, no shell | Runs as `LIFEOS_HOST_TOOLS_RUN_AS`; 30 s; 64 KB of output per stream |
| `lifeos_logs` | Returns the last lines of a LifeOS service's log, optionally filtered | Named services only; 1,000 lines |

The roots are:

- `vault`: `LIFEOS_VAULT_PATH`. Readable and writable.
- `scratch`: `LIFEOS_HOST_TOOLS_SCRATCH_DIR` (default `scratch/` in the LifeOS checkout, created on first use). Readable and writable.
- `lifeos`: the LifeOS checkout. Read-only; code changes go through pull requests.
- `logs`: the checkout's `logs/` directory. Read-only.

Paths are a root name plus a relative path. Absolute paths, `~` and `..` are refused, and the resolved path must stay inside its root, so a symlink pointing out of a root is refused. A write refuses any symlink on its path. Everywhere under a root, the denylist refuses `.git`, `.ssh`, `.gnupg` and similar directories, `.env` files, keys and certificates, pickles, SQLite databases and credential or token JSON files; the checkout's `data/` directory is refused outright. Listings hide denied entries.

A vault write is committed with the message `Host tools: update <path>`, committing only that path, so other uncommitted vault changes stay out of the commit. It is not pushed. Set `LIFEOS_HOST_TOOLS_VAULT_AUTOCOMMIT=false` to leave writes uncommitted.

### The command allowlist

| Command | Allowed arguments |
|---|---|
| `git status` | `--short`, `--branch`, `--porcelain`, `-uno`, paths |
| `git log`, `git show` | `--oneline`, `--stat`, `--name-only`, `--name-status`, `-p`, `--graph`, `-n N`, `--since=`, `--until=` and similar read-only flags, refs, paths |
| `git diff` | `--stat`, `--name-only`, `--name-status`, `--cached`, `--staged`, `-U N`, refs, paths |
| `systemctl status <unit>` | `lifeos-api`, `lifeos-mcp-http`, `lifeos-agent-worker`, `lifeos-chromadb`, `lifeos-llm`, `lifeos-sync` |
| `systemctl restart <unit>` | `lifeos-api`, `lifeos-mcp-http`, `lifeos-agent-worker` (one at a time) |
| `ls` | Common listing flags (not `-R`); relative paths inside a root |
| `tail -n N <file>` | Relative paths inside a root; never `-f` |

Git always runs with no pager, no filesystem monitor and no hooks, and `git diff`, `log` and `show` skip external diff drivers and textconv filters. Every other command, and every other flag, is refused. The allowlist, the roots, the denylist and the caps are defined together at the top of `api/services/host_tools.py`.

### Approval for other commands

`LIFEOS_HOST_TOOLS_SHELL_APPROVAL` is off, so a command outside the allowlist is refused. Turned on, the command is sent to the primary Telegram bot instead, and runs (still as the low-privilege account) only if you reply `APPROVE` to that message before `LIFEOS_HOST_TOOLS_APPROVAL_TIMEOUT_SECONDS` (default 120) runs out. Any other reply denies it, and so does no reply.

The gate is off by default because a phone prompt is a weak check on an arbitrary command: the argv can be long, can look routine while doing something else, and arrives while you are busy. Widen the allowlist in code for anything you want to run regularly.

## Who can call them

- **Local Claude Code (stdio)** and **tailnet clients using the full-access bearer token** see the host tools whenever they are enabled.
- **OAuth-connected apps** (Claude, ChatGPT) see them only when their OAuth client id is listed in `LIFEOS_HOST_TOOLS_OAUTH_CLIENTS`. The grant adds the five host tools and nothing else; the rest of the connected-app tier is unchanged. Find a client id with `scripts/mcp_oauth.py list`, or in the `caller=oauth:<client_id>` field of an `mcp audit:` line in `logs/mcp-http.log`.
- **The agent worker** never gets them; it has its own file and shell tools.

## Audit

Every host tool call is appended to `data/host_tools_audit.jsonl`: the time, the caller (`local-stdio`, `full-access-token` or `oauth:<client_id>`), the tool and the decision. Shell records carry the full argv, the working directory, the rule that allowed it (or why it was refused), the exit code and the duration. Write records carry the path, size, SHA-256 and commit; file content is never recorded. On the HTTP transport, writes and shell calls are also logged at `WARNING` in the `mcp audit:` line.

Command output and log lines have Telegram bot tokens redacted before they are returned.

## Set up

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

3. Give it read access to the checkout and the vault, minus the checkout's `data/` and `.env`:

   ```bash
   sudo setfacl -R -m u:"$TOOLS_USER":rX "$LIFEOS_DIR" "$VAULT_DIR"
   sudo setfacl -R -x u:"$TOOLS_USER" "$LIFEOS_DIR/data" "$LIFEOS_DIR/.env"
   sudo setfacl -m u:"$TOOLS_USER":--- "$LIFEOS_DIR/data" "$LIFEOS_DIR/.env"
   ```

   New files inherit nothing from these entries; rerun the first `setfacl` after large changes, or add a default ACL (`setfacl -d -m ...`) on the vault.

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
   ```

`lifeos_shell` refuses to run when `LIFEOS_HOST_TOOLS_RUN_AS` is empty, names an account that does not exist, names root, or names the account LifeOS itself runs as. The file and log tools work without it.

## Turn it off

- **One app:** remove its client id from `LIFEOS_HOST_TOOLS_OAUTH_CLIENTS` and restart `lifeos-mcp-http`.
- **Commands only:** empty `LIFEOS_HOST_TOOLS_RUN_AS`, or remove `/etc/sudoers.d/lifeos-host-tools`.
- **Everything:** set `LIFEOS_HOST_TOOLS_ENABLED=false` and restart `lifeos-mcp-http`.

## Residual risks

- **Path races.** Paths are checked, then opened. A local process that swaps a directory for a symlink between the two can redirect one read or write. An MCP caller cannot cause that on its own, because writes refuse symlinks and `.git` paths.
- **Vault content.** The vault is readable in full, minus the denylist, and the denylist matches names, not content. A secret pasted into a note is readable.
- **Git configuration.** Git reads repository configuration as the low-privilege account. Hooks, the pager, the filesystem monitor, external diff and textconv are disabled per call, but a clean or smudge filter configured in a repository still runs during `git status`. Host tools cannot write `.git`, so only someone who can already write the repository can set one.
- **Approval prompts** (when turned on) run whatever was approved, with the low-privilege account's rights.

## Related Documents

- [Connected Apps](mcp-connected-apps.md) — Connecting Claude and ChatGPT, and the tailnet bearer route
- [MCP OAuth](../specs/technical/mcp-oauth.md) — The connected-app tool tier the operator grant extends
- [Configuration](configuration.md#host-tools) — `LIFEOS_HOST_TOOLS_*` settings
- [Telegram Setup](telegram-setup.md) — The primary bot that carries approval requests
