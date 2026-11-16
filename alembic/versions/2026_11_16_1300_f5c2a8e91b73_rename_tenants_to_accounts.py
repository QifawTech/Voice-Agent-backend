"""rename tenants to accounts

The word `tenant` means "a business that has customers". That was true when
this platform only did restaurants. It stopped being true the moment a person
could sign up purely to be reminded about their mother's tablets: that person
has no business, no customers, and no api_base_url, and calling their row a
tenant tells the next reader something false.

So `tenants` becomes `accounts`: someone who logged in, who owns agents. This
migration adds no capability and changes no data. It renames.

Three things deliberately keep the old word:

    sub_tenants       one tenant *of* a multi-tenant customer — one restaurant
                      inside SOPZY. That is the correct term for that thing,
                      and renaming it would make a right name wrong. Only its
                      foreign key to the parent becomes account_id.

    sub_tenant_id     same reasoning, everywhere it appears.

    tenant_tools      this table is on its way to becoming per-agent rather
                      than per-account (see the agent model). Renaming it to
                      account_tools now would mean renaming it again in a later
                      phase. Only its foreign key changes here.

`agents.account_id` already pointed at `tenants.id` by design, so it needs no
column rename — only the foreign key it targets moves.

Nothing is copied and no table is rebuilt. Every statement is a catalogue
rename, so this runs in milliseconds and reverses exactly.

Revision ID: f5c2a8e91b73
Revises: e3a91c5d7f48
Create Date: 2026-11-16 13:00:00.000000
"""

from alembic import op

revision = "f5c2a8e91b73"
down_revision = "e3a91c5d7f48"
branch_labels = None
depends_on = None


# The six tables carrying a foreign key to the accounts table. `agents` is
# absent on purpose: its column was named account_id from the start.
TABLES_WITH_FK = (
    "admin_users",
    "sub_tenants",
    "tenant_tools",
    "conversation_logs",
    "daily_analytics",
    "order_events",
)

# Constraints and indexes do not follow a table through a rename in Postgres,
# so each is renamed explicitly. Left alone, the accounts table would carry an
# index called ix_tenants_is_active and the next reader would be back where we
# started.
RENAMES = (
    # (object kind, old name, new name)
    ("INDEX", "ix_tenants_is_active", "ix_accounts_is_active"),
    ("INDEX", "ix_tenants_platform", "ix_accounts_platform"),
    ("INDEX", "ix_tenants_tenancy_type", "ix_accounts_tenancy_type"),
    ("INDEX", "ix_admin_users_tenant_id", "ix_admin_users_account_id"),
    ("INDEX", "ix_sub_tenants_tenant_id", "ix_sub_tenants_account_id"),
    ("INDEX", "ix_tenant_tools_tenant_id", "ix_tenant_tools_account_id"),
    ("INDEX", "ix_conversation_logs_tenant_id", "ix_conversation_logs_account_id"),
    ("INDEX", "ix_daily_analytics_tenant_id", "ix_daily_analytics_account_id"),
    ("INDEX", "ix_order_events_tenant_id", "ix_order_events_account_id"),
)

# Constraint renames go through ALTER TABLE, so they need their table too.
CONSTRAINT_RENAMES = (
    # (table, old name, new name)
    ("accounts", "tenants_pkey", "accounts_pkey"),
    ("accounts", "tenants_slug_key", "accounts_slug_key"),
    ("accounts", "tenants_widget_api_key_key", "accounts_widget_api_key_key"),
    ("admin_users", "admin_users_tenant_id_fkey", "admin_users_account_id_fkey"),
    ("sub_tenants", "sub_tenants_tenant_id_fkey", "sub_tenants_account_id_fkey"),
    ("sub_tenants", "uq_sub_tenants_tenant_slug", "uq_sub_tenants_account_slug"),
    ("tenant_tools", "tenant_tools_tenant_id_fkey", "tenant_tools_account_id_fkey"),
    ("tenant_tools", "uq_tenant_tools_tenant_name", "uq_tenant_tools_account_name"),
    (
        "conversation_logs",
        "conversation_logs_tenant_id_fkey",
        "conversation_logs_account_id_fkey",
    ),
    (
        "daily_analytics",
        "daily_analytics_tenant_id_fkey",
        "daily_analytics_account_id_fkey",
    ),
    (
        "daily_analytics",
        "uq_daily_analytics_tenant_restaurant_date",
        "uq_daily_analytics_account_restaurant_date",
    ),
    ("order_events", "order_events_tenant_id_fkey", "order_events_account_id_fkey"),
)


def upgrade() -> None:
    # 1. The table itself.
    op.rename_table("tenants", "accounts")

    # 2. Every foreign key column that pointed at it.
    for table in TABLES_WITH_FK:
        op.alter_column(table, "tenant_id", new_column_name="account_id")

    # 3. The indexes, which a table rename leaves behind.
    for kind, old, new in RENAMES:
        op.execute(f'ALTER {kind} "{old}" RENAME TO "{new}"')

    # 4. The constraints, same reason.
    for table, old, new in CONSTRAINT_RENAMES:
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"')


def downgrade() -> None:
    # Exactly the reverse, in the reverse order. Tested, not assumed: the plan
    # for this phase requires `alembic downgrade -1 && alembic upgrade head` to
    # run clean before the rename is committed.
    for table, old, new in CONSTRAINT_RENAMES:
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{new}" TO "{old}"')

    for kind, old, new in RENAMES:
        op.execute(f'ALTER {kind} "{new}" RENAME TO "{old}"')

    for table in TABLES_WITH_FK:
        op.alter_column(table, "account_id", new_column_name="tenant_id")

    op.rename_table("accounts", "tenants")
