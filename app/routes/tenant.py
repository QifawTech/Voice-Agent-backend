"""
Voice Agent Platform — Tenant Routes

API endpoints for tenant (restaurant/platform) management:
    POST   /api/v1/tenants                     — Create a new tenant
    GET    /api/v1/tenants                      — List tenants
    GET    /api/v1/tenants/{id}                 — Get tenant details
    PUT    /api/v1/tenants/{id}                 — Update tenant config
    POST   /api/v1/tenants/{id}/regenerate-key  — Generate new widget API key
    POST   /api/v1/tenants/{id}/test-connection — Test restaurant API connection
    GET    /api/v1/tenants/{id}/embed-code      — Get widget embed code

All routes require JWT authentication.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.logger import get_logger
from app.middleware.auth import get_current_user
from app.models.admin_user import AdminUser
from app.redis_client import get_redis
from app.schemas.tenant import (
    TenantCreate,
    TenantListResponse,
    TenantResponse,
    TenantUpdate,
)
from app.services.tenant_service import TenantService

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/tenants", tags=["Tenants"])


# ---- Helper: Get TenantService instance ----

def _get_service(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TenantService:
    """Create a TenantService with injected dependencies."""
    return TenantService(db=db, redis=redis)


# ----------------------------------------------------------------
# CREATE TENANT
# ----------------------------------------------------------------

@router.post(
    "",
    response_model=TenantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Tenant",
    description="Register a new restaurant/platform tenant on the Voice Agent platform.",
)
async def create_tenant(
    payload: TenantCreate,
    current_user: AdminUser = Depends(get_current_user),
    service: TenantService = Depends(_get_service),
):
    """
    Create a new tenant.

    - Refuses with 409 if this admin already has one (one account, one tenant)
    - Validates slug uniqueness
    - Generates a widget API key
    - Encrypts API auth token if provided
    - Associates the creating admin with the tenant
    """
    try:
        tenant = await service.create_tenant(payload, current_user)
        return tenant
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


# ----------------------------------------------------------------
# LIST TENANTS
# ----------------------------------------------------------------

@router.get(
    "",
    response_model=TenantListResponse,
    summary="List Tenants",
    description="List all tenants. Tenant admins see only their own tenant.",
)
async def list_tenants(
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    current_user: AdminUser = Depends(get_current_user),
    service: TenantService = Depends(_get_service),
):
    """
    List tenants with pagination.

    Super admins see all tenants. Tenant admins see only their assigned tenant.
    """
    tenants, total = await service.list_tenants(
        admin_user=current_user,
        page=page,
        page_size=page_size,
    )

    return TenantListResponse(
        tenants=[TenantResponse.model_validate(t) for t in tenants],
        total=total,
        page=page,
        page_size=page_size,
    )


# ----------------------------------------------------------------
# GET TENANT
# ----------------------------------------------------------------

@router.get(
    "/{tenant_id}",
    response_model=TenantResponse,
    summary="Get Tenant",
    description="Get details of a specific tenant.",
)
async def get_tenant(
    tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    service: TenantService = Depends(_get_service),
):
    """
    Get a single tenant by ID.

    Tenant admins can only view their own tenant.
    """
    tenant = await service.get_tenant(tenant_id, current_user)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    return tenant


# ----------------------------------------------------------------
# UPDATE TENANT
# ----------------------------------------------------------------

@router.put(
    "/{tenant_id}",
    response_model=TenantResponse,
    summary="Update Tenant",
    description="Update a tenant's configuration. Only provided fields are updated.",
)
async def update_tenant(
    tenant_id: UUID,
    payload: TenantUpdate,
    current_user: AdminUser = Depends(get_current_user),
    service: TenantService = Depends(_get_service),
):
    """
    Update a tenant's configuration.

    Partial update — only fields included in the request body are changed.

    Switching tenancy_type from "multi" to "single" is refused with 409 while
    the account still has active sub-tenants.
    """
    try:
        tenant = await service.update_tenant(tenant_id, payload, current_user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    return tenant


# ----------------------------------------------------------------
# REGENERATE API KEY
# ----------------------------------------------------------------

@router.post(
    "/{tenant_id}/regenerate-key",
    response_model=TenantResponse,
    summary="Regenerate Widget API Key",
    description="Generate a new widget API key. The old key becomes immediately invalid.",
)
async def regenerate_api_key(
    tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    service: TenantService = Depends(_get_service),
):
    """
    Regenerate the widget API key for a tenant.

    ⚠️ This invalidates the old key immediately. Any widgets using the
    old key will stop working until they're updated.
    """
    tenant = await service.regenerate_api_key(tenant_id, current_user)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )

    logger.info(
        "api_key_regenerated_via_api",
        tenant_id=str(tenant_id),
        by_user=str(current_user.id),
    )

    return tenant


# ----------------------------------------------------------------
# TEST API CONNECTION
# ----------------------------------------------------------------

@router.post(
    "/{tenant_id}/test-connection",
    summary="Test API Connection",
    description="Test if the tenant's restaurant API is reachable and responding correctly.",
)
async def test_api_connection(
    tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    service: TenantService = Depends(_get_service),
):
    """
    Test the restaurant API connection.

    Makes a GET request to {api_base_url}/voice-agent/menu and returns:
    - success: whether the API responded with 200
    - response_time_ms: round-trip time in milliseconds
    - error: error message if failed
    """
    result = await service.test_api_connection(tenant_id, current_user)
    return result


# ----------------------------------------------------------------
# GET EMBED CODE
# ----------------------------------------------------------------

@router.get(
    "/{tenant_id}/embed-code",
    summary="Get Widget Embed Code",
    description="Get the HTML embed code snippet for the voice agent widget.",
)
async def get_embed_code(
    tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    service: TenantService = Depends(_get_service),
):
    """
    Generate the widget embed code for a tenant.

    Returns an HTML snippet that restaurant owners can paste
    into their website to enable the voice ordering widget.
    """
    tenant = await service.get_tenant(tenant_id, current_user)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
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

    embed_data = service.generate_embed_code(tenant, widget_host)
    return embed_data
