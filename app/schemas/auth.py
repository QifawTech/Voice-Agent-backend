"""
Voice Agent Platform — Auth Schemas

Pydantic models for authentication: registration, login, and JWT tokens.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


# ---- Request Schemas ----


class UserRegister(BaseModel):
    """Request body for admin user registration."""

    email: EmailStr = Field(..., examples=["admin@spicegarden.com"])
    password: str = Field(
        ..., min_length=8, max_length=128, examples=["SecurePass123!"]
    )
    full_name: str = Field(
        ..., min_length=1, max_length=255, examples=["Rahul Sharma"]
    )

    # `role` and `tenant_id` are deliberately NOT accepted here.
    #
    # This endpoint is public and unauthenticated, so anything it reads from the
    # body is attacker-controlled. Accepting `role` let anyone self-assign
    # super_admin (access to every tenant); accepting `tenant_id` let anyone
    # bind themselves to a tenant UUID they had guessed or learned, taking over
    # that customer's account. Both are now fixed server-side in
    # `app/routes/auth.py`: every signup is a tenant_admin with no tenant.
    #
    # Extra fields in the body are ignored by pydantic, so a request still
    # carrying `"role": "super_admin"` succeeds — and produces a tenant_admin.
    #
    # Super-admins are created out of band by `create_super_admin.py`.
    # A tenant_id is back-filled when the user creates their own tenant
    # (see TenantService.create_tenant).


class UserLogin(BaseModel):
    """Request body for admin user login."""

    email: EmailStr = Field(..., examples=["admin@spicegarden.com"])
    password: str = Field(..., examples=["SecurePass123!"])


class UserUpdate(BaseModel):
    """Request body for editing your own profile. Partial — omit what you keep."""

    email: EmailStr | None = Field(default=None, examples=["admin@spicegarden.com"])
    full_name: str | None = Field(
        default=None, min_length=1, max_length=255, examples=["Rahul Sharma"]
    )

    # `role`, `tenant_id` and `is_active` are deliberately NOT accepted, for the
    # same reason UserRegister refuses them: this endpoint is authenticated but
    # the body is still client-supplied, so accepting `role` would let any admin
    # promote themselves to super_admin, `tenant_id` would let them re-point
    # their account at another customer's tenant, and `is_active` would let a
    # deactivated account revive itself. Those three are administrative
    # decisions, not profile fields.


class PasswordChange(BaseModel):
    """Request body for changing your own password."""

    current_password: str = Field(..., examples=["SecurePass123!"])
    new_password: str = Field(
        ..., min_length=8, max_length=128, examples=["EvenBetterPass456!"]
    )

    # The current password is required even though the caller is already
    # authenticated: a JWT lives for hours and may be sitting in an unattended
    # browser, so re-proving the password is what stops a borrowed session from
    # locking the real owner out of their account.


# ---- Response Schemas ----


class UserResponse(BaseModel):
    """Response body for a user profile. Excludes password."""

    id: UUID
    email: str
    full_name: str
    tenant_id: UUID | None = None
    role: str
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    """JWT token response after successful login."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(
        description="Token expiry in seconds", examples=[86400]
    )
    user: UserResponse
