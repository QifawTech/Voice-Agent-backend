"""
Voice Agent Platform — Model Registry

Imports all SQLAlchemy models so they're registered with Base.metadata.
This ensures Alembic can detect all tables for auto-generation.
"""

from app.models.admin_user import AdminUser
from app.models.conversation_log import ConversationLog
from app.models.daily_analytics import DailyAnalytics
from app.models.order_event import OrderEvent
from app.models.sub_tenant import SubTenant
from app.models.tenant import Tenant
from app.models.tenant_tool import TenantTool

__all__ = [
    "Tenant",
    "SubTenant",
    "TenantTool",
    "AdminUser",
    "ConversationLog",
    "OrderEvent",
    "DailyAnalytics",
]
