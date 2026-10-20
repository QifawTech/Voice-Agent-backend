"""
Voice Agent Platform — Tenant Schemas

Pydantic models for tenant API request validation and response serialization.
A tenant represents a platform/business (e.g., SOPZY, standalone restaurant).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ---- Request Schemas ----


class TenantCreate(BaseModel):
    """Request body for creating a new tenant (platform/business)."""

    name: str = Field(..., min_length=1, max_length=255, examples=["SOPZY"])
    slug: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        examples=["sopzy"],
    )
    platform: str = Field(
        default="standalone",
        pattern=r"^(sopzy|standalone|custom)$",
        examples=["standalone"],
    )
    tenancy_type: str | None = Field(
        default=None,
        pattern=r"^(single|multi)$",
        description=(
            "'single' = one product, no sub-tenants. 'multi' = many sub-tenants, all "
            "served by this same API. Omit it to decide later — the app prompts when "
            "the choice starts to matter."
        ),
        examples=["single"],
    )
    api_base_url: str | None = Field(
        default=None, max_length=500, examples=["https://api.sopzy.com"]
    )
    api_auth_token: str | None = Field(
        default=None, examples=["sk_live_abc123"]
    )
    upi_id: str | None = Field(
        default=None, max_length=255, examples=["spicegarden@upi"]
    )
    voice_config: dict | None = Field(
        default=None,
        examples=[{"greeting": "Welcome!", "language": "en", "tone": "friendly"}],
    )


class TenantUpdate(BaseModel):
    """Request body for updating a tenant. All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    platform: str | None = Field(
        default=None, pattern=r"^(sopzy|standalone|custom)$"
    )
    tenancy_type: str | None = Field(
        default=None, pattern=r"^(single|multi)$"
    )
    api_base_url: str | None = Field(default=None, max_length=500)
    api_auth_token: str | None = None
    upi_id: str | None = Field(default=None, max_length=255)
    voice_config: dict | None = None
    is_active: bool | None = None


# ---- Response Schemas ----


class TenantResponse(BaseModel):
    """Response body for a single tenant. Excludes sensitive fields like api_auth_token."""

    id: UUID
    name: str
    slug: str
    platform: str
    tenancy_type: str | None = None
    api_base_url: str | None = None
    api_validated: bool
    widget_api_key: str | None = None
    upi_id: str | None = None
    voice_config: dict | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TenantListResponse(BaseModel):
    """Paginated list of tenants."""

    tenants: list[TenantResponse]
    total: int
    page: int = 1
    page_size: int = 20


# ---- Sub-Tenant Schemas ----
#
# Sub-tenants are SYNCED from the customer's API, never hand-created here, so
# there is no SubTenantCreate. `name` and `slug` are owned by their API; the
# only thing we let the dashboard change is the display overrides.


class SubTenantUpdate(BaseModel):
    """Request body for editing a sub-tenant. Display overrides only."""

    upi_id: str | None = Field(default=None, max_length=255)
    voice_config: dict | None = Field(
        default=None,
        examples=[{"greeting": "Welcome to Ocean Grill!", "language": "en"}],
    )
    is_active: bool | None = None


class SubTenantResponse(BaseModel):
    """Response body for a single sub-tenant."""

    id: UUID
    tenant_id: UUID
    name: str
    slug: str
    upi_id: str | None = None
    voice_config: dict | None = None
    is_active: bool
    last_synced_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SubTenantListResponse(BaseModel):
    """List of sub-tenants for a tenant."""

    sub_tenants: list[SubTenantResponse]
    total: int


class SyncDiagnostic(BaseModel):
    """
    Why a sync or endpoint test failed, in enough detail for a developer to fix it
    without contacting support.

    `raw_response` is the key field: showing what their API actually returned,
    beside what we expected, is what makes a shape mismatch obvious at a glance.
    """

    reason: str = Field(
        description=(
            "Machine-readable cause: not_configured | not_found | unauthorized | "
            "timeout | unreachable | bad_shape | empty | server_error"
        ),
        examples=["bad_shape"],
    )
    message: str = Field(
        description="Plain-English explanation shown directly to the user.",
    )
    url_called: str | None = Field(
        default=None,
        description="The exact URL we requested, so they can verify the path.",
    )
    status_code: int | None = None
    raw_response: str | None = Field(
        default=None,
        description="First 200 characters of their actual response body.",
    )


class SubTenantSyncResponse(BaseModel):
    """Result of a sync run against the customer's listing endpoint."""

    success: bool
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    total_active: int = 0
    sub_tenants: list[SubTenantResponse] = Field(default_factory=list)
    diagnostic: SyncDiagnostic | None = Field(
        default=None,
        description="Present only when success is false.",
    )
