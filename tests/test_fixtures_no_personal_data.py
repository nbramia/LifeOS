"""Guard against real personal data in committed test fixtures (#598).

`tests/fixtures/` is committed to a public, open-source repo. #598 found
that a real, machine-specific `.env` could leak into a test process (via
`api/main.py`'s old upward-searching `load_dotenv()` -- see
`tests/test_env_isolation.py`) and get baked into a golden fixture by
whoever captured it, without the capturing agent necessarily noticing.
Fixing the leak (this issue) removes the mechanism going forward, but
doesn't by itself prove a *future* fixture can't be captured carelessly
against a real `.env` on someone's dev machine and committed with real
values in it.

This test is that proof: on a machine where a real `.env` is actually
reachable (any dev machine, including the one this was written on), it
reads the real values of the identity-sensitive settings audited in #598
directly from that file -- without ever loading them into `os.environ` --
and fails if any committed fixture file quotes one of them verbatim. On a
fresh clone or CI, where no such `.env` exists, there is nothing to compare
against and the check is vacuous by design: a fresh clone has no personal
data to leak in the first place.
"""
from pathlib import Path

import pytest
from dotenv import dotenv_values

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Identity-sensitive settings keys found in #598's audit of module-level
# constants computed from `settings` at import time -- see config/settings.py
# for the corresponding fields.
#
# LIFEOS_PARTNER_NAME and LIFEOS_THERAPIST_PATTERNS (settings.personal_context())
# are deliberately NOT included here even though they're exactly the kind of
# personal data this issue cares about: they're plain human names, and a
# literal substring scan against them is unreliable -- while developing this
# check, the real LIFEOS_PARTNER_NAME value on this machine happened to
# collide with "Taylor", the generic example name agent_system_prompt.py's
# own hardcoded prompt text uses for read_vault_file's fuzzy-matching
# example ("... finds Taylor.md"), producing a false positive against
# tests/fixtures/agent_system_prompt_golden_591.py. Unlike the keys below,
# personal_context() is also read live at call time, not cached into a
# module-level constant, so it isn't part of the caching defect this issue
# is centrally about. Real name leakage there is still possible (a fixture
# could capture personal_context()'s output on a real machine) but needs a
# narrower, more targeted check than this one -- flagged here rather than
# silently dropped.
_SENSITIVE_KEYS = (
    "LIFEOS_USER_NAME",
    "LIFEOS_MY_PERSON_ID",
    "LIFEOS_WORK_DOMAIN",
    "LIFEOS_WORK_DOMAIN_2",
)


def _find_real_dotenv_upward(start: Path) -> Path | None:
    """Walk upward from `start` looking for a `.env` -- deliberately the
    same search #598 found unsafe for api/main.py to perform on real
    process environment, reproduced here only for read-only comparison
    (dotenv_values never touches os.environ). Returns None when nothing is
    found (fresh clone, CI): there is then nothing to check fixtures
    against, by design.
    """
    for candidate_dir in (start, *start.parents):
        candidate = candidate_dir / ".env"
        if candidate.is_file():
            return candidate
    return None


def _real_sensitive_values() -> dict:
    real_env = _find_real_dotenv_upward(_REPO_ROOT)
    if real_env is None:
        return {}
    values = dotenv_values(real_env)
    return {k: v for k, v in values.items() if k in _SENSITIVE_KEYS and v and v.strip()}


def test_no_fixture_contains_a_real_sensitive_value():
    real_values = _real_sensitive_values()
    if not real_values:
        pytest.skip(
            "No real .env reachable from this checkout -- nothing to check "
            "fixtures against (expected on a fresh clone or CI)."
        )

    offenders = []
    for path in _FIXTURES_DIR.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        for key, value in real_values.items():
            if value in text:
                # Deliberately omit the value itself from the failure
                # message -- naming the offending key and file is enough to
                # act on, and this message may end up in CI logs.
                offenders.append(f"{path.relative_to(_REPO_ROOT)} contains the real {key} value")

    assert not offenders, (
        "Real personal data found in committed test fixtures:\n" + "\n".join(offenders)
    )
