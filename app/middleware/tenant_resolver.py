"""
Voice Agent Platform — Tenant Resolver Middleware

FastAPI dependency that resolves the tenant from incoming widget requests.
Reads `X-Tenant-ID` and `X-API-Key` headers to identify which restaurant
the request belongs to.

This middleware is used by voice/widget endpoints (NOT admin dashboard routes).

Usage:
    from app.middleware.tenant_resolver import get_current_tenant

    @router.post("/voice/message")
    async def voice_message(
        tenant: Tenant = Depends(get_current_tenant),
    ):
        # tenant is now the resolved Tenant object
        ...
"""

from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logger import get_logger
from app.models.tenant import Tenant
from app.redis_client import get_redis
from app.services.tenant_service import TenantService

logger = get_logger(__name__)


async def get_current_tenant(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> Tenant:
    """
    FastAPI dependency: resolve tenant from widget request headers.

    Validates:
        1. Both X-Tenant-ID and X-API-Key headers are present
        2. The API key matches a valid, active tenant
        3. The tenant ID matches the API key's tenant

    Args:
        x_tenant_id: The tenant UUID from the request header.
        x_api_key: The widget API key from the request header.

    Returns:
        The resolved Tenant object.

    Raises:
        HTTPException 401: If headers are missing or invalid.
        HTTPException 403: If the tenant is inactive.
    """
    # Validate headers are present
    if not x_tenant_id or not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required headers: X-Tenant-ID and X-API-Key",
        )

    # Validate tenant ID format
    try:
        tenant_uuid = UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Tenant-ID format",
        )

    # Look up tenant by API key
    service = TenantService(db=db, redis=redis)
    tenant = await service.get_tenant_by_api_key(x_api_key)

    if tenant is None:
        logger.warning(
            "tenant_resolution_failed",
            reason="invalid_api_key",
            tenant_id=x_tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # Verify the tenant ID matches the API key's tenant
    if tenant.id != tenant_uuid:
        logger.warning(
            "tenant_resolution_failed",
            reason="tenant_id_mismatch",
            header_tenant_id=x_tenant_id,
            api_key_tenant_id=str(tenant.id),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tenant ID does not match API key",
        )

    # Check if tenant is active
    if not tenant.is_active:
        logger.warning(
            "tenant_resolution_failed",
            reason="tenant_inactive",
            tenant_id=str(tenant.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant account is deactivated",
        )

    logger.info(
        "tenant_resolved",
        tenant_id=str(tenant.id),
        tenant_name=tenant.name,
        platform=tenant.platform,
    )

    return tenant
