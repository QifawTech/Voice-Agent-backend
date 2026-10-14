"""make tenancy_type nullable so "not chosen yet" is a real state

People are let into the app before being asked whether their product is single
or multi tenant. NOT NULL DEFAULT 'single' meant an unanswered question looked
identical to a deliberate "single", so we could never tell who still needed
prompting. NULL now means "not chosen"; code treats it as single wherever
behaviour must not block.

Existing rows keep 'single' — they were already running as single-tenant and
re-prompting them would be noise.

Revision ID: c4e8b1a7f932
Revises: a1c7f3d9e204
Create Date: 2026-10-06 01:00:00.000000+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e8b1a7f932'
down_revision: Union[str, None] = 'a1c7f3d9e204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the server default first, so NEW rows are NULL (unanswered) rather
    # than silently claiming to be single-tenant.
    op.alter_column(
        'tenants',
        'tenancy_type',
        existing_type=sa.String(length=20),
        nullable=True,
        server_default=None,
    )


def downgrade() -> None:
    # Anything still unanswered becomes 'single', which is how it behaved before.
    op.execute("UPDATE tenants SET tenancy_type = 'single' WHERE tenancy_type IS NULL")
    op.alter_column(
        'tenants',
        'tenancy_type',
        existing_type=sa.String(length=20),
        nullable=False,
        server_default='single',
    )
