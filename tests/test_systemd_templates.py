"""Every placeholder in the systemd unit templates is substituted by setup-systemd.sh."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


def test_setup_systemd_substitutes_every_system_unit_placeholder():
    setup = (REPO_ROOT / "scripts" / "setup-systemd.sh").read_text()
    substituted = set(re.findall(r'-e "s\|(__[A-Z][A-Z0-9_]*__)\|', setup))
    for unit in sorted((REPO_ROOT / "config" / "systemd").glob("*.[st]*")):
        if not unit.is_file():
            continue
        missing = set(PLACEHOLDER.findall(unit.read_text())) - substituted
        assert not missing, f"{unit.name}: {sorted(missing)} never substituted"


def test_llm_context_size_is_a_setting_with_the_current_default():
    unit = (REPO_ROOT / "config" / "systemd" / "lifeos-llm.service").read_text()
    setup = (REPO_ROOT / "scripts" / "setup-systemd.sh").read_text()
    assert "-c __LLM_CONTEXT_SIZE__" in unit
    assert '_read_env "LIFEOS_LLM_CONTEXT_SIZE" "32768"' in setup


def test_llm_host_memory_caps_are_settings_with_small_defaults():
    unit = (REPO_ROOT / "config" / "systemd" / "lifeos-llm.service").read_text()
    setup = (REPO_ROOT / "scripts" / "setup-systemd.sh").read_text()
    assert "--cache-ram __LLM_CACHE_RAM_MIB__" in unit
    assert "--ctx-checkpoints __LLM_CTX_CHECKPOINTS__" in unit
    assert '_read_env "LIFEOS_LLM_CACHE_RAM_MIB" "1024"' in setup
    assert '_read_env "LIFEOS_LLM_CTX_CHECKPOINTS" "4"' in setup


def _oom_score_adjust(name: str) -> int:
    text = (REPO_ROOT / "config" / "systemd" / name).read_text()
    match = re.search(r"^OOMScoreAdjust=(-?\d+)$", text, re.MULTILINE)
    assert match, f"{name} sets no OOMScoreAdjust"
    return int(match.group(1))


def test_oom_adjustments_weigh_chromadb_then_the_model_server():
    chroma = _oom_score_adjust("lifeos-chromadb.service")
    llm = _oom_score_adjust("lifeos-llm.service")
    assert chroma < llm < 0


def test_the_api_sets_no_oom_adjustment_its_agent_sessions_would_inherit():
    text = (REPO_ROOT / "config" / "systemd" / "lifeos-api.service").read_text()
    assert "OOMScoreAdjust" not in text


def test_root_network_watchdog_runs_a_root_owned_copy():
    """The root unit executes the installed copy, never the user-writable checkout."""
    unit = (REPO_ROOT / "config" / "systemd" / "lifeos-network-watchdog.service").read_text()
    exec_lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert exec_lines == ["ExecStart=/usr/local/libexec/lifeos/network-watchdog.sh"]
    assert not any(line.startswith("User=") for line in unit.splitlines())
    assert "LIFEOS_NET_WATCHDOG_LOG=/var/log/lifeos-network-watchdog.log" in unit
    assert "LIFEOS_NET_WATCHDOG_STATE_DIR=/var/lib/lifeos-network-watchdog" in unit

    setup = (REPO_ROOT / "scripts" / "setup-systemd.sh").read_text()
    assert (
        'install -m 0755 -o root -g root "$SCRIPT_DIR/network-watchdog.sh" '
        '"$ROOT_LIBEXEC_DIR/network-watchdog.sh"'
    ) in setup
    assert 'ROOT_LIBEXEC_DIR="/usr/local/libexec/lifeos"' in setup
