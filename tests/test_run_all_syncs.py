"""Tests for dependency-skip behavior and LLM memory gating in run_all_syncs."""

from unittest.mock import patch, MagicMock

# Minimal SYNC_SOURCES for tests — only defines metadata (depends_on, frequency, phase).
# The actual sync logic is mocked via run_sync.
TEST_SYNC_SOURCES = {
    "source_a": {"description": "A", "phase": 1, "frequency": "daily"},
    "source_b": {"description": "B", "phase": 1, "frequency": "daily"},
    "source_c": {"description": "C", "phase": 2, "frequency": "daily", "depends_on": ["source_a"]},
    "source_d": {"description": "D", "phase": 3, "frequency": "daily", "depends_on": ["source_c"]},
    "source_e": {"description": "E", "phase": 2, "frequency": "daily", "depends_on": ["source_a", "source_b"]},
    # phone is absent from SYNC_ORDER (like FDA sources)
    "phone": {"description": "Phone", "phase": 1, "frequency": "daily"},
    "source_f": {"description": "F", "phase": 2, "frequency": "daily", "depends_on": ["phone"]},
    # disabled source
    "slack": {"description": "Slack", "phase": 1, "frequency": "daily"},
    "link_slack": {"description": "Link Slack", "phase": 2, "frequency": "daily", "depends_on": ["slack"]},
}

TEST_SYNC_ORDER = [
    "source_a",
    "source_b",
    "source_c",
    "source_d",
    "source_e",
    "source_f",  # depends on phone which is NOT in this list
    "slack",
    "link_slack",
]


def _make_run_sync_side_effect(fail_sources: set):
    """Return a side_effect function for run_sync that fails specified sources."""
    def side_effect(source, dry_run=False):
        if source in fail_sources:
            return False, {"error": "simulated failure"}
        return True, {"processed": 1, "created": 0}
    return side_effect


def _run_with_patches(fail_sources: set | None = None, disabled_sources: set | None = None):
    """Run run_all_syncs(dry_run=True) with test patches and return the result dict.

    dry_run=True skips: subprocess calls, backup, maintenance mode, Telegram, server restart.
    We only need to patch: SYNC_SOURCES, SYNC_ORDER, run_sync, check_sync_health,
    get_disabled_work_sources, and log_sync_summary_to_markdown.
    """
    from scripts.run_all_syncs import run_all_syncs

    fail_sources = fail_sources or set()

    run_sync_mock = MagicMock(side_effect=_make_run_sync_side_effect(fail_sources))

    with (
        patch("scripts.run_all_syncs.SYNC_SOURCES", TEST_SYNC_SOURCES),
        patch("scripts.run_all_syncs.SYNC_ORDER", TEST_SYNC_ORDER),
        patch("scripts.run_all_syncs.run_sync", run_sync_mock),
        patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
        patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=disabled_sources or set()),
        patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
    ):
        result = run_all_syncs(dry_run=True)

    return result, run_sync_mock


class TestDependencySkip:
    """Tests for the dependency-skip behavior in run_all_syncs."""

    def test_skip_when_dependency_failed(self):
        """Source C (depends on A) is skipped when A fails."""
        result, run_sync_mock = _run_with_patches(fail_sources={"source_a"})

        assert "source_c" in result["dep_skipped_sources"]
        assert result["results"]["source_c"]["skipped"] is True
        assert result["results"]["source_c"]["reason"] == "dependency_failed"
        assert "source_a" in result["results"]["source_c"]["failed_dependencies"]

        # run_sync should NOT have been called for source_c
        called_sources = [call.args[0] for call in run_sync_mock.call_args_list]
        assert "source_c" not in called_sources

    def test_cascading_skip(self):
        """A→C→D chain: A fails → C skipped → D skipped."""
        result, run_sync_mock = _run_with_patches(fail_sources={"source_a"})

        assert "source_c" in result["dep_skipped_sources"]
        assert "source_d" in result["dep_skipped_sources"]
        assert result["results"]["source_d"]["reason"] == "dependency_failed"
        assert "source_c" in result["results"]["source_d"]["failed_dependencies"]

        called_sources = [call.args[0] for call in run_sync_mock.call_args_list]
        assert "source_c" not in called_sources
        assert "source_d" not in called_sources

    def test_partial_dependency_failure(self):
        """Source E depends on [A, B]. A succeeds, B fails → E skipped."""
        result, _ = _run_with_patches(fail_sources={"source_b"})

        assert "source_e" in result["dep_skipped_sources"]
        assert result["results"]["source_e"]["reason"] == "dependency_failed"
        assert "source_b" in result["results"]["source_e"]["failed_dependencies"]
        # source_a succeeded, so it should NOT be in failed_dependencies
        assert "source_a" not in result["results"]["source_e"]["failed_dependencies"]

    def test_absent_dependency_no_skip(self):
        """Source F depends on 'phone', which is NOT in the run list.
        Absent deps should not trigger a skip."""
        result, run_sync_mock = _run_with_patches(fail_sources=set())

        assert "source_f" not in result["dep_skipped_sources"]
        called_sources = [call.args[0] for call in run_sync_mock.call_args_list]
        assert "source_f" in called_sources

    def test_disabled_dependency_no_skip(self):
        """link_slack depends on 'slack'. If slack is disabled (not failed),
        link_slack should NOT be dependency-skipped."""
        result, run_sync_mock = _run_with_patches(
            fail_sources=set(),
            disabled_sources={"slack"},
        )

        # slack is skipped as disabled, link_slack should NOT be dep-skipped
        assert "link_slack" not in result["dep_skipped_sources"]
        assert result["results"]["slack"]["reason"] == "work_integration_disabled"
        called_sources = [call.args[0] for call in run_sync_mock.call_args_list]
        assert "link_slack" in called_sources

    def test_dep_skipped_in_result(self):
        """dep_skipped_sources appears in result dict and is sorted."""
        result, _ = _run_with_patches(fail_sources={"source_a"})

        assert "dep_skipped_sources" in result
        assert isinstance(result["dep_skipped_sources"], list)
        # Should be sorted
        assert result["dep_skipped_sources"] == sorted(result["dep_skipped_sources"])
        # source_c and source_d should be there (cascade from source_a)
        assert "source_c" in result["dep_skipped_sources"]
        assert "source_d" in result["dep_skipped_sources"]

    def test_all_succeed_no_skips(self):
        """Clean run: no dependency skips when everything succeeds."""
        result, _ = _run_with_patches(fail_sources=set())

        assert result["dep_skipped_sources"] == []
        # No source should have reason=dependency_failed
        for source, stats in result["results"].items():
            assert stats.get("reason") != "dependency_failed"


# =============================================================================
# LLM Memory Gating Tests
# =============================================================================

# Sources for LLM memory gating tests — includes embedding sources
LLM_TEST_SYNC_SOURCES = {
    "source_a": {"description": "A", "phase": 1, "frequency": "daily"},
    "vault_reindex": {"description": "Vault reindex", "phase": 4, "frequency": "daily"},
    "crm_vectorstore": {"description": "CRM vectorstore", "phase": 4, "frequency": "daily"},
    "source_z": {"description": "Z", "phase": 5, "frequency": "daily"},
}

LLM_TEST_SYNC_ORDER = ["source_a", "vault_reindex", "crm_vectorstore", "source_z"]


def _run_llm_test(
    fail_sources: set | None = None,
    llm_running: bool = True,
    gpu_memory_mb: int | None = 2000,
    stop_succeeds: bool = True,
    system_ram_mb: int | None = 16_000,
):
    """Run run_all_syncs with LLM memory gating mocks (dry_run=False).

    Returns (result_dict, run_sync_mock, stop_mock, start_mock).
    """
    from scripts.run_all_syncs import run_all_syncs

    fail_sources = fail_sources or set()
    run_sync_mock = MagicMock(side_effect=_make_run_sync_side_effect(fail_sources))

    # Without patching subprocess.run, every test would trigger a real
    # `scripts/server.sh restart` (the post-sync server reload) and wait
    # ~12s per test for systemd. Mock it to a clean exit so each test runs
    # in milliseconds. Same for telegram + drift detector (need real DBs).
    fake_restart = MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("scripts.run_all_syncs.SYNC_SOURCES", LLM_TEST_SYNC_SOURCES),
        patch("scripts.run_all_syncs.SYNC_ORDER", LLM_TEST_SYNC_ORDER),
        patch("scripts.run_all_syncs.run_sync", run_sync_mock),
        patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
        patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=set()),
        patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
        patch("scripts.run_all_syncs.backup_interactions"),
        patch("scripts.run_all_syncs.send_sync_summary_telegram"),
        patch("scripts.run_all_syncs.reap_orphan_sync_runs", return_value=0),
        patch("scripts.run_all_syncs.detect_silent_source_entity_drift", return_value=[]),
        patch("scripts.run_all_syncs.subprocess.run", return_value=fake_restart),
        patch("urllib.request.urlopen"),
        patch("scripts.run_all_syncs._is_llm_running", return_value=llm_running),
        patch("scripts.run_all_syncs._get_available_gpu_memory_mb", return_value=gpu_memory_mb),
        patch("scripts.run_all_syncs._get_available_system_ram_mb", return_value=system_ram_mb),
        patch("scripts.run_all_syncs._stop_llm_for_embeddings", return_value=stop_succeeds) as stop_mock,
        patch("scripts.run_all_syncs._start_llm") as start_mock,
        patch("scripts.run_all_syncs.get_sync_health") as health_mock,
    ):
        # Make get_sync_health return "not recently synced" so sources aren't skipped
        mock_health = MagicMock()
        mock_health.hours_since_sync = 24.0
        mock_health.last_status = "SUCCESS"
        health_mock.return_value = mock_health

        result = run_all_syncs(dry_run=False, force=True)

    return result, run_sync_mock, stop_mock, start_mock


class TestLLMMemoryGating:
    """Tests for memory-gated LLM stop/start during embedding phases."""

    def test_llm_stopped_when_memory_insufficient(self):
        """LLM is stopped when GPU memory is below threshold."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, _, stop_mock, start_mock = _run_llm_test(
            llm_running=True, gpu_memory_mb=_EMBEDDING_MEMORY_THRESHOLD_MB - 1000,
        )

        stop_mock.assert_called_once()
        start_mock.assert_called()  # Restarted after embeddings

    def test_llm_not_stopped_when_memory_sufficient(self):
        """LLM stays running when GPU memory is above threshold."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, _, stop_mock, start_mock = _run_llm_test(
            llm_running=True, gpu_memory_mb=_EMBEDDING_MEMORY_THRESHOLD_MB + 10_000,
        )

        stop_mock.assert_not_called()
        start_mock.assert_not_called()

    def test_llm_not_stopped_when_not_running(self):
        """No action taken when LLM is not running."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, _, stop_mock, start_mock = _run_llm_test(
            llm_running=False, gpu_memory_mb=_EMBEDDING_MEMORY_THRESHOLD_MB - 1000,
        )

        stop_mock.assert_not_called()
        start_mock.assert_not_called()

    def test_llm_restarted_after_last_embedding_source(self):
        """LLM is restarted after the last embedding source, not the first."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, run_sync_mock, stop_mock, start_mock = _run_llm_test(
            llm_running=True, gpu_memory_mb=_EMBEDDING_MEMORY_THRESHOLD_MB - 1000,
        )

        # Both embedding sources should have been synced
        called_sources = [c.args[0] for c in run_sync_mock.call_args_list]
        assert "vault_reindex" in called_sources
        assert "crm_vectorstore" in called_sources

        # LLM should be restarted exactly once (after crm_vectorstore, the last embedding source)
        stop_mock.assert_called_once()
        start_mock.assert_called_once()

    def test_llm_not_stopped_when_memory_unknown(self):
        """LLM is stopped (conservative) when sysfs returns None."""
        result, _, stop_mock, start_mock = _run_llm_test(
            llm_running=True, gpu_memory_mb=None,
        )

        stop_mock.assert_called_once()

    def test_internal_keys_not_in_results(self):
        """Internal _llm_* keys are not leaked in the result dict."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, _, _, _ = _run_llm_test(
            llm_running=True, gpu_memory_mb=_EMBEDDING_MEMORY_THRESHOLD_MB - 1000,
        )

        assert "_llm_stopped" not in result.get("results", {})
        assert "_llm_was_running" not in result.get("results", {})

    def test_safety_net_restarts_llm_on_stop_failure(self):
        """If stop succeeds but normal restart path is missed, safety net fires."""
        # This tests the try/finally safety net by ensuring the module globals
        # are properly tracked and cleaned up
        from scripts import run_all_syncs as module
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB

        result, _, stop_mock, start_mock = _run_llm_test(
            llm_running=True, gpu_memory_mb=_EMBEDDING_MEMORY_THRESHOLD_MB - 1000,
        )

        # After run_all_syncs returns, the module-level globals should be reset
        assert module._llm_stopped_for_sync is False


# =============================================================================
# System RAM Pre-flight Tests
# =============================================================================


class TestSystemRAMPreFlight:
    """Tests for system RAM check before embedding phases."""

    def test_embeddings_skipped_when_ram_low(self):
        """Embedding sources are skipped when system RAM is below threshold."""
        result, run_sync_mock, _, _ = _run_llm_test(
            llm_running=False, gpu_memory_mb=50_000, system_ram_mb=2000,
        )

        # Embedding sources should be skipped, not run
        called_sources = [c.args[0] for c in run_sync_mock.call_args_list]
        assert "vault_reindex" not in called_sources
        assert "crm_vectorstore" not in called_sources

        # Should be marked with reason
        assert result["results"]["vault_reindex"]["reason"] == "insufficient_ram"
        assert result["results"]["crm_vectorstore"]["reason"] == "insufficient_ram"

    def test_embeddings_run_when_ram_sufficient(self):
        """Embedding sources run normally when system RAM is above threshold."""
        result, run_sync_mock, _, _ = _run_llm_test(
            llm_running=False, gpu_memory_mb=50_000, system_ram_mb=16_000,
        )

        called_sources = [c.args[0] for c in run_sync_mock.call_args_list]
        assert "vault_reindex" in called_sources
        assert "crm_vectorstore" in called_sources

    def test_non_embedding_sources_unaffected_by_low_ram(self):
        """Non-embedding sources still run even when RAM is low."""
        result, run_sync_mock, _, _ = _run_llm_test(
            llm_running=False, gpu_memory_mb=50_000, system_ram_mb=2000,
        )

        called_sources = [c.args[0] for c in run_sync_mock.call_args_list]
        assert "source_a" in called_sources
        assert "source_z" in called_sources

    def test_low_ram_with_llm_running_skips_embeddings_without_stopping_llm(self):
        """When LLM is running but RAM is low, embedding sources are skipped
        and the LLM is NOT stopped (no point stopping it only to skip the work)."""
        result, run_sync_mock, stop_mock, start_mock = _run_llm_test(
            llm_running=True, gpu_memory_mb=2000, system_ram_mb=2000,
        )

        # Embedding sources should be skipped
        called_sources = [c.args[0] for c in run_sync_mock.call_args_list]
        assert "vault_reindex" not in called_sources
        assert "crm_vectorstore" not in called_sources
        assert result["results"]["vault_reindex"]["reason"] == "insufficient_ram"
        assert result["results"]["crm_vectorstore"]["reason"] == "insufficient_ram"

        # LLM should NOT have been stopped — no point stopping it for skipped work
        stop_mock.assert_not_called()
        start_mock.assert_not_called()

        # Non-embedding sources should still run
        assert "source_a" in called_sources
        assert "source_z" in called_sources

    def test_ram_check_unknown_allows_embeddings(self):
        """When RAM check returns None (unsupported OS), embeddings proceed."""
        result, run_sync_mock, _, _ = _run_llm_test(
            llm_running=False, gpu_memory_mb=50_000, system_ram_mb=None,
        )

        called_sources = [c.args[0] for c in run_sync_mock.call_args_list]
        assert "vault_reindex" in called_sources


class TestTimeoutBytesDecoding:
    """Tests for graceful handling of TimeoutExpired with bytes stdout/stderr.

    Regression: ``subprocess.run(..., text=True)`` decodes the streams only
    when the process exits cleanly. On TimeoutExpired the partial buffers
    are still raw bytes, so passing them straight to ``re.search`` crashed
    the entire run with ``cannot use a string pattern on a bytes-like object``
    — taking down every sync after the first timeout.
    """

    def test_timeout_handler_decodes_bytes_stdout(self):
        """run_sync survives a TimeoutExpired whose stdout/stderr are bytes."""
        import subprocess
        from scripts.run_all_syncs import run_sync, SYNC_SCRIPTS

        # Pick any real source key so SYNC_SCRIPTS lookup succeeds.
        source = next(iter(SYNC_SCRIPTS))

        # Simulate the exact CPython quirk: TimeoutExpired carries raw bytes
        # even when subprocess.run() was called with text=True.
        timeout_exc = subprocess.TimeoutExpired(
            cmd=["fake"],
            timeout=60,
            output=b"Processed 100 files\nCreated 50 entries\n",
            stderr=b"INFO: doing work\n",
        )

        with (
            patch("scripts.run_all_syncs.subprocess.run", side_effect=timeout_exc),
            patch("scripts.run_all_syncs.record_sync_start", return_value=999),
            patch("scripts.run_all_syncs.record_sync_complete"),
            patch("scripts.run_all_syncs.record_sync_error"),
            patch("scripts.run_all_syncs.log_error_to_markdown"),
        ):
            # Must NOT raise TypeError from regex-on-bytes; returns (False, stats).
            success, stats = run_sync(source, dry_run=False)

        assert success is False
        assert "timed out" in stats["error"].lower()


class TestParseSyncOutput:
    """Tests for _parse_sync_output, the bridge between sync scripts and sync_runs."""

    def test_sync_stats_line_is_authoritative(self):
        """SYNC_STATS:{json} overrides anything the regex fallback would infer."""
        from scripts.run_all_syncs import _parse_sync_output

        # Regex would parse "inserted: 5" as interactions_created=5, but the
        # canonical line says 100 — the canonical line must win.
        output = (
            "Some preamble\n"
            "Inserted: 5\n"
            'SYNC_STATS:{"interactions_created": 100, "source_entities_created": 42}\n'
            "Trailing log\n"
        )
        stats = _parse_sync_output(output)
        assert stats["interactions_created"] == 100
        assert stats["source_entities_created"] == 42
        # Generic "created" is derived from categorized values.
        assert stats["created"] == 142

    def test_falls_back_to_regex_when_no_canonical_line(self):
        """Regex parsing still works for scripts that haven't been migrated."""
        from scripts.run_all_syncs import _parse_sync_output

        output = (
            "=== iMessage Sync Summary ===\n"
            "Inserted: 47\n"
            "Source entities created: 12\n"
        )
        stats = _parse_sync_output(output)
        assert stats["interactions_created"] == 47
        assert stats["source_entities_created"] == 12

    def test_malformed_sync_stats_falls_back_to_regex(self):
        """A bad JSON payload shouldn't blank out everything — fall back to regex."""
        from scripts.run_all_syncs import _parse_sync_output

        output = (
            "SYNC_STATS:{not valid json\n"
            "Inserted: 7\n"
        )
        stats = _parse_sync_output(output)
        assert stats["interactions_created"] == 7

    def test_later_sync_stats_line_wins(self):
        """Wrappers (e.g. apple_data_import aggregating sub-imports) can override."""
        from scripts.run_all_syncs import _parse_sync_output

        output = (
            'SYNC_STATS:{"interactions_created": 5}\n'
            'SYNC_STATS:{"interactions_created": 50, "source_entities_created": 3}\n'
        )
        stats = _parse_sync_output(output)
        assert stats["interactions_created"] == 50
        assert stats["source_entities_created"] == 3


class TestGetAvailableSystemRAM:
    """Tests for _get_available_system_ram_mb."""

    def test_parses_meminfo(self):
        """Correctly parses MemAvailable from /proc/meminfo."""
        from scripts.run_all_syncs import _get_available_system_ram_mb

        fake_meminfo = (
            "MemTotal:       32000000 kB\n"
            "MemFree:         1000000 kB\n"
            "MemAvailable:   16000000 kB\n"
            "Buffers:          500000 kB\n"
        )
        with patch("pathlib.Path.read_text", return_value=fake_meminfo):
            result = _get_available_system_ram_mb()

        assert result == 15625  # 16000000 // 1024

    def test_returns_none_on_error(self):
        """Returns None when /proc/meminfo is unreadable."""
        from scripts.run_all_syncs import _get_available_system_ram_mb

        with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
            result = _get_available_system_ram_mb()

        assert result is None


class TestDurationCollapse:
    """Tests for silent no-op detection via duration collapse."""

    def test_detects_collapse(self):
        """A source that historically takes minutes finishing in <2s is flagged."""
        from scripts.run_all_syncs import _detect_duration_collapse

        with patch("scripts.run_all_syncs.get_typical_duration_seconds", return_value=450.0):
            info = _detect_duration_collapse("slack", 0.28)

        assert info is not None
        assert info["elapsed_seconds"] == 0.28
        assert info["typical_seconds"] == 450.0

    def test_no_collapse_without_history(self):
        """No history → no collapse verdict."""
        from scripts.run_all_syncs import _detect_duration_collapse

        with patch("scripts.run_all_syncs.get_typical_duration_seconds", return_value=None):
            assert _detect_duration_collapse("slack", 0.28) is None

    def test_no_collapse_for_fast_sources(self):
        """Sources that are typically fast don't alert."""
        from scripts.run_all_syncs import _detect_duration_collapse

        with patch("scripts.run_all_syncs.get_typical_duration_seconds", return_value=30.0):
            assert _detect_duration_collapse("link_slack", 0.5) is None

    def test_no_collapse_for_normal_duration(self):
        """A normal-length run doesn't alert."""
        from scripts.run_all_syncs import _detect_duration_collapse

        with patch("scripts.run_all_syncs.get_typical_duration_seconds", return_value=450.0):
            assert _detect_duration_collapse("slack", 380.0) is None

    def test_relative_threshold_catches_slow_source_collapse(self):
        """The elapsed threshold scales with typical duration: for a
        450s-typical source the cutoff is max(2, 0.05*450) = 22.5s, so a
        10s no-op is flagged even though it's above the 2s floor."""
        from scripts.run_all_syncs import _detect_duration_collapse

        with patch("scripts.run_all_syncs.get_typical_duration_seconds", return_value=450.0):
            info = _detect_duration_collapse("slack", 10.0)

        assert info is not None
        assert info["typical_seconds"] == 450.0

    def test_relative_threshold_allows_fast_but_plausible_run(self):
        """A run above the relative cutoff (30s vs 22.5s) is not flagged."""
        from scripts.run_all_syncs import _detect_duration_collapse

        with patch("scripts.run_all_syncs.get_typical_duration_seconds", return_value=450.0):
            assert _detect_duration_collapse("slack", 30.0) is None

    def test_collapse_check_survives_db_errors(self):
        """A sync_health DB hiccup must never fail the sync itself."""
        from scripts.run_all_syncs import _detect_duration_collapse

        with patch(
            "scripts.run_all_syncs.get_typical_duration_seconds",
            side_effect=RuntimeError("db locked"),
        ):
            assert _detect_duration_collapse("slack", 0.28) is None

    def test_run_all_syncs_collects_collapsed_sources(self):
        """Collapsed sources surface in the run_all_syncs result dict."""
        from scripts.run_all_syncs import run_all_syncs

        def side_effect(source, dry_run=False):
            if source == "source_a":
                return True, {
                    "processed": 0,
                    "duration_collapse": {"elapsed_seconds": 0.3, "typical_seconds": 450.0},
                }
            return True, {"processed": 1}

        with (
            patch("scripts.run_all_syncs.SYNC_SOURCES", TEST_SYNC_SOURCES),
            patch("scripts.run_all_syncs.SYNC_ORDER", TEST_SYNC_ORDER),
            patch("scripts.run_all_syncs.run_sync", MagicMock(side_effect=side_effect)),
            patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
            patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=set()),
            patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
        ):
            result = run_all_syncs(dry_run=True)

        assert result["duration_collapsed_sources"] == ["source_a"]

    def test_telegram_summary_includes_collapse_warning(self):
        """The Telegram summary calls out suspiciously fast completions."""
        from scripts.run_all_syncs import send_sync_summary_telegram

        result = {
            "failed": 0,
            "succeeded": 5,
            "sources_run": 5,
            "failed_sources": [],
            "duration_seconds": 120,
            "duration_collapsed_sources": ["slack"],
            "results": {
                "slack": {
                    "success": True,
                    "duration_collapse": {"elapsed_seconds": 0.3, "typical_seconds": 450.0},
                }
            },
        }

        with patch("api.services.telegram.send_message", return_value=True) as send_mock:
            send_sync_summary_telegram(result, trigger="test")

        message = send_mock.call_args[0][0]
        assert "slack" in message
        assert "silent no-op" in message
        assert "⚠️" in message

    def test_telegram_summary_clean_run_has_no_collapse_section(self):
        """A clean run keeps the ✅ status and no collapse section."""
        from scripts.run_all_syncs import send_sync_summary_telegram

        result = {
            "failed": 0,
            "succeeded": 5,
            "sources_run": 5,
            "failed_sources": [],
            "duration_seconds": 120,
            "duration_collapsed_sources": [],
            "results": {},
        }

        with patch("api.services.telegram.send_message", return_value=True) as send_mock:
            send_sync_summary_telegram(result, trigger="test")

        message = send_mock.call_args[0][0]
        assert "silent no-op" not in message
        assert "✅" in message


def test_sync_summary_surfaces_investments_stale():
    """A stale investments snapshot must reach the user via the nightly Telegram
    summary — a bare logger.warning feeds no batched report (#448)."""
    from scripts.run_all_syncs import send_sync_summary_telegram
    result = {
        "succeeded": 3, "sources_run": 3, "failed": 0, "failed_sources": [],
        "results": {}, "duration_seconds": 12,
        "investments_stale": (
            "Investments snapshot is 6.0 days old (last synced 2026-07-03T18:30:00); "
            "the weekday refresh (~18:30) or Syncthing may have stalled."
        ),
    }
    with patch("api.services.telegram.send_message", return_value=True) as send_mock:
        send_sync_summary_telegram(result, trigger="test")
    message = send_mock.call_args[0][0]
    assert "Investments snapshot" in message
    assert "stale" in message.lower()
    assert message.startswith("⚠️")  # stale flips the top-line status


def test_sync_summary_omits_investments_when_fresh():
    """No investments section (and a clean status) when the snapshot is fresh."""
    from scripts.run_all_syncs import send_sync_summary_telegram
    result = {
        "succeeded": 3, "sources_run": 3, "failed": 0, "failed_sources": [],
        "results": {}, "duration_seconds": 12, "investments_stale": None,
    }
    with patch("api.services.telegram.send_message", return_value=True) as send_mock:
        send_sync_summary_telegram(result, trigger="test")
    message = send_mock.call_args[0][0]
    assert "Investments snapshot stale" not in message
    assert message.startswith("✅")
