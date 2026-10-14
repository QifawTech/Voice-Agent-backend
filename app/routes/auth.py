"""
Voice Agent Platform — Auth Routes

API endpoints for admin user authentication:
    POST /api/v1/auth/register         — Create a new admin account
    POST /api/v1/auth/login            — Login and get JWT token
    GET  /api/v1/auth/me               — Get current user profile
    PUT  /api/v1/auth/me               — Edit your own name / email
    POST /api/v1/auth/change-password  — Change your own password
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logger import get_logger
from app.middleware.auth import get_current_user
from app.models.admin_user import AdminUser
from app.schemas.auth import (
    PasswordChange,
    TokenResponse,
    UserLogin,
    UserRegister,
    UserResponse,
    UserUpdate,
)
from app.services.security import (
    create_access_token,
    hash_password,
    verify_password,
)
from app.config import get_settings

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register Admin",
    description="Create a new admin user account.",
)
async def register(
    payload: UserRegister,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new admin user.

    - Checks for duplicate email
    - Hashes the password with bcrypt
    - Always creates a tenant_admin with no tenant (see UserRegister)
    - Returns the created user profile
    """
    # Check for duplicate email
    existing = await db.execute(
        select(AdminUser).where(AdminUser.email == payload.email)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    # Create the user.
    #
    # role and tenant_id are set here, never taken from the request: this
    # endpoint is public, so a client-supplied role was a self-service path to
    # super_admin, and a client-supplied tenant_id was a path into someone
    # else's tenant. A signup always produces an unprivileged, unattached
    # account; tenant_id is back-filled when they create their own tenant, and
    # super-admins come from create_super_admin.py instead.
    user = AdminUser(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        tenant_id=None,
        role="tenant_admin",
    )

    db.add(user)
    await db.flush()  # Get the generated ID without committing
    await db.refresh(user)

    logger.info(
        "user_registered",
        user_id=str(user.id),
        email=user.email,
        role=user.role,
    )

    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login",
    description="Authenticate with email and password to receive a JWT token.",
)
async def login(
    payload: UserLogin,
    db: AsyncSession = Depends(get_db),
):
    """
    Login and get a JWT access token.

    - Validates email exists
    - Verifies password against bcrypt hash
    - Checks account is active
    - Returns JWT token + user profile
    """
    # Find user by email
    result = await db.execute(
        select(AdminUser).where(AdminUser.email == payload.email)
    )
    user = result.scalar_one_or_none()

    # Check user exists and password matches
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Check account is active
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    # Update last login time
    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()

    # Create JWT token
    settings = get_settings()
    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "email": user.email,
            "role": user.role,
            "tenant_id": str(user.tenant_id) if user.tenant_id else None,
        }
    )

    logger.info("user_logged_in", user_id=str(user.id), email=user.email)

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.JWT_EXPIRY_MINUTES * 60,  # Convert to seconds
        user=UserResponse.model_validate(user),
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get Current User",
    description="Get the profile of the currently authenticated user.",
)
async def get_me(
    current_user: AdminUser = Depends(get_current_user),
):
    """
    Get the authenticated user's profile.

    Requires a valid JWT token in the Authorization header.
    """
    return current_user


@router.put(
    "/me",
    response_model=UserResponse,
    summary="Update Current User",
    description="Edit your own full name and email address.",
)
async def update_me(
    payload: UserUpdate,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Update your own profile.

    Partial — only the fields present in the body are changed.

    Changing the email does not invalidate the current token: the JWT carries
    an email claim, but get_current_user resolves the account from the `sub`
    (user id) claim, so the stale claim is never read for authorization and the
    session stays valid. The next login issues a token with the new address.
    """
    changes = payload.model_dump(exclude_unset=True)

    # A no-op PUT is not an error — return the profile unchanged.
    if not changes:
        return current_user

    new_email = changes.get("email")
    if new_email is not None and new_email != current_user.email:
        # emails are unique in the DB; checking here turns a 500 from the
        # constraint into a 409 the UI can show next to the field.
        existing = await db.execute(
            select(AdminUser).where(
                AdminUser.email == new_email,
                AdminUser.id != current_user.id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists",
            )
        current_user.email = new_email

    new_name = changes.get("full_name")
    if new_name is not None:
        current_user.full_name = new_name

    await db.flush()
    await db.refresh(current_user)

    logger.info(
        "user_profile_updated",
        user_id=str(current_user.id),
        updated_fields=sorted(changes.keys()),
    )

    return current_user


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change Password",
    description="Change your own password. Requires the current password.",
)
async def change_password(
    payload: PasswordChange,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Change your own password.

    - Verifies the current password before accepting a new one
    - Refuses a new password identical to the current one
    - Returns 204 with no body on success

    Existing tokens keep working. Revoking them would need a server-side
    token store or a per-user token version, neither of which exists yet —
    worth adding if sessions ever need to be killed on a password change.
    """
    if not verify_password(payload.current_password, current_user.hashed_password):
        logger.warning(
            "password_change_rejected",
            reason="current_password_incorrect",
            user_id=str(current_user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    if verify_password(payload.new_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The new password must be different from the current one",
        )

    current_user.hashed_password = hash_password(payload.new_password)
    await db.flush()

    logger.info("password_changed", user_id=str(current_user.id))
