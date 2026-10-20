"""
Voice Agent Platform — ConversationLog Model

Logs every voice conversation for analytics and debugging.
Each row represents a complete conversation session between
a customer and the AI voice agent.

The restaurant_id identifies which specific restaurant (within a tenant)
the conversation belongs to. For standalone tenants, this may be null.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ConversationLog(Base):
    """Voice conversation log entry."""

    __tablename__ = "conversation_logs"

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
    )  # e.g., "spice-garden" — sent by widget, identifies restaurant within tenant

    # ---- Sub-Tenant Link ----
    # Proper FK for the sub-tenant. restaurant_id above is kept in sync with
    # sub_tenant.slug so existing dashboards and filters keep working.
    sub_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sub_tenants.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- Session Info ----
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # ---- Conversation Data ----
    messages: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )  # [{role, content, timestamp}]
    outcome: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "ordered" | "browsed" | "cancelled"
    order_value: Mapped[float | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )  # Revenue if order was placed

    # ---- Metrics ----
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)

    # ---- Timestamps ----
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Relationships ----
    tenant = relationship("Tenant", back_populates="conversation_logs")

    # ---- Indexes ----
    __table_args__ = (
        Index("ix_conversation_logs_tenant_id", "tenant_id"),
        Index("ix_conversation_logs_restaurant_id", "restaurant_id"),
        Index("ix_conversation_logs_sub_tenant_id", "sub_tenant_id"),
        Index("ix_conversation_logs_session_id", "session_id"),
        Index("ix_conversation_logs_outcome", "outcome"),
        Index("ix_conversation_logs_started_at", "started_at"),
    )

    def __repr__(self) -> str:
        return f"<ConversationLog(id={self.id}, session='{self.session_id}', restaurant='{self.restaurant_id}', outcome='{self.outcome}')>"
