"""add reminder engine tables

Phase 1 of the Nudge and Vasool plan: the five tables the reminder engine needs.

    agents        one job the voice engine does for an account
    people        someone who receives a message but is not a user
    reminders     the rule — "electricity bill, 5th of every month, 09:00"
    occurrences   one actual moment, with its own state
    deliveries    one attempt to reach someone, on one channel

Nothing existing is altered. `tenants`, `admin_users`, `tenant_tools`,
`conversation_logs`, `order_events`, `daily_analytics` and `sub_tenants` are
untouched, and a restaurant tenant with no rows in these tables behaves exactly
as it did before. That is deliberate: the accounts/agents rename is the
riskiest change in the plan and belongs in its own migration, with its own
tested rollback, in phase 2. This one only adds.

`agents.account_id` is a foreign key to `tenants.id`, named for what it means
rather than for the table it currently points at, so the phase 2 rename moves
one line instead of thirty.

Two indexes here are the engine's hot path rather than ordinary lookups:

    ix_occurrences_due_queue    (status, due_at) — the tick's query
    ix_occurrences_escalation   (status, next_attempt_at) — the ladder's query

And one constraint carries the idempotency the whole design leans on:

    uq_occurrences_reminder_due (reminder_id, due_at)

That is what lets the nightly top-up regenerate the next 60 days every night
with no cursor and no bookkeeping: a row that already exists is rejected by the
database rather than detected by the application, so two workers running the
top-up at the same moment cannot both insert it.

Revision ID: e3a91c5d7f48
Revises: d7f2b4c81e55
Create Date: 2026-11-10 09:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e3a91c5d7f48"
down_revision: Union[str, None] = "d7f2b4c81e55"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------------------------------------------------------------- agents
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column(
            "voice_config",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "slug", name="uq_agents_account_slug"),
    )
    op.create_index("ix_agents_account_id", "agents", ["account_id"])
    op.create_index("ix_agents_type", "agents", ["type"])
    op.create_index("ix_agents_is_active", "agents", ["is_active"])

    # ---------------------------------------------------------------- people
    op.create_table(
        "people",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "is_account_holder",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("whatsapp", sa.String(length=20), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column(
            "phone_verified_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "language", sa.String(length=10), nullable=False, server_default="en"
        ),
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default="Asia/Kolkata",
        ),
        # Quiet hours default to 22:00–07:00 at the database level as well as in
        # the model. A row inserted by a script that forgot them must still be
        # protected: the plan's worst failure is a 2:30 a.m. call to an elderly
        # person, and a default that only exists in Python is one bulk insert
        # away from not existing.
        sa.Column(
            "quiet_hours_start",
            sa.Time(),
            nullable=False,
            server_default="22:00:00",
        ),
        sa.Column(
            "quiet_hours_end",
            sa.Time(),
            nullable=False,
            server_default="07:00:00",
        ),
        sa.Column(
            "consent_confirmed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("consent_confirmed_by", sa.String(length=255), nullable=True),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_people_agent_id", "people", ["agent_id"])
    op.create_index("ix_people_phone", "people", ["phone"])
    op.create_index("ix_people_opted_out_at", "people", ["opted_out_at"])

    # ------------------------------------------------------------- reminders
    op.create_table(
        "reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "category",
            sa.String(length=30),
            nullable=False,
            server_default="personal",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "schedule_kind",
            sa.String(length=20),
            nullable=False,
            server_default="repeating",
        ),
        sa.Column("rrule", sa.String(length=500), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column(
            "warn_before_days",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "time_of_day", sa.Time(), nullable=False, server_default="09:00:00"
        ),
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default="Asia/Kolkata",
        ),
        sa.Column(
            "channel_ladder",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "max_attempts", sa.Integer(), nullable=False, server_default="3"
        ),
        sa.Column(
            "grace_minutes", sa.Integer(), nullable=False, server_default="120"
        ),
        sa.Column("notify_email", sa.String(length=255), nullable=True),
        sa.Column(
            "is_paused", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("generated_through", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reminders_agent_id", "reminders", ["agent_id"])
    op.create_index("ix_reminders_person_id", "reminders", ["person_id"])
    op.create_index("ix_reminders_is_paused", "reminders", ["is_paused"])
    op.create_index("ix_reminders_schedule_kind", "reminders", ["schedule_kind"])

    # ----------------------------------------------------------- occurrences
    op.create_table(
        "occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reminder_id", postgresql.UUID(as_uuid=True), nullable=False),
        # UTC, and the only column the tick loop compares against now().
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        # The same moment as wall-clock time, for display and debugging.
        # Deliberately without a timezone: it is not an instant, it is what a
        # clock on the person's wall reads.
        sa.Column("local_due_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("offset_days", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "attempts_made", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer_channel", sa.String(length=20), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["reminder_id"], ["reminders.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reminder_id", "due_at", name="uq_occurrences_reminder_due"
        ),
    )
    # The tick loop's query, in index form: "scheduled rows due before now,
    # oldest first". Without it, finding what is due is a sequential scan over
    # every occurrence ever generated — which grows forever.
    op.create_index(
        "ix_occurrences_due_queue", "occurrences", ["status", "due_at"]
    )
    op.create_index(
        "ix_occurrences_escalation", "occurrences", ["status", "next_attempt_at"]
    )
    op.create_index("ix_occurrences_reminder_id", "occurrences", ["reminder_id"])
    op.create_index("ix_occurrences_status", "occurrences", ["status"])

    # ------------------------------------------------------------ deliveries
    op.create_table(
        "deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurrence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("recipient", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # Nullable rather than defaulting to zero: nothing yet knows what a
        # send cost, and 0.00 would make a free channel and an unpriced one
        # indistinguishable in a billing total.
        sa.Column("cost", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("attempt_no", sa.Numeric(precision=4, scale=0), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["occurrence_id"], ["occurrences.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_deliveries_occurrence_id", "deliveries", ["occurrence_id"]
    )
    op.create_index("ix_deliveries_channel", "deliveries", ["channel"])
    op.create_index("ix_deliveries_status", "deliveries", ["status"])
    op.create_index("ix_deliveries_created_at", "deliveries", ["created_at"])


def downgrade() -> None:
    """
    Drop the five tables, children first.

    This is a real rollback rather than a stub, and it is cheap to trust
    precisely because this migration only adds: dropping these tables returns
    the database to exactly its previous shape, with no restaurant data
    involved anywhere. The order matters — deliveries reference occurrences,
    occurrences reference reminders, reminders reference people and agents.
    """
    op.drop_index("ix_deliveries_created_at", table_name="deliveries")
    op.drop_index("ix_deliveries_status", table_name="deliveries")
    op.drop_index("ix_deliveries_channel", table_name="deliveries")
    op.drop_index("ix_deliveries_occurrence_id", table_name="deliveries")
    op.drop_table("deliveries")

    op.drop_index("ix_occurrences_status", table_name="occurrences")
    op.drop_index("ix_occurrences_reminder_id", table_name="occurrences")
    op.drop_index("ix_occurrences_escalation", table_name="occurrences")
    op.drop_index("ix_occurrences_due_queue", table_name="occurrences")
    op.drop_table("occurrences")

    op.drop_index("ix_reminders_schedule_kind", table_name="reminders")
    op.drop_index("ix_reminders_is_paused", table_name="reminders")
    op.drop_index("ix_reminders_person_id", table_name="reminders")
    op.drop_index("ix_reminders_agent_id", table_name="reminders")
    op.drop_table("reminders")

    op.drop_index("ix_people_opted_out_at", table_name="people")
    op.drop_index("ix_people_phone", table_name="people")
    op.drop_index("ix_people_agent_id", table_name="people")
    op.drop_table("people")

    op.drop_index("ix_agents_is_active", table_name="agents")
    op.drop_index("ix_agents_type", table_name="agents")
    op.drop_index("ix_agents_account_id", table_name="agents")
    op.drop_table("agents")
