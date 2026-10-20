"""
Voice Agent Platform — DailyAnalytics Model

Pre-computed daily metrics per tenant per restaurant for fast dashboard queries.
Aggregated from ConversationLog and OrderEvent data.
Each row = one restaurant's stats for one day.

For multi-restaurant tenants (like SOPZY), there's one row per restaurant per day.
For standalone tenants, restaurant_id may be null.
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DailyAnalytics(Base):
    """Pre-computed daily analytics per tenant per restaurant."""

    __tablename__ = "daily_analytics"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- Tenant Association ----
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- Restaurant Identification ----
    restaurant_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # e.g., "spice-garden" — identifies restaurant within tenant

    # ---- Sub-Tenant Link ----
    # Proper FK for the sub-tenant. restaurant_id above is kept in sync with
    # sub_tenant.slug so existing dashboards and filters keep working.
    sub_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sub_tenants.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- Date ----
    date: Mapped[date] = mapped_column(Date, nullable=False)

    # ---- Metrics ----
    total_conversations: Mapped[int] = mapped_column(Integer, default=0)
    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    total_revenue: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    conversion_rate: Mapped[float] = mapped_column(
        Numeric(5, 2), default=0
    )  # Percentage
    avg_session_duration: Mapped[float] = mapped_column(
        Numeric(8, 2), default=0
    )  # Seconds

    # ---- Breakdown Data ----
    top_items: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )  # [{name, count}]
    peak_hours: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )  # [{hour, count}]

    # ---- Timestamps ----
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # ---- Relationships ----
    tenant = relationship("Tenant", back_populates="daily_analytics")

    # ---- Indexes & Constraints ----
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "restaurant_id", "date",
            name="uq_daily_analytics_tenant_restaurant_date",
        ),
        Index("ix_daily_analytics_tenant_id", "tenant_id"),
        Index("ix_daily_analytics_restaurant_id", "restaurant_id"),
        Index("ix_daily_analytics_sub_tenant_id", "sub_tenant_id"),
        Index("ix_daily_analytics_date", "date"),
    )

    def __repr__(self) -> str:
        return f"<DailyAnalytics(id={self.id}, tenant={self.tenant_id}, restaurant='{self.restaurant_id}', date={self.date})>"
