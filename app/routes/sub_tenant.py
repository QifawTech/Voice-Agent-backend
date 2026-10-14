"""
Voice Agent Platform — Sub-Tenant Routes

Sub-tenants belong to a multi-tenant account. They are SYNCED from the
customer's own API, never created by hand here — see docs/integration-guide.md.

    POST /api/v1/tenants/{id}/sub-tenants/sync           — Pull the list from their API
    POST /api/v1/tenants/{id}/sub-tenants/test-endpoint  — Dry run; writes nothing
    GET  /api/v1/tenants/{id}/sub-tenants                — List
    GET  /api/v1/tenants/{id}/sub-tenants/{sub_id}       — Get one
    PUT  /api/v1/tenants/{id}/sub-tenants/{sub_id}       — Edit display overrides only
    GET  /api/v1/tenants/{id}/sub-tenants/{sub_id}/embed-code

There is deliberately no POST-to-create and no DELETE: the customer's API owns
that list. A sub-tenant disappears by being dropped from their API, which
deactivates our row rather than deleting it, so call history survives.

All routes require JWT authentication.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.logger import get_logger
from app.middleware.auth import get_current_user
from app.models.admin_user import AdminUser
from app.models.tenant import Tenant
from app.redis_client import get_redis
from app.schemas.tenant import (
    SubTenantListResponse,
    SubTenantResponse,
    SubTenantSyncResponse,
    SubTenantUpdate,
    SyncDiagnostic,
)
from app.services.sub_tenant_service import SubTenantService, SyncError
from app.services.tenant_service import TenantService

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/tenants", tags=["Sub-Tenants"])


def _get_sub_service(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> SubTenantService:
    return SubTenantService(db=db, redis=redis)


def _get_tenant_service(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TenantService:
    return TenantService(db=db, redis=redis)


async def _require_multi_tenant(
    tenant_id: UUID,
    current_user: AdminUser,
    tenant_service: TenantService,
) -> Tenant:
    """
    Load the tenant, enforcing both access scope and tenancy mode.

    A single-tenant account has no sub-tenants at all, so every route in this
    module is a 409 for it rather than an empty list — an empty list would
    suggest "none yet", which is a different and misleading thing.
    """
    tenant = await tenant_service.get_tenant(tenant_id, current_user)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    if tenant.tenancy_type != "multi":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This account is configured as single-tenant and has no "
                "sub-tenants. Switch it to multi-tenant first."
            ),
        )
    return tenant


# ----------------------------------------------------------------
# SYNC
# ----------------------------------------------------------------

@router.post(
    "/{tenant_id}/sub-tenants/sync",
    response_model=SubTenantSyncResponse,
    summary="Sync Sub-Tenants",
    description=(
        "Read the sub-tenant list from the customer's API and reconcile it with "
        "our records. Creates new ones, updates names, and deactivates (never "
        "deletes) ones that have disappeared."
    ),
)
async def sync_sub_tenants(
    tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    service: SubTenantService = Depends(_get_sub_service),
):
    tenant = await _require_multi_tenant(tenant_id, current_user, tenant_service)

    try:
        result = await service.sync_from_api(tenant)
    except SyncError as e:
        # A failed sync is not a server fault — it is their endpoint needing
        # work, so return 200 with the diagnostic the UI renders inline.
        logger.warning(
            "sub_tenant_sync_failed",
            tenant_id=str(tenant_id),
            reason=e.reason,
            status_code=e.status_code,
        )
        return SubTenantSyncResponse(
            success=False,
            diagnostic=SyncDiagnostic(**e.as_dict()),
        )

    rows = await service.list_sub_tenants(tenant_id)
    return SubTenantSyncResponse(
        success=True,
        **result,
        sub_tenants=[SubTenantResponse.model_validate(r) for r in rows],
    )


# ----------------------------------------------------------------
# TEST ENDPOINT (dry run)
# ----------------------------------------------------------------

@router.post(
    "/{tenant_id}/sub-tenants/test-endpoint",
    response_model=SubTenantSyncResponse,
    summary="Test Listing Endpoint",
    description=(
        "Call the customer's listing endpoint and report exactly what came back, "
        "WITHOUT saving anything. Safe to run repeatedly while building."
    ),
)
async def test_listing_endpoint(
    tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    service: SubTenantService = Depends(_get_sub_service),
):
    tenant = await _require_multi_tenant(tenant_id, current_user, tenant_service)

    try:
        entries = await service.fetch_list(tenant)
    except SyncError as e:
        return SubTenantSyncResponse(
            success=False,
            diagnostic=SyncDiagnostic(**e.as_dict()),
        )

    # Report what we read without touching the database.
    return SubTenantSyncResponse(
        success=True,
        total_active=len(entries),
        diagnostic=SyncDiagnostic(
            reason="ok",
            message=(
                f"Success — we read {len(entries)} sub-tenant(s): "
                + ", ".join(e["slug"] for e in entries[:10])
                + ("..." if len(entries) > 10 else "")
            ),
        ),
    )


# ----------------------------------------------------------------
# LIST / GET
# ----------------------------------------------------------------

@router.get(
    "/{tenant_id}/sub-tenants",
    response_model=SubTenantListResponse,
    summary="List Sub-Tenants",
)
async def list_sub_tenants(
    tenant_id: UUID,
    include_inactive: bool = True,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    service: SubTenantService = Depends(_get_sub_service),
):
    await _require_multi_tenant(tenant_id, current_user, tenant_service)
    rows = await service.list_sub_tenants(tenant_id, include_inactive=include_inactive)
    return SubTenantListResponse(
        sub_tenants=[SubTenantResponse.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.get(
    "/{tenant_id}/sub-tenants/{sub_tenant_id}",
    response_model=SubTenantResponse,
    summary="Get Sub-Tenant",
)
async def get_sub_tenant(
    tenant_id: UUID,
    sub_tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    service: SubTenantService = Depends(_get_sub_service),
):
    await _require_multi_tenant(tenant_id, current_user, tenant_service)
    row = await service.get_sub_tenant(tenant_id, sub_tenant_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sub-tenant not found",
        )
    return row


# ----------------------------------------------------------------
# UPDATE (display overrides only)
# ----------------------------------------------------------------

@router.put(
    "/{tenant_id}/sub-tenants/{sub_tenant_id}",
    response_model=SubTenantResponse,
    summary="Update Sub-Tenant Overrides",
    description=(
        "Update a sub-tenant's greeting, language, tone or UPI ID. Name and slug "
        "are owned by the customer's API and cannot be edited here — the next "
        "sync would overwrite them."
    ),
)
async def update_sub_tenant(
    tenant_id: UUID,
    sub_tenant_id: UUID,
    payload: SubTenantUpdate,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    service: SubTenantService = Depends(_get_sub_service),
):
    await _require_multi_tenant(tenant_id, current_user, tenant_service)
    row = await service.update_overrides(tenant_id, sub_tenant_id, payload)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sub-tenant not found",
        )
    return row


# ----------------------------------------------------------------
# EMBED CODE
# ----------------------------------------------------------------

@router.get(
    "/{tenant_id}/sub-tenants/{sub_tenant_id}/embed-code",
    summary="Get Sub-Tenant Embed Code",
    description=(
        "Widget snippet for one sub-tenant. All sub-tenants share the account's "
        "single widget API key and are told apart by data-sub-tenant-id."
    ),
)
async def get_sub_tenant_embed_code(
    tenant_id: UUID,
    sub_tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    service: SubTenantService = Depends(_get_sub_service),
):
    tenant = await _require_multi_tenant(tenant_id, current_user, tenant_service)
    row = await service.get_sub_tenant(tenant_id, sub_tenant_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sub-tenant not found",
        )

    if not tenant.widget_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant has no widget API key. Create one first.",
        )

    settings = get_settings()
    widget_host = (
        # HOST is a bind address (0.0.0.0 = "every interface"), which no browser
        # can fetch. The snippet needs a reachable address, so dev uses localhost.
        f"http://localhost:{settings.PORT}"
        if settings.is_development
        else "https://widget.voiceagent.io"
    )
    return tenant_service.generate_embed_code(tenant, widget_host, sub_tenant=row)
