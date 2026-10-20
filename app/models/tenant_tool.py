"""
Voice Agent Platform — TenantTool Model

One row is one function tool the voice agent can call for a tenant. What used
to be seven hardcoded Python handlers now lives here, so a tenant can enable,
disable, edit or add tools from the dashboard without a deploy.

A row carries two halves:

    1. The LLM-facing contract — name, description, parameters (JSON Schema).
       This is what gets sent to DeepSeek / LiveKit as the tool definition.

    2. The HTTP recipe — method, path, query, body and header templates with
       {placeholder} slots filled from the tool call's arguments at runtime.

The path is always relative and is appended to the parent tenant's
api_base_url: a tool cannot point somewhere else. See app/services/tool_spec.py
for the templating rules and the checks that enforce this.

The seven restaurant tools (check_menu, create_order, …) ship as presets in
app/services/tool_presets.py and are seeded per tenant, flagged is_builtin so
the dashboard can mark them and offer a reset.

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
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TenantTool(Base):
    """A function tool the agent can call, owned by one tenant."""

    __tablename__ = "tenant_tools"

    # ---- Primary Key ----
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ---- Owner ----
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- LLM-facing contract ----
    # `name` is what the model emits in a tool call, so it must be a valid
    # function identifier and unique within the tenant.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    parameters: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=lambda: {"type": "object", "properties": {}}
    )

    # ---- HTTP recipe ----
    http_method: Mapped[str] = mapped_column(String(10), nullable=False, default="GET")
    # Relative to the tenant's api_base_url, e.g. "/voice-agent/orders/{order_id}".
    path_template: Mapped[str] = mapped_column(String(500), nullable=False)
    query_template: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    body_template: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    headers_template: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ---- Spoken result ----
    # How to turn the API's JSON into one sentence the agent can say. NULL falls
    # back to a generic summary. Kept separate from `description` because this
    # one is read out loud — raw JSON in a TTS stream is unlistenable.
    success_template: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Status ----
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # True for a tool seeded from tool_presets.py. Still fully editable — the
    # flag only drives the "built-in" badge and the reset-to-preset action.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

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
    tenant = relationship("Tenant", back_populates="tools")

    # ---- Indexes & Constraints ----
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tenant_tools_tenant_name"),
        Index("ix_tenant_tools_tenant_id", "tenant_id"),
        Index("ix_tenant_tools_is_enabled", "is_enabled"),
    )

    def __repr__(self) -> str:
        return (
            f"<TenantTool(id={self.id}, tenant={self.tenant_id}, "
            f"name='{self.name}', enabled={self.is_enabled})>"
        )
