"""
LifeOS Configuration Settings
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

logger = logging.getLogger(__name__)

# Registry of specialized Telegram bots (beyond the primary). Committed, no
# secrets — each entry references the env var holding its token.
_TELEGRAM_BOTS_FILE = Path("config/telegram_bots.json")
_PRIMARY_PERSONA_FILE = Path("config/personas/primary.md")
_BOT_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class TelegramBotConfig:
    """A single Telegram bot surface: its token, authorized chat, and persona.

    ``name`` doubles as the per-bot state-file suffix, so it must be filesystem
    safe. ``persona`` is a system-prompt preamble injected for this bot's chats
    (empty for the primary bot). ``label`` is an optional human-friendly display
    name surfaced to HTTP clients; when blank, callers fall back to the
    capitalized ``name``. ``orchestrates`` marks a bot that drives Claude Code
    sessions (e.g. the doctor self-repair bot) instead of being pure chat — such
    a bot owns its own agent-session reply threads rather than redirecting coding
    tasks to the primary bot.
    """
    name: str
    token: str
    chat_id: str
    persona: str = ""
    label: str = ""
    orchestrates: bool = False
    # Parsed from the persona file's optional YAML frontmatter (see _parse_persona).
    # `voice` rules are consumed on voice turns (the chat route appends them to the
    # system prompt). `model` is RESERVED — parsed and stored here but not yet read
    # by any code path; the orchestrator resolves its model from anthropic_model +
    # per-turn escalation, so setting a persona `model` is currently a no-op.
    voice: tuple[str, ...] = ()
    model: str = ""


def _parse_persona(text: str, name: str = "") -> "tuple[str, tuple[str, ...], str]":
    """Split a persona file into ``(body, voice, model)``.

    Personas may carry a leading YAML frontmatter block (``id`` / ``model`` /
    ``voice``); only the **body** becomes the system-prompt preamble, so the
    frontmatter never leaks into the prompt. Files without frontmatter pass
    through unchanged. The body is returned verbatim (no ``str.format``) so a
    persona may contain literal ``{...}`` examples. ``voice``/``model`` are parsed
    for the orchestrator to consume later; they are inert here.
    """
    try:
        post = frontmatter.loads(text)
    except Exception as e:  # noqa: BLE001 — a malformed persona file must not take down
        # the whole bot registry (telegram_bots is an uncached property feeding
        # resolve_persona / list_http_personas). Degrade to the raw file as the preamble.
        logger.warning(f"persona file {name!r}: frontmatter parse failed ({e}); using the raw file as the preamble")
        return text.strip(), (), ""
    meta = post.metadata or {}
    raw_voice = meta.get("voice") or []
    if raw_voice and not isinstance(raw_voice, list):
        logger.warning(f"persona file {name!r}: `voice` must be a YAML list; ignoring {type(raw_voice).__name__}")
        raw_voice = []
    voice = tuple(s for v in raw_voice if (s := str(v).strip()))  # drop blank rules
    model = str(meta.get("model") or "").strip()
    fid = meta.get("id")
    if fid and name and str(fid) != name:
        logger.warning(f"persona file id={fid!r} does not match bot name {name!r}")
    return post.content.strip(), voice, model


def _load_primary_persona() -> "tuple[str, tuple[str, ...], str]":
    """Load the primary persona from ``config/personas/primary.md`` if present.

    Primary has no registry entry, so its preamble (and ``voice``/``model``) come
    from this file directly. An absent file → no preamble, the historical default.
    """
    try:
        return _parse_persona(_PRIMARY_PERSONA_FILE.read_text(), "primary")
    except OSError:
        return "", (), ""


# Capabilities advertised to HTTP clients. The primary persona and any
# orchestrating bot (e.g. the doctor self-repair bot) drive Claude Code
# sessions, so they advertise handoff/agent; pure-chat specialized bots do not.
ORCHESTRATOR_PERSONA_CAPABILITIES = ("handoff", "agent")


@dataclass(frozen=True)
class PersonaInfo:
    """An HTTP-visible chat persona (no secrets) for the discovery endpoint."""
    id: str
    label: str
    capabilities: list = field(default_factory=list)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Paths (use LIFEOS_ prefix)
    vault_path: Path = Field(
        default=Path("./vault"),
        alias="LIFEOS_VAULT_PATH"
    )
    chroma_path: Path = Field(
        default=Path("./data/chromadb"),
        alias="LIFEOS_CHROMA_PATH"
    )
    chroma_url: str = Field(
        default="http://localhost:8001",
        alias="LIFEOS_CHROMA_URL",
        description="ChromaDB server URL"
    )

    # Voice gateway (whisper-relay). LifeOS reverse-proxies /api/voice/* here so
    # the browser stays same-origin for mic/HTTPS (#361). See ADR-016.
    voice_gateway_url: str = Field(
        default="http://127.0.0.1:9788",
        alias="LIFEOS_VOICE_GATEWAY_URL",
        description="whisper-relay voice gateway base URL"
    )

    # Agent text backend (OpenClaw voice-adapter). LifeOS proxies the "Agent"
    # text backend to /api/ask/stream here, adding the bearer token server-side
    # so it's never exposed to the browser (#361). Empty url = Agent disabled.
    agent_backend_url: str = Field(
        default="",
        alias="LIFEOS_AGENT_BACKEND_URL",
        description="Agent text backend base URL (empty disables the Agent toggle)"
    )
    agent_backend_token: str = Field(
        default="",
        alias="LIFEOS_AGENT_BACKEND_TOKEN",
        description="Optional bearer token for the Agent text backend"
    )

    # Default /chat input mode. Off (text) by default so a fresh clone without a
    # voice gateway isn't dropped onto a non-functional dock; set true to make
    # voice the default. A ?mode= URL param or a stored preference still wins.
    chat_default_voice: bool = Field(
        default=False,
        alias="LIFEOS_CHAT_DEFAULT_VOICE",
        description="Make voice the default /chat input mode"
    )

    # Code directory (parent directory containing LifeOS and other projects)
    code_dir: str = Field(default="~/Code", alias="LIFEOS_CODE_DIR")

    # Server (port 8000 is canonical - keep in sync with scripts/server.sh)
    port: int = Field(default=8000, alias="LIFEOS_PORT")
    host: str = Field(default="0.0.0.0", alias="LIFEOS_HOST")

    # API Keys (no prefix - standard env var names)
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # Embedding Model
    # mxbai-embed-large-v1: Top-tier 1024-dim model, stable and well-tested
    # Override via LIFEOS_EMBEDDING_MODEL for constrained hardware (e.g., all-MiniLM-L6-v2)
    embedding_model: str = Field(
        default="mixedbread-ai/mxbai-embed-large-v1",
        alias="LIFEOS_EMBEDDING_MODEL"
    )
    embedding_cache_dir: str = Field(
        default="",
        alias="LIFEOS_EMBEDDING_CACHE",
        description="Directory for caching embedding model files (leave empty for HuggingFace default)"
    )

    # Chunking
    chunk_size: int = 500  # tokens
    chunk_overlap: int = 100  # tokens (20% overlap for better boundary handling)

    # Search
    default_top_k: int = 20

    # LLM Backend: "anthropic" (default) or "local"
    llm_backend: str = Field(default="anthropic", alias="LIFEOS_LLM_BACKEND")

    # Anthropic model for orchestration
    anthropic_model: str = Field(default="claude-haiku-4-5", alias="LIFEOS_ANTHROPIC_MODEL")

    # Anthropic model for specialist calls (relationship insights, fact
    # extraction, tone analysis) — Sonnet-tier for quality, independent of the
    # orchestrator model above. Pin ALIASES here (e.g. claude-sonnet-5),
    # never dated snapshots (claude-*-20YYMMDD): snapshots retire and start
    # returning 404, silently breaking every specialist feature (#470).
    anthropic_specialist_model: str = Field(
        default="claude-sonnet-5", alias="LIFEOS_ANTHROPIC_SPECIALIST_MODEL"
    )

    # Local LLM (OpenAI-compatible server, e.g. llama-server)
    local_llm_url: str = Field(default="http://localhost:8080", alias="LIFEOS_LOCAL_LLM_URL")
    local_llm_timeout: int = Field(default=90, alias="LIFEOS_LOCAL_LLM_TIMEOUT")
    local_llm_model: str = Field(
        default="unsloth/gemma-4-26B-A4B-it-GGUF",
        alias="LIFEOS_LLM_MODEL",
        description="HuggingFace GGUF model ID for llama-server"
    )

    # MCP HTTP transport (used by remote agent platforms; local Claude Code keeps stdio)
    mcp_http_port: int = Field(
        default=8765,
        alias="LIFEOS_MCP_HTTP_PORT",
        description="Port for the MCP HTTP transport (lifeos-mcp-http systemd unit)"
    )
    mcp_http_host: str = Field(
        default="127.0.0.1",
        alias="LIFEOS_MCP_HTTP_HOST",
        description="Bind address for the MCP HTTP transport. Default 127.0.0.1 so only "
                    "the local Cloudflare Tunnel daemon can reach it; the tunnel handles "
                    "public exposure."
    )
    mcp_bearer_token: str = Field(
        default="",
        alias="LIFEOS_MCP_BEARER_TOKEN",
        description="Bearer token required by the MCP HTTP transport. Generate with "
                    "`openssl rand -hex 32`. Empty disables the HTTP transport."
    )

    # Agent Worker — external worker that picks up #agent-tagged tasks.
    # See docs/guides/agent-worker-setup.md and epic #98 for the full design.
    agent_worker_poll_seconds: float = Field(
        default=60.0,
        alias="LIFEOS_AGENT_WORKER_POLL_SECONDS",
        description="How often the agent worker polls for new #agent tasks."
    )
    agent_worker_autostart: bool = Field(
        default=False,
        alias="LIFEOS_AGENT_WORKER_AUTOSTART",
        description="Enable systemd auto-start on boot for the agent worker. "
                    "Off by default — opt-in via setup-systemd.sh."
    )
    agent_default_budget_dollars: float = Field(
        default=5.0,
        alias="LIFEOS_AGENT_DEFAULT_BUDGET_DOLLARS",
        description="Default per-task dollar budget when the task title doesn't specify one."
    )
    agent_default_wall_seconds: int = Field(
        default=14400,
        alias="LIFEOS_AGENT_DEFAULT_WALL_SECONDS",
        description="Default per-task wall-clock budget (seconds); 14400 = 4h."
    )
    agent_default_max_tokens: int = Field(
        default=500_000,
        alias="LIFEOS_AGENT_DEFAULT_MAX_TOKENS",
        description="Default per-task token budget (input + output combined)."
    )
    agent_daily_cap_dollars: float = Field(
        default=100.0,
        alias="LIFEOS_AGENT_DAILY_CAP_DOLLARS",
        description="Global daily cap across all agent tasks. New tasks are not "
                    "claimed once today's cumulative spend would exceed this."
    )
    agent_clarification_timeout_hours: int = Field(
        default=72,
        alias="LIFEOS_AGENT_CLARIFICATION_TIMEOUT_HOURS",
        description="How long an #agent-blocked task waits for a Telegram reply "
                    "before being abandoned. Used by Issue F."
    )
    agent_output_dir: str = Field(
        default="LifeOS/Tasks/Agent Output",
        alias="LIFEOS_AGENT_OUTPUT_DIR",
        description="Vault-relative directory where agent-created artifacts "
                    "(Markdown notes, CSVs) land. The local executor writes "
                    "here directly; the worker's spillover for long outputs "
                    "also writes here. Path is joined under LIFEOS_VAULT_PATH."
    )
    agent_preflight_model: str = Field(
        default="claude-haiku-4-5",
        alias="LIFEOS_AGENT_PREFLIGHT_MODEL",
        description="Anthropic model used for the Haiku preflight call that "
                    "classifies #agent tasks (budget, routing, ambiguity, sanity)."
    )
    agent_managed_model: str = Field(
        default="claude-sonnet-5",
        alias="LIFEOS_AGENT_MANAGED_MODEL",
        description="Anthropic model the Managed Agents executor uses for "
                    "Claude-routed #agent tasks. Informational only — the "
                    "actual model is whatever the agent preset says; this "
                    "value is just used for client-side token-cost accounting."
    )
    agent_managed_model_for_tests: str = Field(
        default="",
        alias="LIFEOS_AGENT_MANAGED_MODEL_FOR_TESTS",
        description="Optional dev-only override of `agent_managed_model` used "
                    "when iterating on the cloud agent path. Set to e.g. "
                    "`claude-haiku-4-5` to swap cost accounting to the cheaper "
                    "model during iteration. Empty (default) means no override. "
                    "This only changes client-side dollar accounting; the "
                    "actual remote model is still the agent preset's setting."
    )
    agent_escalation_model: str = Field(
        default="",
        alias="LIFEOS_AGENT_ESCALATION_MODEL",
        description="Anthropic model the chat orchestrator retries a turn on when "
                    "it detects a refusal/impossibility claim followed by the user "
                    "pushing back ('do research', 'you're wrong'). Empty (default) "
                    "disables escalation — safe for fresh clones and the local "
                    "backend. Set to a stronger model than LIFEOS_ANTHROPIC_MODEL "
                    "(e.g. claude-sonnet-5 or claude-opus-4-8) to enable. Only "
                    "applies to the Anthropic backend."
    )
    agent_escalation_ladder: str = Field(
        default="",
        alias="LIFEOS_AGENT_ESCALATION_LADDER",
        description="Ordered, comma-separated escalation rungs climbed on each "
                    "successive refusal+pushback cycle (#305c). Each rung is a "
                    "model id or an engine name (codex / claude_code, which hand "
                    "off to a worker session). Empty (default) derives a ladder "
                    "from LIFEOS_AGENT_ESCALATION_MODEL: [that model, claude_code] "
                    "— so the Claude Code handoff lands on the 2nd pushback. "
                    "Override to insert rungs, e.g. "
                    "'claude-sonnet-5,claude-opus-4-8,claude_code'."
    )
    agent_cost_confirm_threshold_dollars: float = Field(
        default=1.0,
        alias="LIFEOS_AGENT_COST_CONFIRM_THRESHOLD_DOLLARS",
        description="When preflight's cost estimate for a managed-agent task "
                    "exceeds this dollar threshold, the orchestrator must "
                    "confirm with the operator before dispatching (#139 §7). "
                    "Set to 0 to disable confirmation (auto-dispatch all "
                    "managed tasks regardless of estimate)."
    )
    agent_viz_prefetch_enabled: bool = Field(
        default=True,
        alias="LIFEOS_AGENT_VIZ_PREFETCH_ENABLED",
        description="When true, a background loop walks the /agents snapshot "
                    "between user actions and pre-computes Gemma summaries for "
                    "any session that doesn't already have one cached, yielding "
                    "to the agent worker when it's running. Set false to make "
                    "summaries strictly click-on-demand."
    )
    claude_code_viz_enabled: bool = Field(
        default=True,
        alias="LIFEOS_CLAUDE_CODE_VIZ_ENABLED",
        description="When true, the /agents page also surfaces local Claude "
                    "Code CLI sessions discovered under "
                    "$LIFEOS_CLAUDE_CODE_PROJECTS_DIR (default ~/.claude/projects). "
                    "Read-only. Set false to scope the viz to LifeOS agent "
                    "worker sessions only."
    )
    claude_code_projects_dir: str = Field(
        default="~/.claude/projects",
        alias="LIFEOS_CLAUDE_CODE_PROJECTS_DIR",
        description="Filesystem root containing per-cwd Claude Code transcript "
                    "directories. Each child dir is one working directory "
                    "(slashes replaced by hyphens) and holds *.jsonl files, "
                    "one per CLI session."
    )
    claude_code_lookback_days: int = Field(
        default=7,
        alias="LIFEOS_CLAUDE_CODE_LOOKBACK_DAYS",
        description="Only ingest Claude Code session jsonl files modified "
                    "within this many days. Keeps the snapshot lean — older "
                    "transcripts can still be opened on demand by direct id."
    )
    codex_viz_enabled: bool = Field(
        default=True,
        alias="LIFEOS_CODEX_VIZ_ENABLED",
        description="When true, the /agents page also surfaces local Codex "
                    "CLI sessions discovered under "
                    "$LIFEOS_CODEX_SESSIONS_DIR (default ~/.codex/sessions). "
                    "Read-only. Set false to omit Codex sessions from the viz."
    )
    codex_sessions_dir: str = Field(
        default="~/.codex/sessions",
        alias="LIFEOS_CODEX_SESSIONS_DIR",
        description="Filesystem root containing Codex CLI rollout files, "
                    "organized as `<year>/<month>/<day>/rollout-*.jsonl`. "
                    "One JSONL per session."
    )
    codex_lookback_days: int = Field(
        default=7,
        alias="LIFEOS_CODEX_LOOKBACK_DAYS",
        description="Only ingest Codex rollout files modified within this many "
                    "days. Older transcripts can still be opened on demand by "
                    "direct id via the events endpoint."
    )
    codex_resume_enabled: bool = Field(
        default=False,
        alias="LIFEOS_CODEX_RESUME_ENABLED",
        description="When true, the /agents UI exposes a 'Resume' button on "
                    "terminal-state Codex sessions that spawns a local "
                    "terminal running the configured resume command. Opt-in "
                    "because spawning GUI terminals from a systemd service "
                    "depends on the operator's desktop environment."
    )
    codex_resume_cmd: str = Field(
        default="wezterm cli spawn --cwd {cwd} -- {inner_command}",
        alias="LIFEOS_CODEX_RESUME_CMD",
        description="Launcher command for resuming a Codex session. Same "
                    "substitution surface as LIFEOS_CC_RESUME_CMD: "
                    "`{session_id}`, `{cwd}`, `{inner_command}`, URL-encoded "
                    "`{session_id_url}` / `{cwd_url}`. Parsed with shlex.split."
    )
    codex_resume_inner_cmd: str = Field(
        default="codex resume {session_id}",
        alias="LIFEOS_CODEX_RESUME_INNER_CMD",
        description="Command run *inside* the spawned terminal — the actual "
                    "`codex resume` invocation. Substitutions: `{session_id}`, "
                    "`{cwd}`. Set to empty to skip the inner command."
    )
    cc_resume_enabled: bool = Field(
        default=False,
        alias="LIFEOS_CC_RESUME_ENABLED",
        description="When true, the /agents UI exposes a 'Resume' button on "
                    "terminal-state Claude Code sessions that spawns a local "
                    "terminal running the configured resume command. Opt-in "
                    "because spawning GUI terminals from a systemd service "
                    "depends on the operator's desktop environment."
    )
    cc_resume_cmd: str = Field(
        default="wezterm cli spawn --cwd {cwd} -- {inner_command}",
        alias="LIFEOS_CC_RESUME_CMD",
        description="Launcher command for resuming a Claude Code session. "
                    "Default uses `wezterm cli spawn` which opens a new tab "
                    "AND runs the resume command in one shot, no clipboard "
                    "paste needed. Wezterm prints the new pane id on stdout, "
                    "which the /agents page stores so the new `Focus` action "
                    "can call `wezterm cli activate-pane` to revisit the tab. "
                    "Substitutions: `{session_id}`, `{cwd}`, `{inner_command}` "
                    "(rendered LIFEOS_CC_RESUME_INNER_CMD), and URL-encoded "
                    "`{session_id_url}` / `{cwd_url}`. Parsed with shlex.split "
                    "— no shell metacharacters."
    )
    cc_resume_inner_cmd: str = Field(
        default="claude --dangerously-skip-permissions --resume {session_id}",
        alias="LIFEOS_CC_RESUME_INNER_CMD",
        description="Command run *inside* the spawned terminal — the actual "
                    "`claude --resume` invocation. The default template for "
                    "LIFEOS_CC_RESUME_CMD substitutes this in via "
                    "`{inner_command}` so wezterm launches the tab with the "
                    "resume already running. Substitutions: `{session_id}`, "
                    "`{cwd}`. Set to empty to skip the inner command (spawn "
                    "an empty terminal)."
    )
    cc_resume_env_file: str = Field(
        default="",
        alias="LIFEOS_CC_RESUME_ENV_FILE",
        description="Optional path to a `key=value` file pinning DISPLAY / "
                    "XAUTHORITY / WAYLAND_DISPLAY / DBUS_SESSION_BUS_ADDRESS "
                    "for the spawned terminal. Leave empty to inherit the "
                    "systemd service's env (which usually has none of these)."
    )
    agent_vault_id: str = Field(
        default="",
        alias="LIFEOS_AGENT_VAULT_ID",
        description="Managed Agents Vault id holding OAuth credentials for "
                    "MCP servers declared in the agent preset (Gmail / Calendar "
                    "/ Drive / Superhuman / custom MCPs). Created in the "
                    "Anthropic console; see docs/guides/agent-worker-setup.md. "
                    "Passed as the sole entry in `vault_ids` on session create."
    )
    agent_preset_id: str = Field(
        default="",
        alias="LIFEOS_AGENT_PRESET_ID",
        description="Managed Agents Agent preset id (e.g. 'agent_…'). Created "
                    "once in the Anthropic console; the preset holds the model, "
                    "system prompt, MCP servers, and tools for every Claude-"
                    "routed session. See docs/guides/agent-worker-setup.md for "
                    "the YAML pattern."
    )
    agent_environment_id: str = Field(
        default="",
        alias="LIFEOS_AGENT_ENVIRONMENT_ID",
        description="Managed Agents Environment id (e.g. 'env_…'). Created in "
                    "the Anthropic console; controls where tool calls execute "
                    "(cloud container by default, self-hosted sandbox in #111)."
    )
    # Deprecated: kept so existing .env files don't error on parse. Both fields
    # were used by the pre-refactor driver to build per-session MCP / connector
    # lists. The real Managed Agents API expects those to live in the agent
    # preset (configured in the console), not in the session-create body.
    agent_connectors: str = Field(
        default="",
        alias="LIFEOS_AGENT_CONNECTORS",
        description="Deprecated. MCP servers and connectors now live in the "
                    "agent preset (LIFEOS_AGENT_PRESET_ID), not in session "
                    "creation. This field is parsed but unused; safe to remove "
                    "from your .env."
    )
    agent_extra_mcp_servers: str = Field(
        default="",
        alias="LIFEOS_AGENT_EXTRA_MCP_SERVERS",
        description="Deprecated. MCP servers now live in the agent preset "
                    "(LIFEOS_AGENT_PRESET_ID), not in session creation. This "
                    "field is parsed but unused; safe to remove from your .env."
    )
    mcp_http_url: str = Field(
        default="",
        alias="LIFEOS_MCP_HTTP_URL",
        description="Public hostname for the LifeOS MCP HTTP transport "
                    "(e.g. 'https://mcp.example.com/mcp'). Required for "
                    "Managed Agents to reach LifeOS data."
    )
    # Inter-agent caps (Issue E). Spawn / lineage / concurrency limits.
    agent_max_spawn_depth: int = Field(
        default=3,
        alias="LIFEOS_AGENT_MAX_SPAWN_DEPTH",
        description="Max depth of the spawn tree (parent → child → grandchild)."
    )
    agent_max_descendants_per_root: int = Field(
        default=50,
        alias="LIFEOS_AGENT_MAX_DESCENDANTS_PER_ROOT",
        description="Total descendants allowed under any single root session."
    )
    agent_max_concurrent_local: int = Field(
        default=1,
        alias="LIFEOS_AGENT_MAX_CONCURRENT_LOCAL",
        description="Max concurrent local Gemma sessions (VRAM bound)."
    )
    agent_max_concurrent_managed: int = Field(
        default=10,
        alias="LIFEOS_AGENT_MAX_CONCURRENT_MANAGED",
        description="Max concurrent Managed Agents sessions."
    )
    local_llm_autostart: bool = Field(
        default=False,
        alias="LIFEOS_LOCAL_LLM_AUTOSTART",
        description="Enable systemd auto-start on boot and crash-restart for local LLM. "
                    "When false, llama-server must be started manually."
    )

    # Local LLM Router — historically Ollama-named; now consumed by the
    # llama-server-backed summarizer / fact-validation paths after the
    # 2026-05 migration. The env var aliases (OLLAMA_HOST / OLLAMA_MODEL /
    # OLLAMA_TIMEOUT / OLLAMA_RETRY_TIMEOUT) are kept so existing operator
    # .env files don't need to change; only ``ollama_timeout`` /
    # ``ollama_retry_timeout`` are still read in code (as generic request
    # timeouts), ``ollama_host`` / ``ollama_model`` are vestigial.
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")
    ollama_model: str = Field(default="gemma4:26b", alias="OLLAMA_MODEL")
    ollama_timeout: int = Field(default=45, alias="OLLAMA_TIMEOUT")
    ollama_retry_timeout: int = Field(default=60, alias="OLLAMA_RETRY_TIMEOUT")  # Longer timeout for retries

    # Cross-encoder re-ranking (P9.2)
    # Query-aware reranking: protects BM25 exact matches for factual queries
    # Override via LIFEOS_RERANKER_MODEL; set LIFEOS_RERANKER_ENABLED=false to disable
    reranker_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L6-v2",
        alias="LIFEOS_RERANKER_MODEL"
    )
    reranker_enabled: bool = Field(
        default=True,
        alias="LIFEOS_RERANKER_ENABLED"
    )
    reranker_candidates: int = 50

    # Notifications
    alert_email: str = Field(
        default="",
        alias="LIFEOS_ALERT_EMAIL",
        description="Email address for sync failure alerts"
    )

    # Slack Integration
    slack_client_id: str = Field(default="", alias="SLACK_CLIENT_ID")
    slack_client_secret: str = Field(default="", alias="SLACK_CLIENT_SECRET")
    slack_redirect_uri: str = Field(
        default="http://localhost:8000/api/crm/slack/callback",
        alias="SLACK_REDIRECT_URI"
    )

    # Work email domain for CRM category detection
    work_email_domain: str = Field(
        default="",
        alias="LIFEOS_WORK_DOMAIN",
        description="Your work email domain (e.g., yourcompany.com) for categorizing work contacts"
    )
    work_email_domain_2: str = Field(
        default="",
        alias="LIFEOS_WORK_DOMAIN_2",
        description="Second work email domain (e.g., othercompany.com) for categorizing work contacts"
    )

    # ==========================================================================
    # WORK INTEGRATION TOGGLES
    # ==========================================================================
    # These control whether work account data is synced. All default to False
    # for safety - work data will NOT be indexed unless explicitly enabled.
    # This protects users who may not realize work data could be sent to
    # Claude API during synthesis operations.
    # ==========================================================================

    sync_work_gmail: bool = Field(
        default=False,
        alias="LIFEOS_SYNC_WORK_GMAIL",
        description="Enable syncing work Gmail account (requires work_email_domain)"
    )
    sync_work_calendar: bool = Field(
        default=False,
        alias="LIFEOS_SYNC_WORK_CALENDAR",
        description="Enable syncing work Google Calendar (requires work_email_domain)"
    )
    sync_work2_gmail: bool = Field(
        default=False,
        alias="LIFEOS_SYNC_WORK2_GMAIL",
        description="Enable syncing second work Gmail account (requires work_email_domain_2)"
    )
    sync_work2_calendar: bool = Field(
        default=False,
        alias="LIFEOS_SYNC_WORK2_CALENDAR",
        description="Enable syncing second work Google Calendar (requires work_email_domain_2)"
    )
    sync_slack: bool = Field(
        default=False,
        alias="LIFEOS_SYNC_SLACK",
        description="Enable syncing Slack workspace messages"
    )

    # Timezone (IANA format)
    timezone: str = Field(
        default="America/New_York",
        alias="LIFEOS_TIMEZONE",
        description="IANA timezone for schedules, reminders, and AI context"
    )

    # User name for fact extraction prompts
    user_name: str = Field(
        default="User",
        alias="LIFEOS_USER_NAME",
        description="Your name for fact extraction prompts"
    )

    # CRM Owner (the user's person ID for relationship tracking)
    # WARNING: This ID is from people_entities.json and must remain stable.
    # If you rebuild people_entities.json from scratch, this ID will become
    # invalid and you'll need to find your new ID and update this value.
    # See data/README.md for why you should NEVER rebuild from scratch.
    my_person_id: str = Field(
        default="",
        alias="LIFEOS_MY_PERSON_ID",
        description="Your PersonEntity ID for relationship tracking"
    )

    # Apple Photos Integration
    photos_library_path: str = Field(
        default="~/Pictures/Photos Library.photoslibrary",
        alias="LIFEOS_PHOTOS_PATH",
        description="Path to Photos Library"
    )

    @property
    def work_email_domains(self) -> list[str]:
        """All configured work email domains."""
        return [d for d in [self.work_email_domain, self.work_email_domain_2] if d]

    def is_sync_enabled(self, account: str, service: str) -> bool:
        """Check if sync is enabled for account+service (gmail/calendar)."""
        if account == "work":
            return self.sync_work_gmail if service == "gmail" else self.sync_work_calendar
        elif account == "work2":
            return self.sync_work2_gmail if service == "gmail" else self.sync_work2_calendar
        return True  # personal always enabled

    # Personal relationship patterns for Granola meeting routing
    # Regex patterns (pipe-separated) to match meeting titles for routing to Personal/Relationship
    # Example: "Partner|Spouse|Wife|Husband" or specific names
    personal_relationship_patterns: str = Field(
        default="",
        alias="LIFEOS_PERSONAL_RELATIONSHIP_PATTERNS",
        description="Pipe-separated regex patterns for personal relationship meeting routing"
    )

    # Partner name for relationship features
    partner_name: str = Field(
        default="Partner",
        alias="LIFEOS_PARTNER_NAME",
        description="Partner's name for relationship insights"
    )

    # Therapist patterns for meeting classification (pipe-separated full names)
    therapist_patterns: str = Field(
        default="",
        alias="LIFEOS_THERAPIST_PATTERNS",
        description="Pipe-separated therapist names for meeting routing (e.g., 'Dr. Smith|Jane Doe')"
    )

    # Current work vault path (include trailing slash)
    current_work_path: str = Field(
        default="Work/",
        alias="LIFEOS_CURRENT_WORK_PATH",
        description="Vault path prefix for current work"
    )

    # Personal archive path (include trailing slash)
    personal_archive_path: str = Field(
        default="Personal/zArchive/",
        alias="LIFEOS_PERSONAL_ARCHIVE_PATH",
        description="Vault path prefix for archived personal items"
    )

    # Relationship folder name (for partner-specific content)
    relationship_folder: str = Field(
        default="Relationship",
        alias="LIFEOS_RELATIONSHIP_FOLDER",
        description="Folder name under Personal/ for relationship content"
    )

    # Telegram Bot
    telegram_bot_token: str = Field(
        default="",
        alias="TELEGRAM_BOT_TOKEN",
        description="Telegram bot token from @BotFather"
    )
    telegram_chat_id: str = Field(
        default="",
        alias="TELEGRAM_CHAT_ID",
        description="Telegram chat ID for receiving messages"
    )

    # Fitness
    fitness_sheet_id: str = Field(
        default="",
        alias="LIFEOS_FITNESS_SHEET_ID",
        description="Google Sheet ID to mirror the workout log into (optional; mirror is off if unset)"
    )
    health_export_path: str = Field(
        default="data/apple-imports/health.json",
        alias="LIFEOS_HEALTH_EXPORT_PATH",
        description="Path to the Apple Health export JSON written by the iOS Shortcut "
                    "(e.g. a synced ~/Code/Sync/health/health.json). Imported nightly."
    )
    health_ingest_token: str = Field(
        default="",
        alias="LIFEOS_HEALTH_INGEST_TOKEN",
        description="Bearer token for POST /api/fitness/health/ingest (the HealthBridge "
                    "app's POST delivery mode). Empty disables the endpoint (503). "
                    "Generate with `openssl rand -hex 32`."
    )

    # Monarch Money
    monarch_email: str = Field(
        default="",
        alias="MONARCH_EMAIL",
        description="Monarch Money account email"
    )
    monarch_password: str = Field(
        default="",
        alias="MONARCH_PASSWORD",
        description="Monarch Money account password"
    )

    # Backup directory
    backup_path: str = Field(
        default="./data/backups",
        alias="LIFEOS_BACKUP_PATH",
        description="Directory for database backups (use fast storage like NVMe)"
    )

    # Claude Code orchestration
    claude_binary: str = Field(
        default="claude",
        alias="LIFEOS_CLAUDE_BINARY",
        description="Path to claude CLI binary (or just 'claude' if on PATH)"
    )
    claude_timeout_seconds: int = Field(
        default=3600,
        alias="LIFEOS_CLAUDE_TIMEOUT",
        description="Safety-net timeout for Claude Code sessions (seconds). Heartbeats keep user informed; this is a backstop."
    )
    claude_max_turns: int = Field(
        default=50,
        alias="LIFEOS_CLAUDE_MAX_TURNS",
        description="Max agentic turns per Claude Code session. Prevents runaway retry loops."
    )
    claude_max_cost_usd: float = Field(
        default=2.0,
        alias="LIFEOS_CLAUDE_MAX_COST",
        description="Max cost in USD per Claude Code session. Session cancelled if exceeded."
    )
    @property
    def telegram_enabled(self) -> bool:
        """Check if Telegram bot is configured."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def telegram_primary_bot(self) -> "TelegramBotConfig":
        """The default Telegram bot, driven by TELEGRAM_BOT_TOKEN/CHAT_ID.

        Named "primary" so its update-offset file stays the legacy
        ``data/telegram_state.json`` and existing behavior is unchanged.
        """
        persona, voice, model = _load_primary_persona()
        return TelegramBotConfig(
            name="primary",
            token=self.telegram_bot_token,
            chat_id=self.telegram_chat_id,
            persona=persona,
            voice=voice,
            model=model,
        )

    @property
    def telegram_bots(self) -> list["TelegramBotConfig"]:
        """Specialized Telegram bots beyond the primary, from the registry file.

        Each registry entry names an env var holding the bot's token; entries
        whose token is unset are skipped (logged) so a fresh clone with no extra
        tokens simply runs the primary bot. Persona text is loaded from the
        entry's ``persona_file``. ``chat_id_env`` is optional and defaults to the
        primary ``TELEGRAM_CHAT_ID`` (in Telegram DMs the chat id is your user
        id, identical across bots).
        """
        if not _TELEGRAM_BOTS_FILE.exists():
            return []
        try:
            entries = json.loads(_TELEGRAM_BOTS_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read {_TELEGRAM_BOTS_FILE}: {e}")
            return []
        if not isinstance(entries, list):
            logger.warning(f"{_TELEGRAM_BOTS_FILE} must contain a JSON list, ignoring")
            return []

        # pydantic-settings loads .env into the model but does NOT export to
        # os.environ, so resolve token/chat-id env vars from the .env file too
        # (real environment wins over .env on conflict).
        env_file = self.model_config.get("env_file", ".env")
        env: dict = {**dotenv_values(env_file), **os.environ}

        bots: list[TelegramBotConfig] = []
        seen: set[str] = set()
        for entry in entries:
            name = (entry.get("name") or "").strip().lower()
            if not name or not _BOT_NAME_RE.match(name):
                logger.warning(f"Skipping Telegram bot with invalid name: {entry.get('name')!r}")
                continue
            if name in ("primary",) or name in seen:
                logger.warning(f"Skipping duplicate/reserved Telegram bot name: {name!r}")
                continue
            token = (env.get(entry.get("token_env", "")) or "").strip()
            if not token:
                logger.info(
                    f"Telegram bot '{name}' skipped: env var "
                    f"{entry.get('token_env')!r} is unset"
                )
                continue
            chat_id = (env.get(entry.get("chat_id_env", "")) or "").strip() or self.telegram_chat_id
            persona, voice, model = "", (), ""
            persona_file = entry.get("persona_file")
            if persona_file:
                try:
                    persona, voice, model = _parse_persona(Path(persona_file).read_text(), name)
                except OSError as e:
                    logger.warning(f"Telegram bot '{name}': could not read persona file {persona_file}: {e}")
            label = (entry.get("label") or "").strip()
            seen.add(name)
            bots.append(TelegramBotConfig(
                name=name, token=token, chat_id=chat_id, persona=persona, label=label,
                orchestrates=bool(entry.get("orchestrates", False)),
                voice=voice, model=model,
            ))
        return bots

    def list_http_personas(self) -> list["PersonaInfo"]:
        """Chat personas visible to HTTP clients (web, voice/whisper-relay).

        Returns the primary persona plus every configured specialized bot from
        the registry whose token env is set (``telegram_bots`` already drops the
        unset ones). No secrets are exposed. The primary persona and any
        orchestrating bot (``orchestrates: true``, e.g. the doctor self-repair
        bot) advertise ``handoff``/``agent`` capabilities; pure-chat specialized
        bots advertise none. Adding a registry entry + its token env var surfaces
        a new persona on the next restart with no code change.
        """
        personas = [PersonaInfo(
            id="primary",
            label="Primary",
            capabilities=list(ORCHESTRATOR_PERSONA_CAPABILITIES),
        )]
        for bot in self.telegram_bots:
            personas.append(PersonaInfo(
                id=bot.name,
                label=bot.label or bot.name.capitalize(),
                capabilities=(
                    list(ORCHESTRATOR_PERSONA_CAPABILITIES) if bot.orchestrates else []
                ),
            ))
        return personas

    def resolve_persona(self, persona_id: str) -> "str | None":
        """Resolve a persona id to its system-prompt preamble for HTTP clients.

        Same registry source as Telegram, so ``persona_id="fitness"`` yields the
        exact preamble the fitness Telegram bot uses. ``"primary"`` resolves to
        an empty preamble (the default, no persona). Returns ``None`` for an
        unknown id so the caller can reject it with HTTP 400.
        """
        if persona_id == "primary":
            return _load_primary_persona()[0]
        for bot in self.telegram_bots:
            if bot.name == persona_id:
                return bot.persona
        return None

    def persona_voice(self, persona_id: str) -> "tuple[str, ...]":
        """Spoken-turn rules for a persona id; empty tuple if none or unknown.

        Appended to the system prompt on voice turns (the `modality` flag on
        /api/ask/stream). Same registry source as resolve_persona.
        """
        if persona_id == "primary":
            return _load_primary_persona()[1]
        for bot in self.telegram_bots:
            if bot.name == persona_id:
                return bot.voice
        return ()

    def personal_context(self, persona_id: str) -> str:
        """A resolved people block for a persona, from existing config.

        Scoped to the therapist (the surface built around the user's relationships
        and therapy). Names come from LIFEOS_PARTNER_NAME / LIFEOS_THERAPIST_PATTERNS
        — never hardcoded in a persona file, and resolved only at runtime so the
        committed repo stays clean. Empty for other personas and for a fresh clone
        with no config set.
        """
        if persona_id != "therapist":
            return ""
        lines = []
        partner = (self.partner_name or "").strip()
        if partner and partner.lower() != "partner":
            lines.append(f"- Partner: {partner}")
        therapists = [t.strip() for t in re.split(r"[|,]", self.therapist_patterns or "") if t.strip()]
        if therapists:
            lines.append(f"- Therapists (individual + couples): {', '.join(therapists)}")
        if not lines:
            return ""
        return (
            "## Your people (resolved from config)\n\n"
            + "\n".join(lines)
            + "\n\nThese are the actual people behind this surface — search their "
            "sessions and messages directly rather than asking who they are."
        )

    def persona_orchestrates(self, persona_id: str) -> bool:
        """True if the persona drives a Claude Code session instead of inline chat.

        Orchestrating bots (e.g. doctor) spawn a worker rather than answering
        inline. The primary persona is NOT an orchestrator (it uses the inline
        loop + claude_intent handoff for code tasks).
        """
        return any(b.name == persona_id and b.orchestrates for b in self.telegram_bots)

    @property
    def photos_db_path(self) -> str:
        """Get path to Photos.sqlite database."""
        return f"{self.photos_library_path}/database/Photos.sqlite"

    @property
    def photos_enabled(self) -> bool:
        """Check if Photos database is available."""
        from pathlib import Path
        return Path(self.photos_db_path).exists()


settings = Settings()
