"""
Voice Agent Platform — Agent Model

An agent is one job the voice engine does for one account: answer the phone for
a restaurant, ring you about your electricity bill, or chase your customers for
money. An account can own several.

Why this table exists now, in the engine phase, before any agent-picker UI:
every table below it (`people`, `reminders`) is keyed by agent_id. Keying them
by tenant_id instead and re-pointing them later would mean re-keying the whole
engine during the accounts migration — which is already the riskiest change in
the plan. One small table now avoids that.

On the column name `account_id`: it is a foreign key to `tenants.id` today.
What is called a Tenant is really an Account that owns agents, and the plan
renames the table. Naming the column for what it means rather than for the
table it currently points at means the rename moves one line, not thirty.

Nothing about the restaurant product changes: no existing table is touched, and
a restaurant tenant with no row here keeps working exactly as before.

Relationships:
    - Belongs to one Tenant (the account)
    - Has many People (Nudge: who gets reminded)
    - Has many Reminders
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# The three agent types at launch. Stored as a plain string rather than a
# Postgres ENUM: adding a fourth vertical should be a code change, not a
# migration that locks the table.
AGENT_TYPES = ("restaurant", "nudge", "vasool")


class Agent(Base):
    """One job the voice engine does for one account."""

    __tablename__ = "agents"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- Account Association ----
    # FK to tenants.id. See the module docstring on the name.
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- Identity ----
    type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # "restaurant" | "nudge" | "vasool"
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Unique per account, not globally: two unrelated accounts may both have a
    # "my-reminders" agent, and routes are account-scoped anyway.
    slug: Mapped[str] = mapped_column(String(100), nullable=False)

    # ---- Voice Configuration ----
    # Same shape as tenants.voice_config. Per-agent from the start, because the
    # reminder voice and the restaurant voice are different decisions.
    voice_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    # ---- Status ----
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

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
    account = relationship("Tenant", back_populates="agents")
    people = relationship(
        "Person",
        back_populates="agent",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    reminders = relationship(
        "Reminder",
        back_populates="agent",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    # ---- Indexes & Constraints ----
    __table_args__ = (
        UniqueConstraint("account_id", "slug", name="uq_agents_account_slug"),
        Index("ix_agents_account_id", "account_id"),
        Index("ix_agents_type", "type"),
        Index("ix_agents_is_active", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<Agent(id={self.id}, type='{self.type}', slug='{self.slug}')>"
