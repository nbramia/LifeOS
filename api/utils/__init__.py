# LifeOS API Utilities
"""
Shared utility functions for LifeOS API services.
"""

from api.utils.datetime_utils import make_aware
from api.utils.db_paths import get_crm_db_path

__all__ = ["make_aware", "get_crm_db_path"]
