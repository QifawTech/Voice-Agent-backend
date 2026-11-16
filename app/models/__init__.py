"""
Voice Agent Platform — Model Registry

Imports all SQLAlchemy models so they're registered with Base.metadata.
This ensures Alembic can detect all tables for auto-generation.
"""

from app.models.admin_user import AdminUser
from app.models.agent import Agent
from app.models.conversation_log import ConversationLog
from app.models.daily_analytics import DailyAnalytics
from app.models.delivery import Delivery
from app.models.occurrence import Occurrence
from app.models.order_event import OrderEvent
from app.models.person import Person
from app.models.reminder import Reminder
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
    # ---- Reminder engine ----
    "Agent",
    "Person",
    "Reminder",
    "Occurrence",
    "Delivery",
]
