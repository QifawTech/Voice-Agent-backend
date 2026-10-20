"""
Voice Agent Platform — SubTenant Model

Represents one tenant *of* a multi-tenant customer. For a restaurant platform
like SOPZY, one SubTenant is one restaurant.

IMPORTANT — there is only ONE API per account:
    Sub-tenants do NOT hold api_base_url or api_auth_token. The customer exposes
    a single API on the parent Tenant, and we select the sub-tenant by sending
    its slug to that API as `?restaurantId=<slug>`.

    name and slug are owned by the customer's API and are refreshed by
    SubTenantService.sync_from_api(). They are not editable from our dashboard.
    Only the display overrides (upi_id, voice_config) are ours to change.

Relationships:
    - Belongs to one Tenant
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SubTenant(Base):
    """A tenant belonging to a multi-tenant customer."""

    __tablename__ = "sub_tenants"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- Parent Tenant ----
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- Identity (owned by the customer's API — read-only for us) ----
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # Sent to the parent's API as ?restaurantId=<slug>

    # ---- Display Overrides (ours; NULL means inherit from the parent tenant) ----
    upi_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    voice_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )  # {greeting, language, tone}

    # ---- Status ----
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ---- Sync Tracking ----
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
    tenant = relationship("Tenant", back_populates="sub_tenants")

    # ---- Indexes & Constraints ----
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "slug", name="uq_sub_tenants_tenant_slug"
        ),
        Index("ix_sub_tenants_tenant_id", "tenant_id"),
        Index("ix_sub_tenants_is_active", "is_active"),
    )

    def __repr__(self) -> str:
        return (
            f"<SubTenant(id={self.id}, tenant={self.tenant_id}, "
            f"slug='{self.slug}', active={self.is_active})>"
        )
