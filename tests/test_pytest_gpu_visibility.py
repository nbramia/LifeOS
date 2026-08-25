"""
Tests for the CPU-only-embeddings-under-pytest guard (#521).

This host's iGPU has only 8 SDMA queues; `pytest -n auto` spawns one worker
per core, and each worker independently loading a GPU embedding model would
exhaust those queues. `tests/conftest.py`'s `pytest_configure` hook hides the
GPU from every test process by setting HIP_VISIBLE_DEVICES /
ROCR_VISIBLE_DEVICES / CUDA_VISIBLE_DEVICES to empty. These tests confirm
that guard is actually wired up and behaves correctly (setdefault, not
overwrite).
"""
import os

import pytest

pytestmark = pytest.mark.unit

_GPU_VISIBILITY_VARS = ("HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES")


def test_pytest_session_has_gpu_hidden():
    """By the time any test runs, pytest_configure has already fired (it runs
    before test collection), so these vars should already be set to empty —
    proving the hook actually ran for this session, not just that the
    function exists.
    """
    for var in _GPU_VISIBILITY_VARS:
        assert os.environ.get(var) == "", (
            f"{var} was not hidden by tests/conftest.py's pytest_configure hook"
        )


def test_force_cpu_embeddings_sets_unset_vars(monkeypatch):
    """The helper sets each visibility var to empty when it isn't already set."""
    from tests.conftest import _force_cpu_embeddings_for_tests

    for var in _GPU_VISIBILITY_VARS:
        monkeypatch.delenv(var, raising=False)

    _force_cpu_embeddings_for_tests()

    for var in _GPU_VISIBILITY_VARS:
        assert os.environ[var] == ""


def test_force_cpu_embeddings_does_not_override_explicit_value(monkeypatch):
    """An operator/CI override (e.g. a real single-GPU test rig that wants
    HIP_VISIBLE_DEVICES=0) must survive — the helper uses setdefault, not
    unconditional assignment.
    """
    from tests.conftest import _force_cpu_embeddings_for_tests

    monkeypatch.setenv("HIP_VISIBLE_DEVICES", "0")

    _force_cpu_embeddings_for_tests()

    assert os.environ["HIP_VISIBLE_DEVICES"] == "0"
