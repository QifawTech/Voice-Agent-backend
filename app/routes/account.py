"""
Voice Agent Platform — Account Routes

API endpoints for account (restaurant/platform) management:
    POST   /api/v1/accounts                     — Create a new account
    GET    /api/v1/accounts                      — List accounts
    GET    /api/v1/accounts/{id}                 — Get account details
    PUT    /api/v1/accounts/{id}                 — Update account config
    POST   /api/v1/accounts/{id}/regenerate-key  — Generate new widget API key
    POST   /api/v1/accounts/{id}/test-connection — Test restaurant API connection
    GET    /api/v1/accounts/{id}/embed-code      — Get widget embed code

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
from app.schemas.account import (
    AccountCreate,
    AccountListResponse,
    AccountResponse,
    AccountUpdate,
)
from app.services.account_service import AccountService

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/accounts", tags=["Accounts"])


# ---- Helper: Get AccountService instance ----

def _get_service(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AccountService:
    """Create a AccountService with injected dependencies."""
    return AccountService(db=db, redis=redis)


# ----------------------------------------------------------------
# CREATE TENANT
# ----------------------------------------------------------------

@router.post(
    "",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Account",
    description="Register a new restaurant/platform account on the Voice Agent platform.",
)
async def create_account(
    payload: AccountCreate,
    current_user: AdminUser = Depends(get_current_user),
    service: AccountService = Depends(_get_service),
):
    """
    Create a new account.

    - Refuses with 409 if this admin already has one (one admin, one account)
    - Validates slug uniqueness
    - Generates a widget API key
    - Encrypts API auth token if provided
    - Associates the creating admin with the account
    """
    try:
        account = await service.create_account(payload, current_user)
        return account
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
    response_model=AccountListResponse,
    summary="List Accounts",
    description="List all accounts. Account admins see only their own account.",
)
async def list_accounts(
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=20, ge=1, le=100, description="Items per page"),
    current_user: AdminUser = Depends(get_current_user),
    service: AccountService = Depends(_get_service),
):
    """
    List accounts with pagination.

    Super admins see all accounts. Account admins see only their assigned account.
    """
    accounts, total = await service.list_accounts(
        admin_user=current_user,
        page=page,
        page_size=page_size,
    )

    return AccountListResponse(
        accounts=[AccountResponse.model_validate(t) for t in accounts],
        total=total,
        page=page,
        page_size=page_size,
    )


# ----------------------------------------------------------------
# GET TENANT
# ----------------------------------------------------------------

@router.get(
    "/{account_id}",
    response_model=AccountResponse,
    summary="Get Account",
    description="Get details of a specific account.",
)
async def get_account(
    account_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    service: AccountService = Depends(_get_service),
):
    """
    Get a single account by ID.

    Account admins can only view their own account.
    """
    account = await service.get_account(account_id, current_user)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )
    return account


# ----------------------------------------------------------------
# UPDATE TENANT
# ----------------------------------------------------------------

@router.put(
    "/{account_id}",
    response_model=AccountResponse,
    summary="Update Account",
    description="Update a account's configuration. Only provided fields are updated.",
)
async def update_account(
    account_id: UUID,
    payload: AccountUpdate,
    current_user: AdminUser = Depends(get_current_user),
    service: AccountService = Depends(_get_service),
):
    """
    Update a account's configuration.

    Partial update — only fields included in the request body are changed.

    Switching tenancy_type from "multi" to "single" is refused with 409 while
    the account still has active sub-tenants.
    """
    try:
        account = await service.update_account(account_id, payload, current_user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )
    return account


# ----------------------------------------------------------------
# REGENERATE API KEY
# ----------------------------------------------------------------

@router.post(
    "/{account_id}/regenerate-key",
    response_model=AccountResponse,
    summary="Regenerate Widget API Key",
    description="Generate a new widget API key. The old key becomes immediately invalid.",
)
async def regenerate_api_key(
    account_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    service: AccountService = Depends(_get_service),
):
    """
    Regenerate the widget API key for a account.

    ⚠️ This invalidates the old key immediately. Any widgets using the
    old key will stop working until they're updated.
    """
    account = await service.regenerate_api_key(account_id, current_user)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )

    logger.info(
        "api_key_regenerated_via_api",
        account_id=str(account_id),
        by_user=str(current_user.id),
    )

    return account


# ----------------------------------------------------------------
# TEST API CONNECTION
# ----------------------------------------------------------------

@router.post(
    "/{account_id}/test-connection",
    summary="Test API Connection",
    description="Test if the account's restaurant API is reachable and responding correctly.",
)
async def test_api_connection(
    account_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    service: AccountService = Depends(_get_service),
):
    """
    Test the restaurant API connection.

    Makes a GET request to {api_base_url}/voice-agent/menu and returns:
    - success: whether the API responded with 200
    - response_time_ms: round-trip time in milliseconds
    - error: error message if failed
    """
    result = await service.test_api_connection(account_id, current_user)
    return result


# ----------------------------------------------------------------
# GET EMBED CODE
# ----------------------------------------------------------------

@router.get(
    "/{account_id}/embed-code",
    summary="Get Widget Embed Code",
    description="Get the HTML embed code snippet for the voice agent widget.",
)
async def get_embed_code(
    account_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    service: AccountService = Depends(_get_service),
):
    """
    Generate the widget embed code for a account.

    Returns an HTML snippet that restaurant owners can paste
    into their website to enable the voice ordering widget.
    """
    account = await service.get_account(account_id, current_user)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )

    if not account.widget_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account has no widget API key. Create one first.",
        )

    settings = get_settings()
    widget_host = (
        # HOST is a bind address (0.0.0.0 = "every interface"), which no browser
        # can fetch. The snippet needs a reachable address, so dev uses localhost.
        f"http://localhost:{settings.PORT}"
        if settings.is_development
        else "https://widget.voiceagent.io"
    )

    embed_data = service.generate_embed_code(account, widget_host)
    return embed_data
