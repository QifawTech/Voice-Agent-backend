"""add reminders.deleted_at

Deleting a reminder must not delete what already happened. The occurrences a
reminder has already fired, and the delivery rows under them, are the answer to
"was she reminded on the 9th?" — and someone removing a rule from a list is not
asking for that record to disappear.

So a delete marks this column and the row stays. A reminder with deleted_at set
is hidden from every list, generates no occurrences, and is never claimed by
the tick.

Kept separate from is_paused deliberately: pausing means "not for now" and is
meant to be undone; deleting means "never again". One flag for both would make
the resume control in the interface either wrong or dangerous.

Revision ID: a7d3f1b86c42
Revises: f5c2a8e91b73
Create Date: 2026-11-16 14:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "a7d3f1b86c42"
down_revision = "f5c2a8e91b73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The tick and the nightly top-up both filter on this, and both already
    # filter on is_paused. A partial index over the live rows keeps those
    # queries reading only what they can act on.
    op.create_index(
        "ix_reminders_live",
        "reminders",
        ["agent_id"],
        postgresql_where=sa.text("deleted_at IS NULL AND is_paused IS false"),
    )


def downgrade() -> None:
    op.drop_index("ix_reminders_live", table_name="reminders")
    op.drop_column("reminders", "deleted_at")
