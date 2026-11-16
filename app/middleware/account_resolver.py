"""
Voice Agent Platform — Account Resolver Middleware

FastAPI dependency that resolves the account from incoming widget requests.
Reads `X-Tenant-ID` and `X-API-Key` headers to identify which restaurant
the request belongs to.

This middleware is used by voice/widget endpoints (NOT admin dashboard routes).

Usage:
    from app.middleware.account_resolver import get_current_account

    @router.post("/voice/message")
    async def voice_message(
        account: Account = Depends(get_current_account),
    ):
        # account is now the resolved Account object
        ...
"""

from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logger import get_logger
from app.models.account import Account
from app.redis_client import get_redis
from app.services.account_service import AccountService

logger = get_logger(__name__)


async def get_current_account(
    x_account_id: str | None = Header(default=None, alias="X-Account-ID"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> Account:
    """
    FastAPI dependency: resolve account from widget request headers.

    Validates:
        1. Both X-Account-ID and X-API-Key headers are present
        2. The API key matches a valid, active account
        3. The account ID matches the API key's account

    X-Tenant-ID is the old name for this header and is still accepted. It is
    hard-coded into integrations written before the rename, which we cannot
    reach and should not break for the sake of a tidier header name.

    Args:
        x_account_id: The account UUID from the request header.
        x_tenant_id: The same thing, under its old name.
        x_api_key: The widget API key from the request header.

    Returns:
        The resolved Account object.

    Raises:
        HTTPException 401: If headers are missing or invalid.
        HTTPException 403: If the account is inactive.
    """
    x_account_id = x_account_id or x_tenant_id

    # Validate headers are present
    if not x_account_id or not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required headers: X-Account-ID and X-API-Key",
        )

    # Validate account ID format
    try:
        tenant_uuid = UUID(x_account_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Tenant-ID format",
        )

    # Look up account by API key
    service = AccountService(db=db, redis=redis)
    account = await service.get_tenant_by_api_key(x_api_key)

    if account is None:
        logger.warning(
            "tenant_resolution_failed",
            reason="invalid_api_key",
            account_id=x_account_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # Verify the account ID matches the API key's account
    if account.id != tenant_uuid:
        logger.warning(
            "tenant_resolution_failed",
            reason="account_id_mismatch",
            header_account_id=x_account_id,
            api_key_account_id=str(account.id),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account ID does not match API key",
        )

    # Check if account is active
    if not account.is_active:
        logger.warning(
            "tenant_resolution_failed",
            reason="tenant_inactive",
            account_id=str(account.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account account is deactivated",
        )

    logger.info(
        "tenant_resolved",
        account_id=str(account.id),
        tenant_name=account.name,
        platform=account.platform,
    )

    return account
