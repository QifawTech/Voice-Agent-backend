"""
Voice Agent Platform — Reminder Model (the rule)

A reminder is the *recipe*, not the meal. "Electricity bill, 5th of every
month, 09:00" is written once and rarely changes; "5 November, 09:00" is one
actual moment with its own state, and lives in `occurrences`.

Conflating the two is the classic mistake in reminder systems, and the symptom
is always the same: snoozing tonight's reminder silently moves next month's.
Two tables, always.

Three shapes of reminder, not one — all three are needed or documents do not
work:

    repeating   electricity bill, 5th of every month   -> an RRULE
    expiry      passport expires 12 March 2027         -> due_date + warnings
    balance     FASTag running low                      -> a periodic check

Only `repeating` and `expiry` generate occurrences in this phase. `balance`
needs an API call per provider, so it is stored but not generated — the column
exists so adding it later is not a migration of live data.

Why warn_before_days must exist now: a passport reminder on the expiry day is
useless, because most countries refuse entry with under six months of validity.
One expiry date generates several occurrences, each knowing how far ahead it
is. Adding that later means rewriting occurrence generation.
"""

import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

SCHEDULE_KINDS = ("repeating", "expiry", "balance")

CATEGORIES = ("bill", "medicine", "renewal", "document", "business", "personal")

# The default escalation ladder. Per reminder, because urgency differs:
# medicine is call-then-whatsapp, a Netflix renewal is email and nothing more.
DEFAULT_CHANNEL_LADDER = ["call", "whatsapp", "email"]

# Warning schedule for an expiry with none given: nine months, three months,
# one month, and the day itself. The nine-month rung is the one that matters
# for passports and the one every other product misses.
DEFAULT_WARN_BEFORE_DAYS = [270, 90, 30, 0]


class Reminder(Base):
    """The rule. One row, written once; occurrences are generated from it."""

    __tablename__ = "reminders"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- Associations ----
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("people.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- What it is ----
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(
        String(30), nullable=False, default="personal"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- When ----
    schedule_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="repeating"
    )

    # RFC 5545 RRULE, without the DTSTART line: "FREQ=MONTHLY;BYMONTHDAY=5".
    # Expanded by dateutil. Used when schedule_kind = 'repeating'.
    rrule: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Used when schedule_kind = 'expiry'.
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    warn_before_days: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )  # [270, 90, 30, 0] — days before due_date

    # The wall-clock time, in the reminder's own timezone. Stored apart from the
    # rule so "monthly on the 5th" and "at 09:00" stay independently editable.
    time_of_day: Mapped[time] = mapped_column(
        Time, nullable=False, default=time(9, 0)
    )

    # Copied from the person at creation, then owned by the reminder. The plan's
    # first trap: store the timezone here, compute in local, persist UTC. A
    # reminder must not silently change hour because the person moved.
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Kolkata"
    )

    # ---- How to reach them ----
    channel_ladder: Mapped[list] = mapped_column(
        JSON, nullable=False, default=lambda: list(DEFAULT_CHANNEL_LADDER)
    )
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # How late is too late. Past this, an occurrence is marked missed instead of
    # firing — otherwise a three-hour outage delivers "time for your tablet" at
    # midnight. Per reminder, because medicine and a domain renewal disagree
    # about what counts as still useful.
    grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)

    # ---- Who to tell when it is missed ----
    # The caregiver feature: the son in Chennai hears that his mother did not
    # answer. Set from the creating user's email, and the only reason Phase 1
    # can honour "and the son is told" without a notifications system.
    notify_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ---- Status ----
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Deleted, but not gone. The row stays because its past occurrences and
    # their delivery log are the answer to "was she reminded on the 9th?", and
    # deleting the rule is no reason to lose that. A deleted reminder is hidden
    # from every list, generates nothing, and is never claimed.
    #
    # Separate from is_paused on purpose: pausing is "not for now" and is meant
    # to be undone, deleting is "never again". One flag covering both would
    # make the resume button in the interface either wrong or dangerous.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # How far occurrences have been generated. Lets the nightly top-up skip
    # reminders it has already covered instead of re-expanding every rule every
    # night. Advisory only: the unique index is what actually prevents
    # duplicates, so a wrong value here costs work, never correctness.
    generated_through: Mapped[date | None] = mapped_column(Date, nullable=True)

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
    agent = relationship("Agent", back_populates="reminders")
    person = relationship("Person", back_populates="reminders")
    occurrences = relationship(
        "Occurrence",
        back_populates="reminder",
        cascade="all, delete-orphan",
    )

    # ---- Indexes ----
    __table_args__ = (
        Index("ix_reminders_agent_id", "agent_id"),
        Index("ix_reminders_person_id", "person_id"),
        Index("ix_reminders_is_paused", "is_paused"),
        Index("ix_reminders_schedule_kind", "schedule_kind"),
    )

    def __repr__(self) -> str:
        return (
            f"<Reminder(id={self.id}, title='{self.title}', "
            f"kind='{self.schedule_kind}', tz='{self.timezone}')>"
        )
