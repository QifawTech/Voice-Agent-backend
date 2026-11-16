"""
Voice Agent Platform — Account Model

An account is someone who signed up. That is all it means.

It used to be called a Account, which meant "a business that has customers" —
true for Spice Garden, false for a son in Chennai who signed up only to be
reminded about his mother's tablets. He has no customers and no api_base_url.
Calling his row a account told the next reader something that was not so.

What an account owns depends on what it is for:
- A multi-restaurant platform like SOPZY  (tenancy_type="multi", many SubTenants)
- A standalone single-restaurant website  (tenancy_type="single", no SubTenants)
- A person with reminders and nothing else (no sub-tenants, no API at all)

The API connection (api_base_url + api_auth_token) always lives HERE, even for
multi-tenant customers — there is only one API per account. Sub-tenants are
selected by sending their slug to that API as `?restaurantId=<slug>`.

Note that SubTenant keeps its name on purpose. It means "one account *of* a
multi-tenant customer" — one restaurant inside SOPZY — and that is the right
word for that thing.

Relationships:
    - Has many AdminUsers
    - Has many Agents (the jobs the voice engine does for it)
    - Has many SubTenants (multi-tenant accounts only)
    - Has many TenantTools (the function tools its agent can call)
    - Has many ConversationLogs
    - Has many OrderEvents
    - Has many DailyAnalytics
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Account(Base):
    """One signed-up account, and everything configured on it."""

    __tablename__ = "accounts"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- Basic Info ----
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(
        String(50), nullable=False, default="standalone"
    )  # "sopzy" | "standalone" | "custom"

    # ---- Architecture ----
    # "single" = one product, no sub-tenants (config lives here).
    # "multi"  = many sub-tenants, each selected by passing its slug to the SAME api_base_url.
    # NULL     = not chosen yet. This is a real state, not a missing value: we let
    #            people into the app before asking, and prompt when it actually
    #            matters. Treated as "single" everywhere behaviour must not block.
    tenancy_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default=None
    )  # "single" | "multi" | None

    # ---- API Connection ----
    api_base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_auth_token: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Encrypted at rest
    api_validated: Mapped[bool] = mapped_column(Boolean, default=False)

    # ---- Widget Auth ----
    widget_api_key: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )

    # ---- Payment ----
    upi_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ---- Voice Configuration ----
    voice_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )  # {greeting, language, tone}

    # ---- Status ----
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

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
    admin_users = relationship("AdminUser", back_populates="account", lazy="selectin")
    # The agents this account owns. A restaurant account has no rows here and
    # is unaffected.
    agents = relationship(
        "Agent",
        back_populates="account",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    sub_tenants = relationship(
        "SubTenant",
        back_populates="account",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    tools = relationship(
        "TenantTool",
        back_populates="account",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    conversation_logs = relationship(
        "ConversationLog", back_populates="account", lazy="selectin"
    )
    order_events = relationship("OrderEvent", back_populates="account", lazy="selectin")
    daily_analytics = relationship(
        "DailyAnalytics", back_populates="account", lazy="selectin"
    )

    # ---- Indexes ----
    __table_args__ = (
        Index("ix_accounts_platform", "platform"),
        Index("ix_accounts_is_active", "is_active"),
        Index("ix_accounts_tenancy_type", "tenancy_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<Account(id={self.id}, name='{self.name}', "
            f"platform='{self.platform}', tenancy='{self.tenancy_type}')>"
        )
