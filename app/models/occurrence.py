"""
Voice Agent Platform — Occurrence Model (one actual moment)

One reminder at one moment, with its own state. Generated from the rule ahead
of time — 60 days by default — which buys two things: the "next 7 days" screen
is a plain query, and a server outage cannot break the chain, because the rows
were already there.

Two columns carry the correctness of the whole engine:

    due_at        UTC. The only thing the tick loop compares against now().
    local_due_at  the same moment as wall-clock time, for display and
                  debugging. Derived, never authoritative.

Storing local time as the source of truth is the bug behind every "my 21:00
medicine reminder fired at 02:30" report. Compute in the person's timezone,
persist UTC, show local.

The status machine:

    scheduled ──claim──> claimed ──send──> sent ──answer──> done
        ▲                   │                │
        │                   │                ├──no rungs left──> missed
        └───quiet hours─────┘                └──snooze──> snoozed (+ new row)

    cancelled is terminal and external: the person opted out, the rule was
    deleted, or (in Vasool) the invoice was paid.

'claimed' is the state that makes concurrency safe, and it is also the state
that can strand work: a process that dies between claiming and sending leaves a
row claimed forever. claimed_at exists so the tick can hand those back.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Statuses an occurrence can hold. Plain strings, not a Postgres ENUM: adding
# a state should not require ALTER TYPE on a hot table.
STATUSES = (
    "scheduled",  # waiting for its moment
    "claimed",    # a worker has taken it and is about to send
    "sent",       # delivered on some channel, waiting for an answer
    "done",       # the person said done / tapped the button / paid
    "snoozed",    # pushed back; a separate occurrence carries the new time
    "missed",     # nobody answered, or it was too late to be useful
    "cancelled",  # it must not be sent at all
)

# Terminal states: nothing in the engine may move an occurrence out of these.
#
# 'snoozed' belongs here, which is easy to get wrong. A snooze does not pause
# this occurrence — it closes it and writes a *new* one at the new time. So the
# snoozed row is history, and the live reminder is its replacement. Treating it
# as still open would let a second snooze write a second replacement, giving
# the person two reminders for one evening.
TERMINAL_STATUSES = ("done", "missed", "cancelled", "snoozed")


class Occurrence(Base):
    """One reminder at one moment."""

    __tablename__ = "occurrences"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- Rule it came from ----
    reminder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reminders.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- When ----
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    local_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False
    )

    # For the expiry shape: how many days before the real due date this one is.
    # 270 means "nine months left, time to apply"; 0 means "expires today".
    # It is what lets four occurrences from one passport date say four
    # different things.
    offset_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- State ----
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled"
    )

    # Index into the reminder's channel_ladder. 0 = the first rung.
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # How many times a real send was actually attempted. Not the same as
    # attempt_no, and the difference is the point: a ladder of
    # ['call', 'whatsapp', 'email'] on a platform with neither calling nor
    # WhatsApp yet walks to index 2 having attempted once. The plan's hard cap
    # is three *contact attempts* — skipping a channel that does not exist is
    # not one of them, and counting it as one would silently shorten the ladder
    # for every reminder until phase 4 lands.
    attempts_made: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # When the escalation loop should look at this row again. Set after every
    # send; the escalation loop is driven entirely by this column.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Audit trail ----
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # How the answer arrived: "call" | "whatsapp" | "email" | "dashboard".
    answer_channel: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Why it ended the way it did — "no rungs left", "past grace window",
    # "person opted out". Shown on the dashboard, because "sent" with nothing
    # behind it is exactly the quiet failure the plan forbids.
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    reminder = relationship("Reminder", back_populates="occurrences")
    deliveries = relationship(
        "Delivery",
        back_populates="occurrence",
        cascade="all, delete-orphan",
    )

    # ---- Indexes & Constraints ----
    __table_args__ = (
        # The constraint that makes the nightly top-up safe to re-run: the same
        # rule at the same moment can only exist once, so generating 60 days
        # ahead every night inserts only what is genuinely new.
        UniqueConstraint("reminder_id", "due_at", name="uq_occurrences_reminder_due"),
        # The tick loop's query, in index form. Without this, finding what is
        # due becomes a full scan of every reminder ever generated.
        Index("ix_occurrences_due_queue", "status", "due_at"),
        # The escalation loop's query.
        Index("ix_occurrences_escalation", "status", "next_attempt_at"),
        Index("ix_occurrences_reminder_id", "reminder_id"),
        Index("ix_occurrences_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<Occurrence(id={self.id}, due_at={self.due_at.isoformat()}, "
            f"status='{self.status}', attempt={self.attempt_no})>"
        )
