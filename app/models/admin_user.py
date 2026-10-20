"""
Voice Agent Platform — AdminUser Model

Represents an admin user who manages tenants on the platform.
- Super admins (tenant_id=NULL) can manage all tenants.
- Tenant admins are scoped to a single tenant.

Used by the auth system (Day 4) for registration, login, and JWT.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AdminUser(Base):
    """Platform admin user account."""

    __tablename__ = "admin_users"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- Auth Fields ----
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # ---- Tenant Association ----
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,  # NULL = super-admin with access to all tenants
    )
    role: Mapped[str] = mapped_column(
        String(50), nullable=False, default="tenant_admin"
    )  # "super_admin" | "tenant_admin"

    # ---- Status ----
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
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
    tenant = relationship("Tenant", back_populates="admin_users")

    # ---- Indexes ----
    __table_args__ = (
        Index("ix_admin_users_tenant_id", "tenant_id"),
        Index("ix_admin_users_role", "role"),
    )

    def __repr__(self) -> str:
        return f"<AdminUser(id={self.id}, email='{self.email}', role='{self.role}')>"
