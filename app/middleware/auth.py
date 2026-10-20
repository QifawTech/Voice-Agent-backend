"""
Voice Agent Platform — JWT Auth Middleware

FastAPI dependency that extracts and verifies JWT tokens from the
Authorization header. Protects routes that require authentication.

Usage:
    from app.middleware.auth import get_current_user, require_role

    @router.get("/me")
    async def me(user: AdminUser = Depends(get_current_user)):
        return user

    @router.get("/admin-only")
    async def admin_only(user: AdminUser = Depends(require_role("super_admin"))):
        return {"msg": "You are a super admin"}
"""

from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logger import get_logger
from app.models.admin_user import AdminUser
from app.services.security import verify_access_token

logger = get_logger(__name__)

# HTTPBearer extracts "Bearer <token>" from the Authorization header
security_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    db: AsyncSession = Depends(get_db),
) -> AdminUser:
    """
    FastAPI dependency: extract JWT, verify it, and return the AdminUser.

    Raises:
        HTTPException 401: If token is missing, invalid, or expired.
        HTTPException 401: If the user doesn't exist or is inactive.
    """
    token = credentials.credentials

    # Verify the JWT
    payload = verify_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extract user ID from token payload
    user_id_str = payload.get("sub")
    if user_id_str is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = UUID(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Fetch user from database
    result = await db.execute(select(AdminUser).where(AdminUser.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    logger.info("user_authenticated", user_id=str(user.id), role=user.role)
    return user


def require_role(*allowed_roles: str):
    """
    Factory that creates a dependency requiring specific roles.

    Usage:
        @router.get("/admin-only")
        async def admin_only(user = Depends(require_role("super_admin"))):
            ...

        @router.get("/any-admin")
        async def any_admin(user = Depends(require_role("super_admin", "tenant_admin"))):
            ...
    """

    async def role_checker(
        user: AdminUser = Depends(get_current_user),
    ) -> AdminUser:
        if user.role not in allowed_roles:
            logger.warning(
                "access_denied_insufficient_role",
                user_id=str(user.id),
                user_role=user.role,
                required_roles=list(allowed_roles),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of these roles: {', '.join(allowed_roles)}",
            )
        return user

    return role_checker
