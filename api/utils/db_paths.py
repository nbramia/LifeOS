"""
Database path utilities for LifeOS API services.
"""
from pathlib import Path

from config.settings import settings


def get_crm_db_path() -> str:
    """
    Get the path to the CRM database.

    Creates the parent directory if it doesn't exist.

    Returns:
        Absolute path to the crm.db file
    """
    db_dir = Path(settings.chroma_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "crm.db")
