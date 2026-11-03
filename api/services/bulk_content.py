"""Which indexed notes are bulk content, decided at query time from the path alone."""
from __future__ import annotations

from typing import Callable, Optional

from api.services.vault_listing import no_people_reason, vault_relative_path
from config.settings import settings


def bulk_path_matcher() -> Optional[Callable[[str], bool]]:
    """
    A test for whether a chunk id or note path sits under a configured bulk folder.

    Returns:
        The test, or None when ``LIFEOS_VAULT_NO_PEOPLE_PATHS`` names no folder
    """
    folders = settings.vault_no_people_paths
    if not folders:
        return None
    root = settings.vault_path.resolve()
    return lambda path: no_people_reason(vault_relative_path(path, root), {}, folders) == "folder"
