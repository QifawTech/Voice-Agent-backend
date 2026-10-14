"""add tenancy_type and sub_tenants

Adds the single-tenant / multi-tenant architecture choice:
  - tenants.tenancy_type ("single" | "multi"), existing rows become "single"
  - sub_tenants table (identity + display overrides only; NO api credentials,
    because there is one API per account on the parent tenant)
  - sub_tenant_id FK on conversation_logs / order_events / daily_analytics,
    kept alongside the existing restaurant_id string

Revision ID: a1c7f3d9e204
Revises: bd9e3aa04a8d
Create Date: 2026-10-06 00:00:00.000000+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1c7f3d9e204'
down_revision: Union[str, None] = 'bd9e3aa04a8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- tenants.tenancy_type ----
    # server_default makes existing rows valid without a separate backfill.
    op.add_column(
        'tenants',
        sa.Column(
            'tenancy_type',
            sa.String(length=20),
            nullable=False,
            server_default='single',
        ),
    )
    op.create_index('ix_tenants_tenancy_type', 'tenants', ['tenancy_type'], unique=False)

    # ---- sub_tenants ----
    op.create_table(
        'sub_tenants',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('slug', sa.String(length=255), nullable=False),
        sa.Column('upi_id', sa.String(length=255), nullable=True),
        sa.Column('voice_config', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'slug', name='uq_sub_tenants_tenant_slug'),
    )
    op.create_index('ix_sub_tenants_tenant_id', 'sub_tenants', ['tenant_id'], unique=False)
    op.create_index('ix_sub_tenants_is_active', 'sub_tenants', ['is_active'], unique=False)

    # ---- sub_tenant_id on the analytics-bearing tables ----
    # SET NULL on delete: a removed sub-tenant must never destroy call history.
    for table in ('conversation_logs', 'order_events', 'daily_analytics'):
        op.add_column(
            table,
            sa.Column('sub_tenant_id', postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f'fk_{table}_sub_tenant_id',
            table,
            'sub_tenants',
            ['sub_tenant_id'],
            ['id'],
            ondelete='SET NULL',
        )
        op.create_index(
            f'ix_{table}_sub_tenant_id', table, ['sub_tenant_id'], unique=False
        )


def downgrade() -> None:
    for table in ('daily_analytics', 'order_events', 'conversation_logs'):
        op.drop_index(f'ix_{table}_sub_tenant_id', table_name=table)
        op.drop_constraint(f'fk_{table}_sub_tenant_id', table, type_='foreignkey')
        op.drop_column(table, 'sub_tenant_id')

    op.drop_index('ix_sub_tenants_is_active', table_name='sub_tenants')
    op.drop_index('ix_sub_tenants_tenant_id', table_name='sub_tenants')
    op.drop_table('sub_tenants')

    op.drop_index('ix_tenants_tenancy_type', table_name='tenants')
    op.drop_column('tenants', 'tenancy_type')
