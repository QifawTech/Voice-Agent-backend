"""
Voice Agent Platform — OrderEvent Model

Tracks every order-related action taken through voice conversations.
Events include: order created, order checked, order cancelled.
Used for analytics and audit trails.

The restaurant_id identifies which specific restaurant (within a tenant)
the order belongs to.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OrderEvent(Base):
    """Order activity event log."""

    __tablename__ = "order_events"

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

    # ---- Session Link ----
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # ---- Order Data ----
    external_order_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # e.g., "ORD-1042"
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # "created" | "checked" | "cancelled"
    order_value: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    items: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )  # [{name, quantity, price}]

    # ---- Timestamps ----
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # ---- Relationships ----
    tenant = relationship("Tenant", back_populates="order_events")

    # ---- Indexes ----
    __table_args__ = (
        Index("ix_order_events_tenant_id", "tenant_id"),
        Index("ix_order_events_restaurant_id", "restaurant_id"),
        Index("ix_order_events_sub_tenant_id", "sub_tenant_id"),
        Index("ix_order_events_external_order_id", "external_order_id"),
        Index("ix_order_events_event_type", "event_type"),
        Index("ix_order_events_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<OrderEvent(id={self.id}, restaurant='{self.restaurant_id}', order='{self.external_order_id}', type='{self.event_type}')>"
