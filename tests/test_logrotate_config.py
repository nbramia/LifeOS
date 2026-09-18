"""Validate the checked-in logrotate configuration."""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LOGROTATE_CONFIG = REPO_ROOT / "config" / "logrotate-lifeos.conf"


def _parse_stanzas(config: str) -> list[tuple[list[str], set[str]]]:
    return [
        (header.split(), set(body.splitlines()))
        for header, body in re.findall(r"(?m)^([^\n{]+)\s*\{([^}]*)\}", config)
    ]


@pytest.mark.unit
def test_server_and_worker_logs_use_copytruncate():
    stanzas = _parse_stanzas(LOGROTATE_CONFIG.read_text(encoding="utf-8"))
    log_paths = {
        "__LIFEOS_DIR__/logs/server.log",
        "__LIFEOS_DIR__/logs/agent-worker.log",
    }

    matching_directives = [
        {directive.strip() for directive in directives if directive.strip()}
        for patterns, directives in stanzas
        if all(any(fnmatch.fnmatch(path, pattern) for pattern in patterns) for path in log_paths)
    ]

    assert len(matching_directives) == 1
    assert {
        "daily",
        "rotate 7",
        "compress",
        "missingok",
        "notifempty",
        "copytruncate",
    } <= matching_directives[0]
