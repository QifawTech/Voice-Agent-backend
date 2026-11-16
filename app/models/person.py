"""
Voice Agent Platform — Person Model

The thing the platform had no concept of: someone who receives a message but is
not a user. A mother getting a medicine reminder has no login, no password and
no app. She is not a account and not an admin.

Everything that decides *how* to reach someone lives here rather than on the
reminder, because it belongs to the person: her language, her timezone, the
hours she must not be called, and whether she has told us to stop.

Two fields carry most of the legal and reputational weight of this product:

    consent_confirmed_at   who agreed that this person may be contacted
    opted_out_at           she said stop

opted_out_at is read-only to business owners by design. The plan is explicit:
if an owner can re-enable someone who said STOP, the platform's WhatsApp sender
gets reported, and that takes down every other account — not just his. No route
in this product may ever clear it.

Relationships:
    - Belongs to one Agent
    - Has many Reminders
"""

import uuid
from datetime import datetime, time, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Person(Base):
    """Someone who receives reminders. Not a user of the platform."""

    __tablename__ = "people"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- Agent Association ----
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- Identity ----
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Whether this person is the account holder themselves, rather than someone
    # they look after. The plan's number policy turns on exactly this: the
    # shared WhatsApp sender is allowed for the account holder and their own
    # family, and never for a third party.
    is_account_holder: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # ---- How to reach them ----
    # All three are optional, but a person with none of them can never be
    # reached. The create route rejects that rather than storing a reminder
    # that cannot fire.
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    phone_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- How to speak to them ----
    language: Mapped[str] = mapped_column(
        String(10), nullable=False, default="en"
    )  # "en" | "ta" | "te" | "kn" | "ml"

    # An IANA name ("Asia/Kolkata"), never a UTC offset: an offset cannot
    # survive a DST change, and "21:00 her time" has to mean 21:00 in July too.
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Kolkata"
    )

    # ---- Quiet hours ----
    # Local wall-clock times. The pair wraps midnight (22:00 → 07:00), which is
    # the normal case, so the comparison is not a simple between. Enforced in
    # the sending path, never only in the UI.
    quiet_hours_start: Mapped[time] = mapped_column(
        Time, nullable=False, default=time(22, 0)
    )
    quiet_hours_end: Mapped[time] = mapped_column(
        Time, nullable=False, default=time(7, 0)
    )

    # ---- Consent ----
    consent_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Who confirmed it — an admin_users.id, kept as text so a deleted user does
    # not erase the record of who agreed. A consent record that can disappear
    # is not a consent record.
    consent_confirmed_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # ---- Opt-out (write-once; see the module docstring) ----
    opted_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opt_out_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Timestamps ----
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # ---- Relationships ----
    agent = relationship("Agent", back_populates="people")
    reminders = relationship(
        "Reminder",
        back_populates="person",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    # ---- Indexes ----
    __table_args__ = (
        Index("ix_people_agent_id", "agent_id"),
        Index("ix_people_phone", "phone"),
        Index("ix_people_opted_out_at", "opted_out_at"),
    )

    @property
    def is_contactable(self) -> bool:
        """False once they have opted out, whatever else is set."""
        return self.opted_out_at is None

    def address_for(self, channel: str) -> str | None:
        """The address this person is reachable at on a given channel."""
        return {
            "call": self.phone,
            "whatsapp": self.whatsapp or self.phone,
            "email": self.email,
            "sms": self.phone,
        }.get(channel)

    def __repr__(self) -> str:
        return (
            f"<Person(id={self.id}, name='{self.name}', "
            f"lang='{self.language}', tz='{self.timezone}')>"
        )
