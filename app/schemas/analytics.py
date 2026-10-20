"""
Voice Agent Platform — Analytics Schemas

Pydantic models for analytics API responses: conversation logs,
order events, daily stats, and dashboard overview.

All models include restaurant_id to support multi-restaurant tenants.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ---- Conversation Log ----


class ConversationLogResponse(BaseModel):
    """Response body for a single conversation log entry."""

    id: UUID
    tenant_id: UUID
    restaurant_id: str | None = None
    session_id: str
    messages: list | None = None
    outcome: str | None = None
    order_value: float | None = None
    message_count: int = 0
    duration_seconds: int = 0
    started_at: datetime
    ended_at: datetime | None = None

    model_config = {"from_attributes": True}


class ConversationLogListResponse(BaseModel):
    """Paginated list of conversation logs."""

    conversations: list[ConversationLogResponse]
    total: int
    page: int = 1
    page_size: int = 20


# ---- Order Event ----


class OrderEventResponse(BaseModel):
    """Response body for a single order event."""

    id: UUID
    tenant_id: UUID
    restaurant_id: str | None = None
    session_id: str
    external_order_id: str
    event_type: str
    order_value: float | None = None
    items: list | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class OrderEventListResponse(BaseModel):
    """Paginated list of order events."""

    events: list[OrderEventResponse]
    total: int
    page: int = 1
    page_size: int = 20


# ---- Daily Analytics ----


class DailyAnalyticsResponse(BaseModel):
    """Response body for a single day's analytics."""

    id: UUID
    tenant_id: UUID
    restaurant_id: str | None = None
    date: date
    total_conversations: int = 0
    total_orders: int = 0
    total_revenue: float = 0
    conversion_rate: float = 0
    avg_session_duration: float = 0
    top_items: list | None = None
    peak_hours: list | None = None

    model_config = {"from_attributes": True}


class DailyAnalyticsListResponse(BaseModel):
    """List of daily analytics entries (date range)."""

    analytics: list[DailyAnalyticsResponse]
    total: int


# ---- Dashboard Overview ----


class AnalyticsOverview(BaseModel):
    """Summary dashboard data for a tenant (optionally filtered by restaurant)."""

    tenant_id: UUID
    restaurant_id: str | None = None
    period_start: date
    period_end: date
    total_conversations: int = 0
    total_orders: int = 0
    total_revenue: float = 0
    conversion_rate: float = 0
    avg_session_duration: float = 0
    top_items: list = Field(default_factory=list)
    daily_breakdown: list[DailyAnalyticsResponse] = Field(default_factory=list)
