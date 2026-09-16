"""Holds `docs/guides/configuration.md` to its own claim.

That guide calls itself the single authoritative reference for every
`LIFEOS_*` environment variable. Nothing checked it, so a setting could be
added to `config/settings.py` and reach an operator only as a name in source.
The cost lands on whoever has to discover a lever exists before they can pull
it — an install whose CLI binary sits outside the service's PATH, for example,
has no documented way to point at it.

The check runs one way: every `LIFEOS_*` alias declared in `config/settings.py`
must appear in the guide. It deliberately does not require the reverse. Several
documented variables are read straight from the environment by scripts rather
than declared as a settings field (`LIFEOS_API_URL`, `LIFEOS_AUTODEPLOY_ENABLED`,
`LIFEOS_EMBEDDING_MEMORY_THRESHOLD_MB`, `LIFEOS_VAULT_MTIME_TRUSTED_AFTER`), and
those rows earn their place.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent
SETTINGS = REPO / "config" / "settings.py"
GUIDE = REPO / "docs" / "guides" / "configuration.md"

_ALIAS = re.compile(r"""alias\s*=\s*["'](LIFEOS_[A-Z0-9_]+)["']""")


def _declared_settings() -> set[str]:
    return set(_ALIAS.findall(SETTINGS.read_text(encoding="utf-8")))


def test_every_setting_appears_in_the_configuration_guide():
    declared = _declared_settings()
    assert declared, "no LIFEOS_* aliases parsed from config/settings.py"

    guide = GUIDE.read_text(encoding="utf-8")
    undocumented = sorted(name for name in declared if name not in guide)

    assert not undocumented, (
        "config/settings.py declares settings that docs/guides/configuration.md "
        "does not mention, while that guide describes itself as the authoritative "
        "reference for every LIFEOS_* variable. Add a row for each, giving type, "
        "default, and what an operator changes it for:\n  "
        + "\n  ".join(undocumented)
    )
