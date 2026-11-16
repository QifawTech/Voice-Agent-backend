"""
Voice Agent Platform — Delivery Model

One row per attempt to reach someone, on one channel, for one occurrence. A
reminder that rang, then sent a WhatsApp message, then emailed is one
occurrence and three deliveries.

Why every attempt is logged, including the ones that were never sent:

    billing    every call and message costs money; the invoice has to be
               defensible line by line
    disputes   "nobody ever told me" is answered by a row with a timestamp
               and a provider message id
    honesty    the plan is blunt that an unreachable reminder must not be
               recorded as "sent". A skipped row with a reason is the only way
               the dashboard can show missed in red and mean it.

`cost` is nullable rather than zero-by-default on purpose: nothing yet knows
what a send cost, and storing 0.00 would make a free channel and an unpriced
one look identical in the billing total.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

DELIVERY_STATUSES = (
    "sent",     # the provider accepted it
    "failed",   # the provider rejected it, or the network did
    "skipped",  # never attempted — channel not configured, or no address
)


class Delivery(Base):
    """One attempt to reach someone on one channel."""

    __tablename__ = "deliveries"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- What it was for ----
    # Vasool's invoice_reminders table has the same shape as occurrences, so in
    # phase 5 this becomes one of two nullable parents rather than a new table.
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("occurrences.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- How ----
    channel: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "call" | "whatsapp" | "email" | "sms"
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # "smtp" | "livekit-sip" | "meta-cloud" | ...

    # The address actually used. Stored rather than derived from the person,
    # because a phone number can be edited later and a dispute is about what
    # was contacted at the time.
    recipient: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ---- Result ----
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)

    # Which rung of the ladder this was, copied from the occurrence at send
    # time. The occurrence's own attempt_no moves on; this one must not.
    attempt_no: Mapped[int | None] = mapped_column(
        Numeric(4, 0), nullable=True
    )

    # ---- Timestamps ----
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # ---- Relationships ----
    occurrence = relationship("Occurrence", back_populates="deliveries")

    # ---- Indexes ----
    __table_args__ = (
        Index("ix_deliveries_occurrence_id", "occurrence_id"),
        Index("ix_deliveries_channel", "channel"),
        Index("ix_deliveries_status", "status"),
        Index("ix_deliveries_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Delivery(id={self.id}, channel='{self.channel}', "
            f"status='{self.status}')>"
        )
