"""Caller filter for the public MCP listener.

Tailscale Funnel cannot filter callers, so the public unix-socket listener
admits only the address ranges the Claude and ChatGPT apps publish. Tailscale
Serve sets `X-Forwarded-For` to the TCP source address it saw and strips any
incoming value, so the last entry of that header is the caller's address.

Modes (`LIFEOS_MCP_PUBLIC_CALLERS`): `off` (no filtering), `report` (log the
classification, admit everything), `enforce` (refuse callers outside the ranges).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MODES = ("off", "report", "enforce")

# Outbound IP addresses of the Claude apps' servers:
# https://platform.claude.com/docs/en/api/ip-addresses ("Outbound IP addresses")
ANTHROPIC_RANGES = ("160.79.104.0/21", "153.61.208.0/20")

DEFAULT_OPENAI_URL = "https://openai.com/chatgpt-connectors.json"
CACHE_FILENAME = "chatgpt-connectors.json"
REFRESH_INTERVAL_SECONDS = 12 * 3600
STALE_AFTER_SECONDS = 3 * 86400
STALE_NOTICE_INTERVAL_SECONDS = 24 * 3600
CREDENTIALS_NOTICE_INTERVAL_SECONDS = 3600
MIN_OPENAI_NETWORKS = 10
FETCH_TIMEOUT_SECONDS = 20.0
FETCH_USER_AGENT = "Mozilla/5.0 (compatible; LifeOS)"
TICK_SECONDS = 3600
# While no ChatGPT list has loaded (first start, failed fetch), retry this often.
EMPTY_RETRY_SECONDS = 300

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network


def normalize_mode(raw: str | None) -> str:
    """The configured mode; anything but `report` or `enforce` behaves as `off`."""
    value = (raw or "off").strip().lower()
    if value in MODES:
        return value
    logger.warning("LIFEOS_MCP_PUBLIC_CALLERS=%r is not off, report or enforce; using off", raw)
    return "off"


def parse_networks(items, *, source: str) -> list[_Net]:
    """Parse CIDR strings, skipping (and warning about) invalid entries."""
    nets: list[_Net] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            nets.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            logger.warning("%s: skipping invalid network %r", source, text[:64])
    return nets


def parse_openai_list(raw: bytes | str) -> list[_Net] | None:
    """Networks from the ChatGPT connectors file, or None when it is not a usable list."""
    try:
        doc = json.loads(raw)
        prefixes = doc["prefixes"]
        if not isinstance(prefixes, list):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    nets: list[_Net] = []
    for entry in prefixes:
        if not isinstance(entry, dict):
            continue
        text = entry.get("ipv4Prefix") or entry.get("ipv6Prefix")
        if not isinstance(text, str):
            continue
        try:
            nets.append(ipaddress.ip_network(text.strip(), strict=False))
        except ValueError:
            continue
    return nets if len(nets) >= MIN_OPENAI_NETWORKS else None


def forwarded_address(headers) -> str | None:
    """The last `X-Forwarded-For` entry, as a normalized IP string, else None."""
    value = None
    for name, val in headers:
        if name.lower() == b"x-forwarded-for":
            value = val.decode("latin-1")
    if not value:
        return None
    last = value.split(",")[-1].strip()
    try:
        return str(ipaddress.ip_address(last))
    except ValueError:
        return None


def _fetch_openai(url: str) -> bytes:
    import httpx

    resp = httpx.get(
        url, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True,
        headers={"User-Agent": FETCH_USER_AGENT, "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.content


class PublicCallerFilter:
    def __init__(
        self,
        mode: str,
        *,
        extra_ranges: list[_Net] | None = None,
        cache_path: Path | None = None,
        openai_url: str = DEFAULT_OPENAI_URL,
        fetch: Callable[[str], bytes] | None = None,
        notify: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.mode = mode
        self._anthropic = tuple(parse_networks(ANTHROPIC_RANGES, source="anthropic"))
        self._extra = tuple(extra_ranges or ())
        self._openai: tuple[_Net, ...] = ()
        self._cache_path = cache_path
        self._openai_url = openai_url
        self._fetch = fetch or _fetch_openai
        self._notify = notify
        self._clock = clock
        self._last_refresh: float | None = None
        self._last_attempt: float | None = None
        self._lock = threading.Lock()
        self._last_credentials_notice: float | None = None
        self._last_stale_notice: float | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-public-notice")
        self._thread: threading.Thread | None = None
        self._load_cache()

    @classmethod
    def from_settings(cls, **overrides) -> "PublicCallerFilter":
        from config.settings import settings

        extra = parse_networks(
            (settings.mcp_public_caller_extra_ranges or "").split(","),
            source="LIFEOS_MCP_PUBLIC_CALLER_EXTRA_RANGES",
        )
        kwargs = dict(
            extra_ranges=extra,
            cache_path=Path(settings.chroma_path).parent / CACHE_FILENAME,
            openai_url=(settings.mcp_public_caller_openai_url or DEFAULT_OPENAI_URL).strip(),
        )
        kwargs.update(overrides)
        return cls(normalize_mode(settings.mcp_public_callers), **kwargs)

    # -- classification -------------------------------------------------

    def classify(self, addr: str | None) -> str:
        if not addr:
            return "unknown"
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return "unknown"
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        for label, nets in (
            ("anthropic", self._anthropic), ("openai", self._openai), ("extra", self._extra),
        ):
            if any(ip.version == n.version and ip in n for n in nets):
                return label
        return "outside"

    # -- OpenAI list ------------------------------------------------------

    def _load_cache(self) -> None:
        path = self._cache_path
        if path is None:
            return
        try:
            nets = parse_openai_list(path.read_bytes())
            mtime = path.stat().st_mtime
        except OSError:
            return
        if nets is None:
            logger.warning("ChatGPT address list cache is unusable; it will be fetched again")
            return
        self._openai = tuple(nets)
        self._last_refresh = mtime

    def refresh(self) -> bool:
        """Fetch the list now; True when a valid list replaced the current one."""
        self._last_attempt = self._clock()
        try:
            raw = self._fetch(self._openai_url)
        except Exception as exc:  # network, HTTP status, anything
            logger.warning("ChatGPT address list fetch failed: %s", type(exc).__name__)
            return False
        nets = parse_openai_list(raw)
        if nets is None:
            logger.warning("ChatGPT address list fetch returned an unusable file; keeping the last list")
            return False
        if self._cache_path is not None:
            try:
                self._write_cache(raw if isinstance(raw, bytes) else raw.encode())
            except OSError as exc:
                logger.warning("ChatGPT address list cache write failed: %s", type(exc).__name__)
        self._openai = tuple(nets)
        self._last_refresh = self._clock()
        return True

    def _write_cache(self, raw: bytes) -> None:
        path = self._cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def next_tick_seconds(self) -> int:
        """Seconds until the next tick: soon while no ChatGPT list is loaded, else hourly."""
        return EMPTY_RETRY_SECONDS if not self._openai else TICK_SECONDS

    def tick(self) -> None:
        """Refresh the list when due, then send the stale-list notice when due."""
        now = self._clock()
        if (not self._openai or self._last_refresh is None
                or now - self._last_refresh >= REFRESH_INTERVAL_SECONDS):
            self.refresh()
        self._check_stale()

    def _check_stale(self) -> None:
        if self.mode == "off":
            return
        now = self._clock()
        if self._last_refresh is not None and now - self._last_refresh <= STALE_AFTER_SECONDS:
            return
        with self._lock:
            if (self._last_stale_notice is not None
                    and now - self._last_stale_notice < STALE_NOTICE_INTERVAL_SECONDS):
                return
            self._last_stale_notice = now
        if self._last_refresh is None:
            age = "has never loaded"
        else:
            age = f"has not refreshed for {int((now - self._last_refresh) // 86400)} days"
        self._send(
            f"LifeOS connector: the ChatGPT address list {age}; "
            "ChatGPT calls may be refused if its addresses changed."
        )

    def start(self) -> None:
        """Start the daemon thread that keeps the OpenAI list fresh."""
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                try:
                    self.tick()
                except Exception:
                    logger.exception("ChatGPT address list refresher failed")
                time.sleep(self.next_tick_seconds())

        self._thread = threading.Thread(target=loop, name="mcp-public-callers", daemon=True)
        self._thread.start()

    # -- notices ----------------------------------------------------------

    def _send(self, text: str) -> None:
        self._executor.submit(self._deliver, text)

    def _deliver(self, text: str) -> None:
        try:
            notify = self._notify
            if notify is None:
                from api.services.mcp_oauth_http import telegram_notify as notify
            notify(text)
        except Exception:
            logger.exception("public caller notice failed to send")

    def flush(self) -> None:
        """Wait for queued notices (tests)."""
        self._executor.submit(lambda: None).result()

    def credentials_refused(self, addr: str | None) -> None:
        now = self._clock()
        with self._lock:
            if (self._last_credentials_notice is not None
                    and now - self._last_credentials_notice < CREDENTIALS_NOTICE_INTERVAL_SECONDS):
                return
            self._last_credentials_notice = now
        self._send(
            f"LifeOS connector: refused a call carrying app credentials from {addr or 'unknown'}, "
            "outside the Anthropic and OpenAI ranges. If Claude or ChatGPT stopped reaching "
            "LifeOS, set LIFEOS_MCP_PUBLIC_CALLERS=report and restart lifeos-mcp-http."
        )
