"""
LifeOS API Routes Package.

This package contains all FastAPI route handlers organized by domain.
Use this module to import routers for registration with the FastAPI app.

Example:
    from api.routes import crm_router, chat_router

    app.include_router(crm_router)
    app.include_router(chat_router)
"""

# ============================================================================
# Core API Routers
# ============================================================================

from api.routes.chat import router as chat_router
from api.routes.crm import router as crm_router
from api.routes.ask import router as ask_router
from api.routes.search import router as search_router

# ============================================================================
# Admin & System Routers
# ============================================================================

from api.routes.admin import router as admin_router
from api.routes.memories import router as memories_router
from api.routes.conversations import router as conversations_router

# ============================================================================
# Integration Routers
# ============================================================================

from api.routes.gmail import router as gmail_router
from api.routes.calendar import router as calendar_router
from api.routes.drive import router as drive_router
from api.routes.slack import router as slack_router
from api.routes.imessage import router as imessage_router

# ============================================================================
# People & Briefings Routers
# ============================================================================

from api.routes.people import router as people_router
from api.routes.briefings import router as briefings_router


__all__ = [
    # Core
    "chat_router",
    "crm_router",
    "ask_router",
    "search_router",
    # Admin
    "admin_router",
    "memories_router",
    "conversations_router",
    # Integrations
    "gmail_router",
    "calendar_router",
    "drive_router",
    "slack_router",
    "imessage_router",
    # People
    "people_router",
    "briefings_router",
]
