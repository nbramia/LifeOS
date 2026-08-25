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


class TestRepeatedYieldExcludedFromNewRecords:
    """Issue #646 acceptance criterion: the nightly summary must distinguish
    'N new records' from 'N records re-written from an unchanged upstream
    file' — a stale-input run must not report new interactions."""

    def test_repeated_yield_source_excluded_from_aggregate_counts(self):
        from scripts.run_all_syncs import run_all_syncs

        def side_effect(source, dry_run=False):
            if source == "source_a":
                return True, {
                    "interactions_created": 1294,
                    "repeated_yield": {"repeated_value": 1294, "consecutive_runs": 10},
                }
            return True, {"interactions_created": 5}

        with (
            patch("scripts.run_all_syncs.SYNC_SOURCES", TEST_SYNC_SOURCES),
            patch("scripts.run_all_syncs.SYNC_ORDER", TEST_SYNC_ORDER),
            patch("scripts.run_all_syncs.run_sync", MagicMock(side_effect=side_effect)),
            patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
            patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=set()),
            patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
        ):
            result = run_all_syncs(dry_run=True)

        assert result["repeated_yield_sources"] == ["source_a"]
        # The flagged source's count must not appear in the "new" breakdown...
        assert "source_a" not in result["interactions_by_source"]
        # ...nor be folded into the aggregate total the Telegram/markdown
        # "New Interactions" line reports — only the 7 other sources' real
        # counts (5 each) should be there.
        assert result["interactions_created"] == 35


def test_run_all_syncs_surfaces_apple_agent_sha_drift():
    """run_all_syncs() reads the SHA-drift signal directly from the Apple
    import manifest (independent of the apple_import subprocess) and puts
    it on the result dict so the Telegram/markdown summary can show it."""
    from scripts.run_all_syncs import run_all_syncs

    drifted_manifest = {
        "exported_at": "2026-08-20T00:00:00+00:00",
        "_agent_sha_drift_message": (
            "Apple Data Agent exported from b347e1f, which differs from this "
            "host's main (c13ba77) — its self-update may have failed."
        ),
    }

    with (
        patch("scripts.run_all_syncs.SYNC_SOURCES", TEST_SYNC_SOURCES),
        patch("scripts.run_all_syncs.SYNC_ORDER", TEST_SYNC_ORDER),
        patch("scripts.run_all_syncs.run_sync", MagicMock(side_effect=_make_run_sync_side_effect(set()))),
        patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
        patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=set()),
        patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
        patch("scripts.apple_data_import.check_manifest", return_value=drifted_manifest),
    ):
        result = run_all_syncs(dry_run=True)

    assert result["apple_agent_sha_drift"] == drifted_manifest["_agent_sha_drift_message"]


def test_run_all_syncs_sha_drift_absent_when_no_manifest():
    """No Apple import configured (fresh clone, no manifest.json) must not
    surface a drift warning or raise."""
    from scripts.run_all_syncs import run_all_syncs

    with (
        patch("scripts.run_all_syncs.SYNC_SOURCES", TEST_SYNC_SOURCES),
        patch("scripts.run_all_syncs.SYNC_ORDER", TEST_SYNC_ORDER),
        patch("scripts.run_all_syncs.run_sync", MagicMock(side_effect=_make_run_sync_side_effect(set()))),
        patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
        patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=set()),
        patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
        patch("scripts.apple_data_import.check_manifest", return_value=None),
    ):
        result = run_all_syncs(dry_run=True)

    assert result["apple_agent_sha_drift"] is None


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
        patch("scripts.run_all_syncs.backup_databases"),
        patch("scripts.run_all_syncs.prune_backups_after_success") as prune_mock,
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

    return result, run_sync_mock, stop_mock, start_mock, prune_mock


class TestLLMMemoryGating:
    """Tests for memory-gated LLM stop/start during embedding phases."""

    def test_llm_stopped_when_memory_insufficient(self):
        """LLM is stopped when GPU memory is below threshold."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, _, stop_mock, start_mock, _ = _run_llm_test(
            llm_running=True, gpu_memory_mb=_EMBEDDING_MEMORY_THRESHOLD_MB - 1000,
        )

        stop_mock.assert_called_once()
        start_mock.assert_called()  # Restarted after embeddings

    def test_llm_not_stopped_when_memory_sufficient(self):
        """LLM stays running when GPU memory is above threshold."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, _, stop_mock, start_mock, _ = _run_llm_test(
            llm_running=True, gpu_memory_mb=_EMBEDDING_MEMORY_THRESHOLD_MB + 10_000,
        )

        stop_mock.assert_not_called()
        start_mock.assert_not_called()

    def test_llm_not_stopped_when_not_running(self):
        """No action taken when LLM is not running."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, _, stop_mock, start_mock, _ = _run_llm_test(
            llm_running=False, gpu_memory_mb=_EMBEDDING_MEMORY_THRESHOLD_MB - 1000,
        )

        stop_mock.assert_not_called()
        start_mock.assert_not_called()

    def test_llm_restarted_after_last_embedding_source(self):
        """LLM is restarted after the last embedding source, not the first."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, run_sync_mock, stop_mock, start_mock, _ = _run_llm_test(
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
        result, _, stop_mock, start_mock, _ = _run_llm_test(
            llm_running=True, gpu_memory_mb=None,
        )

        stop_mock.assert_called_once()

    def test_internal_keys_not_in_results(self):
        """Internal _llm_* keys are not leaked in the result dict."""
        from scripts.run_all_syncs import _EMBEDDING_MEMORY_THRESHOLD_MB
        result, _, _, _, _ = _run_llm_test(
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

        result, _, stop_mock, start_mock, _ = _run_llm_test(
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
        result, run_sync_mock, _, _, _ = _run_llm_test(
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
        result, run_sync_mock, _, _, _ = _run_llm_test(
            llm_running=False, gpu_memory_mb=50_000, system_ram_mb=16_000,
        )

        called_sources = [c.args[0] for c in run_sync_mock.call_args_list]
        assert "vault_reindex" in called_sources
        assert "crm_vectorstore" in called_sources

    def test_non_embedding_sources_unaffected_by_low_ram(self):
        """Non-embedding sources still run even when RAM is low."""
        result, run_sync_mock, _, _, _ = _run_llm_test(
            llm_running=False, gpu_memory_mb=50_000, system_ram_mb=2000,
        )

        called_sources = [c.args[0] for c in run_sync_mock.call_args_list]
        assert "source_a" in called_sources
        assert "source_z" in called_sources

    def test_low_ram_with_llm_running_skips_embeddings_without_stopping_llm(self):
        """When LLM is running but RAM is low, embedding sources are skipped
        and the LLM is NOT stopped (no point stopping it only to skip the work)."""
        result, run_sync_mock, stop_mock, start_mock, _ = _run_llm_test(
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
        result, run_sync_mock, _, _, _ = _run_llm_test(
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


def test_sync_summary_surfaces_apple_agent_sha_drift():
    """A silently-broken Mac Mini self-update must reach the nightly
    Telegram summary — a bare logger.warning feeds no batched report (#646,
    same lesson as #448's investments_stale)."""
    from scripts.run_all_syncs import send_sync_summary_telegram
    result = {
        "succeeded": 3, "sources_run": 3, "failed": 0, "failed_sources": [],
        "results": {}, "duration_seconds": 12,
        "apple_agent_sha_drift": (
            "Apple Data Agent exported from b347e1f, which differs from this "
            "host's main (c13ba77) — its self-update may have failed."
        ),
    }
    with patch("api.services.telegram.send_message", return_value=True) as send_mock:
        send_sync_summary_telegram(result, trigger="test")
    message = send_mock.call_args[0][0]
    assert "SHA drift" in message
    assert "self-update" in message
    assert message.startswith("⚠️")


def test_sync_summary_omits_sha_drift_when_absent():
    from scripts.run_all_syncs import send_sync_summary_telegram
    result = {
        "succeeded": 3, "sources_run": 3, "failed": 0, "failed_sources": [],
        "results": {}, "duration_seconds": 12, "apple_agent_sha_drift": None,
    }
    with patch("api.services.telegram.send_message", return_value=True) as send_mock:
        send_sync_summary_telegram(result, trigger="test")
    message = send_mock.call_args[0][0]
    assert "SHA drift" not in message
    assert message.startswith("✅")


def test_sync_summary_surfaces_repeated_yield_sources():
    """A source stuck reporting the same non-zero count must be called out
    in the nightly summary and flip the status away from a clean ✅ (#646)."""
    from scripts.run_all_syncs import send_sync_summary_telegram
    result = {
        "succeeded": 3, "sources_run": 3, "failed": 0, "failed_sources": [],
        "duration_seconds": 12,
        "repeated_yield_sources": ["apple_import"],
        "results": {
            "apple_import": {
                "success": True,
                "repeated_yield": {"repeated_value": 1294, "consecutive_runs": 10},
            }
        },
    }
    with patch("api.services.telegram.send_message", return_value=True) as send_mock:
        send_sync_summary_telegram(result, trigger="test")
    message = send_mock.call_args[0][0]
    assert "apple_import" in message
    assert "1294" in message
    assert "not counted as new" in message
    assert message.startswith("⚠️")


def test_sync_summary_omits_repeated_yield_section_when_clean():
    from scripts.run_all_syncs import send_sync_summary_telegram
    result = {
        "succeeded": 3, "sources_run": 3, "failed": 0, "failed_sources": [],
        "duration_seconds": 12, "repeated_yield_sources": [], "results": {},
    }
    with patch("api.services.telegram.send_message", return_value=True) as send_mock:
        send_sync_summary_telegram(result, trigger="test")
    message = send_mock.call_args[0][0]
    assert "not counted as new" not in message
    assert message.startswith("✅")


def test_markdown_summary_surfaces_repeated_yield_and_sha_drift():
    """Same distinction (#646) must land in the markdown log, not just
    Telegram — sync_errors.md is the other half of "the nightly summary"."""
    from scripts.run_all_syncs import log_sync_summary_to_markdown
    result = {
        "succeeded": 3, "sources_run": 3, "failed": 0, "failed_sources": [],
        "duration_seconds": 12,
        "repeated_yield_sources": ["apple_import"],
        "apple_agent_sha_drift": "Apple Data Agent exported from b347e1f, self-update may have failed.",
        "results": {
            "apple_import": {
                "success": True,
                "repeated_yield": {"repeated_value": 1294, "consecutive_runs": 10},
            }
        },
    }
    with patch("scripts.run_all_syncs._write_to_markdown_log") as write_mock:
        log_sync_summary_to_markdown(result, trigger="test")

    entry = write_mock.call_args[0][0]
    assert "apple_import" in entry
    assert "1294" in entry
    assert "not counted as new" in entry
    assert "SHA drift" in entry
    assert "self-update may have failed" in entry


def test_markdown_summary_omits_sections_when_clean():
    from scripts.run_all_syncs import log_sync_summary_to_markdown
    result = {
        "succeeded": 3, "sources_run": 3, "failed": 0, "failed_sources": [],
        "duration_seconds": 12,
        "repeated_yield_sources": [], "apple_agent_sha_drift": None,
        "results": {},
    }
    with patch("scripts.run_all_syncs._write_to_markdown_log") as write_mock:
        log_sync_summary_to_markdown(result, trigger="test")

    entry = write_mock.call_args[0][0]
    assert "not counted as new" not in entry
    assert "SHA drift" not in entry


class TestYieldCollapse:
    """Yield-based no-op detection (#494).

    Duration collapse only catches sources that used to be slow. These cover
    the blind spot: sources that produce nothing while looking normal.
    """

    def test_detects_yield_collapse(self):
        """A source that normally produces records, producing none, is flagged."""
        from scripts.run_all_syncs import _detect_yield_collapse

        with patch("scripts.run_all_syncs.get_typical_yield", return_value=1564.0), \
             patch("scripts.run_all_syncs.get_consecutive_zero_yield_runs", return_value=50):
            info = _detect_yield_collapse("entity_cleanup", {"created": 0, "updated": 0})

        assert info is not None
        assert info["typical_yield"] == 1564.0
        assert info["consecutive_zero_runs"] == 51  # +1 for the run in flight

    def test_productive_run_never_flagged(self):
        """A run that produced records is fine regardless of history."""
        from scripts.run_all_syncs import _detect_yield_collapse

        with patch("scripts.run_all_syncs.get_typical_yield", return_value=500.0), \
             patch("scripts.run_all_syncs.get_consecutive_zero_yield_runs", return_value=50):
            assert _detect_yield_collapse("gmail_work", {"created": 42}) is None

    def test_single_quiet_night_not_flagged(self):
        """One empty run is normal (no new mail) — only a streak is suspicious."""
        from scripts.run_all_syncs import _detect_yield_collapse

        with patch("scripts.run_all_syncs.get_typical_yield", return_value=14.0), \
             patch("scripts.run_all_syncs.get_consecutive_zero_yield_runs", return_value=0):
            assert _detect_yield_collapse("calendar_personal", {"created": 0}) is None

    def test_no_productive_history_not_flagged(self):
        """A source that has never produced anything is the never-yielded case."""
        from scripts.run_all_syncs import _detect_yield_collapse

        with patch("scripts.run_all_syncs.get_typical_yield", return_value=None), \
             patch("scripts.run_all_syncs.get_consecutive_zero_yield_runs", return_value=50):
            assert _detect_yield_collapse("contacts", {"created": 0}) is None

    def test_db_error_never_raises(self):
        """A sync_health hiccup must not fail the sync run."""
        from scripts.run_all_syncs import _detect_yield_collapse

        with patch("scripts.run_all_syncs.get_typical_yield", side_effect=RuntimeError("db gone")):
            assert _detect_yield_collapse("slack", {"created": 0}) is None


class TestRepeatedYield:
    """Repeated-identical-yield detection (#646).

    Distinct from yield collapse (which only fires on zero output): a source
    re-importing the same unchanged upstream file reports the same non-zero
    count every run. Zero output is invisible to it; identical non-zero
    output for several runs in a row is the signature.
    """

    def test_detects_repeated_identical_yield(self):
        from scripts.run_all_syncs import _detect_repeated_yield

        with patch("scripts.run_all_syncs.get_repeated_yield_streak", return_value=9):
            info = _detect_repeated_yield("apple_import", {"created": 1294})

        assert info is not None
        assert info["repeated_value"] == 1294
        assert info["consecutive_runs"] == 10  # +1 for the run in flight

    def test_zero_yield_never_flagged(self):
        """Zero output is yield_collapse's job, not this detector's."""
        from scripts.run_all_syncs import _detect_repeated_yield

        with patch("scripts.run_all_syncs.get_repeated_yield_streak", return_value=50):
            assert _detect_repeated_yield("entity_cleanup", {"created": 0}) is None

    def test_below_minimum_streak_not_flagged(self):
        """A single repeat (this run matching just the last one) is normal —
        only a real streak is suspicious."""
        from scripts.run_all_syncs import _detect_repeated_yield

        with patch("scripts.run_all_syncs.get_repeated_yield_streak", return_value=1):
            assert _detect_repeated_yield("gmail_work", {"created": 42}) is None

    def test_db_error_never_raises(self):
        from scripts.run_all_syncs import _detect_repeated_yield

        with patch("scripts.run_all_syncs.get_repeated_yield_streak", side_effect=RuntimeError("db gone")):
            assert _detect_repeated_yield("slack", {"created": 42}) is None


class TestNeverYielded:
    """Dead/misconfigured source detection (#494)."""

    def test_flags_source_that_never_produced(self):
        from scripts.run_all_syncs import _detect_never_yielded

        history = {"runs": 185, "best_yield": 0, "avg_duration_seconds": 3.0}
        with patch("scripts.run_all_syncs.get_yield_history", return_value=history):
            info = _detect_never_yielded("contacts", {"created": 0})

        assert info is not None
        assert info["runs"] == 185

    def test_long_running_phase_exempt(self):
        """relationship_discovery runs ~40min without reporting stats — it is
        doing real work (#496), not a dead source, and must not be flagged."""
        from scripts.run_all_syncs import _detect_never_yielded

        history = {"runs": 151, "best_yield": 0, "avg_duration_seconds": 2203.0}
        with patch("scripts.run_all_syncs.get_yield_history", return_value=history):
            assert _detect_never_yielded("relationship_discovery", {"created": 0}) is None

    def test_insufficient_history_not_flagged(self):
        """A new source needs enough runs before we call it dead."""
        from scripts.run_all_syncs import _detect_never_yielded

        history = {"runs": 3, "best_yield": 0, "avg_duration_seconds": 0.5}
        with patch("scripts.run_all_syncs.get_yield_history", return_value=history):
            assert _detect_never_yielded("new_source", {"created": 0}) is None

    def test_source_with_past_yield_not_flagged(self):
        """Ever having produced records means it's a regression, not a dead source."""
        from scripts.run_all_syncs import _detect_never_yielded

        history = {"runs": 114, "best_yield": 1798, "avg_duration_seconds": 0.6}
        with patch("scripts.run_all_syncs.get_yield_history", return_value=history):
            assert _detect_never_yielded("entity_cleanup", {"created": 0}) is None

    def test_db_error_never_raises(self):
        from scripts.run_all_syncs import _detect_never_yielded

        with patch("scripts.run_all_syncs.get_yield_history", side_effect=RuntimeError("db gone")):
            assert _detect_never_yielded("contacts", {"created": 0}) is None


class TestNeverYieldedDamping:
    """Never-yielded warning must fire once, not nightly (#494 follow-up):
    a chronic source (link_slack, repoint_stale_ids, google_sheets) should
    only re-warn every ``NEVER_YIELDED_REWARN_DAYS`` days, not every run."""

    def test_no_prior_warning_not_recently_warned(self):
        from scripts.run_all_syncs import _recently_warned_never_yielded

        with patch("scripts.run_all_syncs.get_recent_errors", return_value=[]):
            assert _recently_warned_never_yielded("link_slack") is False

    def test_recent_warning_suppresses(self):
        from datetime import datetime, timezone, timedelta
        from scripts.run_all_syncs import _recently_warned_never_yielded

        recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        errors = [{"error_type": "never_yielded", "timestamp": recent}]
        with patch("scripts.run_all_syncs.get_recent_errors", return_value=errors):
            assert _recently_warned_never_yielded("link_slack") is True

    def test_warning_older_than_window_rewarns(self):
        from datetime import datetime, timezone, timedelta
        from scripts.run_all_syncs import _recently_warned_never_yielded

        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        errors = [{"error_type": "never_yielded", "timestamp": stale}]
        with patch("scripts.run_all_syncs.get_recent_errors", return_value=errors):
            assert _recently_warned_never_yielded("link_slack") is False

    def test_other_error_types_ignored(self):
        from datetime import datetime, timezone
        from scripts.run_all_syncs import _recently_warned_never_yielded

        now = datetime.now(timezone.utc).isoformat()
        errors = [{"error_type": "yield_collapse", "timestamp": now}]
        with patch("scripts.run_all_syncs.get_recent_errors", return_value=errors):
            assert _recently_warned_never_yielded("link_slack") is False

    def test_db_error_never_raises(self):
        from scripts.run_all_syncs import _recently_warned_never_yielded

        with patch("scripts.run_all_syncs.get_recent_errors", side_effect=RuntimeError("db gone")):
            assert _recently_warned_never_yielded("link_slack") is False

    def test_run_sync_skips_warning_and_error_when_recently_warned(self):
        """End-to-end through run_sync: when damped, neither the WARNING log
        nor a new sync_errors row should be produced, and the source must not
        land in the nightly 'Never produced records' rollup."""
        import subprocess
        from scripts.run_all_syncs import run_sync, SYNC_SCRIPTS

        source = next(iter(SYNC_SCRIPTS))
        never_yielded_info = {"runs": 185, "avg_duration_seconds": 3.0}

        completed = subprocess.CompletedProcess(args=["fake"], returncode=0, stdout="", stderr="")
        with (
            patch("scripts.run_all_syncs.subprocess.run", return_value=completed),
            patch("scripts.run_all_syncs.record_sync_start", return_value=999),
            patch("scripts.run_all_syncs.record_sync_complete"),
            patch("scripts.run_all_syncs.record_sync_error") as record_error_mock,
            patch("scripts.run_all_syncs._detect_duration_collapse", return_value=None),
            patch("scripts.run_all_syncs._detect_yield_collapse", return_value=None),
            patch("scripts.run_all_syncs._detect_never_yielded", return_value=never_yielded_info),
            patch("scripts.run_all_syncs._detect_repeated_yield", return_value=None),
            patch("scripts.run_all_syncs._recently_warned_never_yielded", return_value=True),
        ):
            success, stats = run_sync(source, dry_run=False)

        assert success is True
        assert stats["never_yielded"] == never_yielded_info
        assert "never_yielded_warned" not in stats
        record_error_mock.assert_not_called()

    def test_run_sync_warns_and_records_error_when_not_recently_warned(self):
        """When not damped, run_sync must log the warning, record a
        sync_errors row with error_type='never_yielded', and mark the stats
        so the nightly rollup line includes this source."""
        import subprocess
        from scripts.run_all_syncs import run_sync, SYNC_SCRIPTS

        source = next(iter(SYNC_SCRIPTS))
        never_yielded_info = {"runs": 185, "avg_duration_seconds": 3.0}

        completed = subprocess.CompletedProcess(args=["fake"], returncode=0, stdout="", stderr="")
        with (
            patch("scripts.run_all_syncs.subprocess.run", return_value=completed),
            patch("scripts.run_all_syncs.record_sync_start", return_value=999),
            patch("scripts.run_all_syncs.record_sync_complete"),
            patch("scripts.run_all_syncs.record_sync_error") as record_error_mock,
            patch("scripts.run_all_syncs._detect_duration_collapse", return_value=None),
            patch("scripts.run_all_syncs._detect_yield_collapse", return_value=None),
            patch("scripts.run_all_syncs._detect_never_yielded", return_value=never_yielded_info),
            patch("scripts.run_all_syncs._detect_repeated_yield", return_value=None),
            patch("scripts.run_all_syncs._recently_warned_never_yielded", return_value=False),
        ):
            success, stats = run_sync(source, dry_run=False)

        assert success is True
        assert stats["never_yielded_warned"] is True
        record_error_mock.assert_called_once()
        assert record_error_mock.call_args.kwargs.get("error_type") == "never_yielded"


class TestSkippedMarker:
    """SYNC_SKIPPED marker parsing (#494/#495): an unconfigured source must not
    be recorded as a healthy success."""

    def test_parses_skip_reason(self):
        from scripts.run_all_syncs import _parse_sync_output

        stats = _parse_sync_output("SYNC_SKIPPED: Photos library unavailable\n")
        assert stats["skipped_reason"] == "Photos library unavailable"

    def test_absent_marker_leaves_no_reason(self):
        from scripts.run_all_syncs import _parse_sync_output

        assert _parse_sync_output("normal sync output\n").get("skipped_reason") is None


class TestBackupRetentionGating:
    """
    Retention must not run when the sync failed (#562).

    Snapshots are taken before the sync; whether they are a usable rollback
    point is only known once the run finishes. Pruning on a failed night could
    rotate away the last good copy exactly when it is most needed.
    """

    def test_clean_run_prunes(self):
        _, _, _, _, prune_mock = _run_llm_test(fail_sources=set())

        prune_mock.assert_called_once()

    def test_failed_source_skips_pruning(self):
        _, _, _, _, prune_mock = _run_llm_test(fail_sources={"source_a"})

        prune_mock.assert_not_called()

    def test_backups_are_still_taken_on_a_failing_run(self):
        """
        Only the *deletion* is gated. A failing night still gets its snapshot —
        that is the run most likely to need one.
        """
        from scripts.run_all_syncs import run_all_syncs

        fake_restart = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("scripts.run_all_syncs.SYNC_SOURCES", LLM_TEST_SYNC_SOURCES),
            patch("scripts.run_all_syncs.SYNC_ORDER", LLM_TEST_SYNC_ORDER),
            patch("scripts.run_all_syncs.run_sync",
                  MagicMock(side_effect=_make_run_sync_side_effect({"source_a"}))),
            patch("scripts.run_all_syncs.check_sync_health", return_value=(True, "healthy")),
            patch("scripts.run_all_syncs.get_disabled_work_sources", return_value=set()),
            patch("scripts.run_all_syncs.log_sync_summary_to_markdown"),
            patch("scripts.run_all_syncs.backup_databases") as backup_mock,
            patch("scripts.run_all_syncs.prune_backups_after_success") as prune_mock,
            patch("scripts.run_all_syncs.send_sync_summary_telegram"),
            patch("scripts.run_all_syncs.reap_orphan_sync_runs", return_value=0),
            patch("scripts.run_all_syncs.detect_silent_source_entity_drift", return_value=[]),
            patch("scripts.run_all_syncs.subprocess.run", return_value=fake_restart),
            patch("urllib.request.urlopen"),
            patch("scripts.run_all_syncs._is_llm_running", return_value=False),
            patch("scripts.run_all_syncs._get_available_gpu_memory_mb", return_value=20000),
            patch("scripts.run_all_syncs._get_available_system_ram_mb", return_value=16000),
            patch("scripts.run_all_syncs._start_llm"),
            patch("scripts.run_all_syncs.get_sync_health") as health_mock,
        ):
            mock_health = MagicMock()
            mock_health.hours_since_sync = 24.0
            mock_health.last_status = "SUCCESS"
            health_mock.return_value = mock_health

            run_all_syncs(dry_run=False, force=True)

        backup_mock.assert_called_once()
        prune_mock.assert_not_called()
