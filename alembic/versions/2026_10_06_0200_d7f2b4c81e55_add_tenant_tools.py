"""add tenant_tools

Moves the voice agent's function tools out of Python and into the database, so
a tenant can add, edit, enable or disable them from the dashboard:

  - tenant_tools table: the LLM-facing contract (name, description, parameters)
    plus the HTTP recipe (method, path, query/body/header templates)
  - backfill: every existing tenant is seeded with the seven built-in
    restaurant tools, defined exactly as the hardcoded handlers behaved, so
    nothing about a live agent changes when this runs

The backfill reads its definitions from app.services.tool_presets rather than
copying them here. Normally a migration should not import application code —
the code moves on and the migration has to keep describing the past. The
exception holds here because these rows ARE the former hardcoded behaviour: if
the presets change before this migration runs somewhere, seeding the new ones
is the correct outcome, and it keeps one definition of the restaurant tools
instead of two that can disagree.

Revision ID: d7f2b4c81e55
Revises: c4e8b1a7f932
Create Date: 2026-10-06 02:00:00.000000+00:00
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd7f2b4c81e55'
down_revision: Union[str, None] = 'c4e8b1a7f932'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tenant_tools = op.create_table(
        'tenant_tools',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('display_name', sa.String(length=120), nullable=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('parameters', postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column('http_method', sa.String(length=10), nullable=False, server_default='GET'),
        sa.Column('path_template', sa.String(length=500), nullable=False),
        sa.Column('query_template', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('body_template', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('headers_template', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('success_template', sa.Text(), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('is_builtin', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_tenant_tools_tenant_name'),
    )
    op.create_index('ix_tenant_tools_tenant_id', 'tenant_tools', ['tenant_id'], unique=False)
    op.create_index('ix_tenant_tools_is_enabled', 'tenant_tools', ['is_enabled'], unique=False)

    _seed_existing_tenants(tenant_tools)


def _seed_existing_tenants(tenant_tools) -> None:
    """Give every existing tenant the seven built-in restaurant tools."""
    from app.services.tool_presets import preset_rows

    conn = op.get_bind()
    tenant_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM tenants"))]
    if not tenant_ids:
        return

    now = datetime.now(timezone.utc)
    presets = preset_rows()

    rows = [
        {
            'id': uuid.uuid4(),
            'tenant_id': tenant_id,
            'name': preset['name'],
            'display_name': preset.get('display_name'),
            'description': preset['description'],
            'parameters': preset['parameters'],
            'http_method': preset['http_method'],
            'path_template': preset['path_template'],
            'query_template': preset.get('query_template'),
            'body_template': preset.get('body_template'),
            'headers_template': preset.get('headers_template'),
            'success_template': preset.get('success_template'),
            'is_enabled': True,
            'is_builtin': True,
            'sort_order': preset.get('sort_order', 0),
            'created_at': now,
            'updated_at': now,
        }
        for tenant_id in tenant_ids
        for preset in presets
    ]

    op.bulk_insert(tenant_tools, rows)


def downgrade() -> None:
    op.drop_index('ix_tenant_tools_is_enabled', table_name='tenant_tools')
    op.drop_index('ix_tenant_tools_tenant_id', table_name='tenant_tools')
    op.drop_table('tenant_tools')
