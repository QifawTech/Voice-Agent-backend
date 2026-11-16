"""
Voice Agent Platform — Analytics Routes

API endpoints for dashboard analytics:
    GET /api/v1/analytics/overview          — Dashboard summary stats
    GET /api/v1/analytics/conversations     — Paginated conversation logs
    GET /api/v1/analytics/orders            — Paginated order events
    GET /api/v1/analytics/daily             — Daily analytics breakdown

All endpoints require JWT authentication (admin dashboard).
Tenant admins see only their own data; super admins can see any tenant.
"""

from datetime import date, datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logger import get_logger
from app.middleware.auth import get_current_user
from app.models.admin_user import AdminUser
from app.models.conversation_log import ConversationLog
from app.models.daily_analytics import DailyAnalytics
from app.models.order_event import OrderEvent
from app.schemas.analytics import (
    AnalyticsOverview,
    ConversationLogListResponse,
    ConversationLogResponse,
    DailyAnalyticsListResponse,
    DailyAnalyticsResponse,
    OrderEventListResponse,
    OrderEventResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────


def _resolve_tenant_id(user: AdminUser, tenant_id: UUID | None) -> UUID:
    """
    Resolve the effective tenant ID based on user role.

    - Super admins: can query any tenant (tenant_id param required).
    - Tenant admins: always scoped to their own tenant.
    """
    if user.role == "super_admin":
        if tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Super admins must specify tenant_id",
            )
        return tenant_id

    # Tenant admin — use their own tenant_id
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with any tenant",
        )
    return user.tenant_id


# Order item payloads are stored verbatim from each restaurant's own ordering
# API, so the field names differ between integrations. Read the spellings we
# have seen rather than dropping a line because one API says "item_name".
_ITEM_NAME_KEYS = ("name", "item_name", "title", "product_name")
_ITEM_QTY_KEYS = ("quantity", "qty", "count")


def _item_name_and_qty(item: object) -> tuple[str, int] | None:
    """Pull (name, quantity) out of one order-item payload, or None if absent."""
    if not isinstance(item, dict):
        return None

    name = ""
    for key in _ITEM_NAME_KEYS:
        value = item.get(key)
        if value:
            name = str(value).strip()
            break
    if not name:
        return None

    quantity = 1
    for key in _ITEM_QTY_KEYS:
        if item.get(key) is not None:
            try:
                quantity = max(1, int(float(item[key])))
            except (TypeError, ValueError):
                quantity = 1
            break

    return name, quantity


def _as_date(value: object) -> date | None:
    """Normalise whatever the driver returns for a grouped date expression."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


# ──────────────────────────────────────────────────────────────
# Overview — Dashboard Summary
# ──────────────────────────────────────────────────────────────


@router.get(
    "/overview",
    response_model=AnalyticsOverview,
    summary="Dashboard Overview",
    description="Get aggregated analytics summary for the dashboard.",
)
async def get_overview(
    tenant_id: UUID | None = Query(default=None, description="Tenant ID (required for super admins)"),
    restaurant_id: str | None = Query(default=None, description="Filter by restaurant ID"),
    days: int = Query(default=30, ge=1, le=365, description="Number of days to aggregate"),
    user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get a high-level analytics overview for the dashboard.

    Returns:
    - Total conversations, orders, revenue
    - Conversion rate (orders / conversations)
    - Average session duration
    - Top ordered items
    - Daily breakdown for charting
    """
    effective_tenant_id = _resolve_tenant_id(user, tenant_id)
    period_end = date.today()
    period_start = period_end - timedelta(days=days)

    # ── Base filter conditions ────────────────────────────────
    conversation_filters = [
        ConversationLog.tenant_id == effective_tenant_id,
        ConversationLog.started_at >= datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc),
    ]
    order_filters = [
        OrderEvent.tenant_id == effective_tenant_id,
        OrderEvent.created_at >= datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc),
    ]

    if restaurant_id:
        conversation_filters.append(ConversationLog.restaurant_id == restaurant_id)
        order_filters.append(OrderEvent.restaurant_id == restaurant_id)

    # ── Total conversations ───────────────────────────────────
    conv_count_result = await db.execute(
        select(func.count(ConversationLog.id)).where(and_(*conversation_filters))
    )
    total_conversations = conv_count_result.scalar() or 0

    # ── Total orders (only 'created' events) ──────────────────
    order_count_result = await db.execute(
        select(func.count(OrderEvent.id)).where(
            and_(*order_filters, OrderEvent.event_type == "created")
        )
    )
    total_orders = order_count_result.scalar() or 0

    # ── Total revenue ─────────────────────────────────────────
    revenue_result = await db.execute(
        select(func.coalesce(func.sum(OrderEvent.order_value), 0)).where(
            and_(*order_filters, OrderEvent.event_type == "created")
        )
    )
    total_revenue = float(revenue_result.scalar() or 0)

    # ── Conversion rate ───────────────────────────────────────
    conversion_rate = 0.0
    if total_conversations > 0:
        conversion_rate = round((total_orders / total_conversations) * 100, 2)

    # ── Average session duration ──────────────────────────────
    avg_duration_result = await db.execute(
        select(func.coalesce(func.avg(ConversationLog.duration_seconds), 0)).where(
            and_(*conversation_filters)
        )
    )
    avg_session_duration = float(avg_duration_result.scalar() or 0)

    # ── Daily breakdown, computed from the source rows ────────
    # This used to read the daily_analytics table, but nothing in the service
    # ever writes a row to it, so every dashboard chart came back empty no
    # matter how many calls had been handled. Aggregate the same rows the
    # totals above are counted from instead.
    conv_day = func.date(ConversationLog.started_at).label("day")
    conv_daily_result = await db.execute(
        select(
            conv_day,
            func.count(ConversationLog.id),
            func.coalesce(func.avg(ConversationLog.duration_seconds), 0),
        )
        .where(and_(*conversation_filters))
        .group_by(conv_day)
    )
    conversations_by_day: dict[date, tuple[int, float]] = {}
    for day, count, avg_duration in conv_daily_result.all():
        key = _as_date(day)
        if key is not None:
            conversations_by_day[key] = (int(count or 0), float(avg_duration or 0))

    order_day = func.date(OrderEvent.created_at).label("day")
    order_daily_result = await db.execute(
        select(
            order_day,
            func.count(OrderEvent.id),
            func.coalesce(func.sum(OrderEvent.order_value), 0),
        )
        .where(and_(*order_filters, OrderEvent.event_type == "created"))
        .group_by(order_day)
    )
    orders_by_day: dict[date, tuple[int, float]] = {}
    for day, count, revenue in order_daily_result.all():
        key = _as_date(day)
        if key is not None:
            orders_by_day[key] = (int(count or 0), float(revenue or 0))

    # One entry per calendar day, zeros included, so the chart draws a
    # continuous series rather than bunching the active days together.
    daily_breakdown: list[DailyAnalyticsResponse] = []
    cursor = period_start
    while cursor <= period_end:
        conversations, avg_duration = conversations_by_day.get(cursor, (0, 0.0))
        orders, revenue = orders_by_day.get(cursor, (0, 0.0))
        daily_breakdown.append(
            DailyAnalyticsResponse(
                # Synthetic but stable: these rows are computed, not stored,
                # and the id only has to be consistent between requests.
                id=uuid5(
                    NAMESPACE_URL,
                    f"daily:{effective_tenant_id}:{restaurant_id or ''}:{cursor.isoformat()}",
                ),
                tenant_id=effective_tenant_id,
                restaurant_id=restaurant_id,
                date=cursor,
                total_conversations=conversations,
                total_orders=orders,
                total_revenue=round(revenue, 2),
                conversion_rate=(
                    round((orders / conversations) * 100, 2) if conversations else 0.0
                ),
                avg_session_duration=round(avg_duration, 2),
            )
        )
        cursor += timedelta(days=1)

    # ── Top items, counted from the order payloads ────────────
    # Parsed in Python rather than SQL because the JSON shape comes from the
    # restaurant's API and is not ours to rely on. Bounded by the window.
    items_result = await db.execute(
        select(OrderEvent.items).where(
            and_(
                *order_filters,
                OrderEvent.event_type == "created",
                OrderEvent.items.isnot(None),
            )
        )
    )
    item_counts: dict[str, int] = {}
    for (payload,) in items_result.all():
        if not isinstance(payload, list):
            continue
        for item in payload:
            parsed = _item_name_and_qty(item)
            if parsed is None:
                continue
            name, quantity = parsed
            item_counts[name] = item_counts.get(name, 0) + quantity

    top_items = sorted(
        ({"name": name, "count": count} for name, count in item_counts.items()),
        key=lambda entry: (-entry["count"], entry["name"]),
    )[:10]

    logger.info(
        "analytics_overview_fetched",
        tenant_id=str(effective_tenant_id),
        restaurant_id=restaurant_id,
        days=days,
        total_conversations=total_conversations,
        total_orders=total_orders,
    )

    return AnalyticsOverview(
        tenant_id=effective_tenant_id,
        restaurant_id=restaurant_id,
        period_start=period_start,
        period_end=period_end,
        total_conversations=total_conversations,
        total_orders=total_orders,
        total_revenue=total_revenue,
        conversion_rate=conversion_rate,
        avg_session_duration=avg_session_duration,
        top_items=top_items,
        daily_breakdown=daily_breakdown,
    )


# ──────────────────────────────────────────────────────────────
# Conversations — Paginated Logs
# ──────────────────────────────────────────────────────────────


@router.get(
    "/conversations",
    response_model=ConversationLogListResponse,
    summary="List Conversations",
    description="Get paginated conversation logs with optional filters.",
)
async def list_conversations(
    tenant_id: UUID | None = Query(default=None, description="Tenant ID (required for super admins)"),
    restaurant_id: str | None = Query(default=None, description="Filter by restaurant ID"),
    outcome: str | None = Query(default=None, description="Filter by outcome: ordered, browsed, cancelled"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List conversation logs with pagination and filters.

    Supports filtering by restaurant_id and outcome (ordered/browsed/cancelled).
    Results are ordered by started_at descending (most recent first).
    """
    effective_tenant_id = _resolve_tenant_id(user, tenant_id)

    # Build filter conditions
    filters = [ConversationLog.tenant_id == effective_tenant_id]
    if restaurant_id:
        filters.append(ConversationLog.restaurant_id == restaurant_id)
    if outcome:
        filters.append(ConversationLog.outcome == outcome)

    # Get total count
    count_result = await db.execute(
        select(func.count(ConversationLog.id)).where(and_(*filters))
    )
    total = count_result.scalar() or 0

    # Get paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        select(ConversationLog)
        .where(and_(*filters))
        .order_by(desc(ConversationLog.started_at))
        .offset(offset)
        .limit(page_size)
    )
    rows = result.scalars().all()

    conversations = [ConversationLogResponse.model_validate(row) for row in rows]

    logger.info(
        "analytics_conversations_fetched",
        tenant_id=str(effective_tenant_id),
        total=total,
        page=page,
        page_size=page_size,
    )

    return ConversationLogListResponse(
        conversations=conversations,
        total=total,
        page=page,
        page_size=page_size,
    )


# ──────────────────────────────────────────────────────────────
# Orders — Paginated Events
# ──────────────────────────────────────────────────────────────


@router.get(
    "/orders",
    response_model=OrderEventListResponse,
    summary="List Order Events",
    description="Get paginated order events with optional filters.",
)
async def list_orders(
    tenant_id: UUID | None = Query(default=None, description="Tenant ID (required for super admins)"),
    restaurant_id: str | None = Query(default=None, description="Filter by restaurant ID"),
    event_type: str | None = Query(default=None, description="Filter by event type: created, checked, cancelled"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List order events with pagination and filters.

    Supports filtering by restaurant_id and event_type (created/checked/cancelled).
    Results are ordered by created_at descending (most recent first).
    """
    effective_tenant_id = _resolve_tenant_id(user, tenant_id)

    # Build filter conditions
    filters = [OrderEvent.tenant_id == effective_tenant_id]
    if restaurant_id:
        filters.append(OrderEvent.restaurant_id == restaurant_id)
    if event_type:
        filters.append(OrderEvent.event_type == event_type)

    # Get total count
    count_result = await db.execute(
        select(func.count(OrderEvent.id)).where(and_(*filters))
    )
    total = count_result.scalar() or 0

    # Get paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        select(OrderEvent)
        .where(and_(*filters))
        .order_by(desc(OrderEvent.created_at))
        .offset(offset)
        .limit(page_size)
    )
    rows = result.scalars().all()

    events = [OrderEventResponse.model_validate(row) for row in rows]

    logger.info(
        "analytics_orders_fetched",
        tenant_id=str(effective_tenant_id),
        total=total,
        page=page,
        page_size=page_size,
    )

    return OrderEventListResponse(
        events=events,
        total=total,
        page=page,
        page_size=page_size,
    )


# ──────────────────────────────────────────────────────────────
# Daily Analytics — Date Range Breakdown
# ──────────────────────────────────────────────────────────────


@router.get(
    "/daily",
    response_model=DailyAnalyticsListResponse,
    summary="Daily Analytics",
    description="Get daily analytics breakdown for a date range.",
)
async def get_daily_analytics(
    tenant_id: UUID | None = Query(default=None, description="Tenant ID (required for super admins)"),
    restaurant_id: str | None = Query(default=None, description="Filter by restaurant ID"),
    start_date: date | None = Query(default=None, description="Start date (default: 30 days ago)"),
    end_date: date | None = Query(default=None, description="End date (default: today)"),
    user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get daily analytics breakdown for charting.

    Returns pre-computed daily metrics from the daily_analytics table.
    Each row contains: conversations, orders, revenue, conversion rate,
    avg session duration, top items, and peak hours.
    """
    effective_tenant_id = _resolve_tenant_id(user, tenant_id)

    # Default date range: last 30 days
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=30)

    # Validate date range
    if start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date must be before or equal to end_date",
        )

    if (end_date - start_date).days > 365:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Date range cannot exceed 365 days",
        )

    # Build filter conditions
    filters = [
        DailyAnalytics.tenant_id == effective_tenant_id,
        DailyAnalytics.date >= start_date,
        DailyAnalytics.date <= end_date,
    ]
    if restaurant_id:
        filters.append(DailyAnalytics.restaurant_id == restaurant_id)

    # Get results
    result = await db.execute(
        select(DailyAnalytics)
        .where(and_(*filters))
        .order_by(DailyAnalytics.date.asc())
    )
    rows = result.scalars().all()

    analytics = [DailyAnalyticsResponse.model_validate(row) for row in rows]

    logger.info(
        "analytics_daily_fetched",
        tenant_id=str(effective_tenant_id),
        restaurant_id=restaurant_id,
        start_date=str(start_date),
        end_date=str(end_date),
        total=len(analytics),
    )

    return DailyAnalyticsListResponse(
        analytics=analytics,
        total=len(analytics),
    )
