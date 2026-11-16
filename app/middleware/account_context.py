"""
Voice Agent Platform — Account Context

Resolves a widget request down to "which account, and which sub-tenant?", and
carries the display config the voice agent should actually use.

Why this exists alongside get_current_account:
    get_current_account answers "who is this account?" and is used by the rate
    limiter and the older voice routes. It returns a bare Account and must keep
    doing so. This module answers the newer question — "and which of their
    sub-tenants is this call for?" — without changing that contract.

The security gap this closes:
    Until now `restaurant_id` was an unvalidated free string from the widget.
    Any value was accepted and written straight into the logs, including one
    belonging to a different account. Every sub-tenant is now looked up and
    checked against the resolved account before it is used.

Two ways a sub-tenant arrives, because the widget is mid-migration:
    1. X-Sub-Tenant-ID header   — the new way (item 28)
    2. restaurant_id in the body — what existing widgets send

Both are validated identically. The header wins when both are present.
"""

from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logger import get_logger
from app.middleware.account_resolver import get_current_account
from app.models.sub_tenant import SubTenant
from app.models.account import Account
from app.redis_client import get_redis
from app.services.sub_tenant_service import SubTenantService
from app.services.account_service import resolve_display_config

logger = get_logger(__name__)


@dataclass
class AccountContext:
    """A resolved widget request: the account, the sub-tenant, and the config."""

    account: Account
    db: AsyncSession
    redis: Redis | None = None
    sub_tenant: SubTenant | None = None
    display_config: dict = field(default_factory=dict)

    @property
    def is_multi(self) -> bool:
        return self.account.tenancy_type == "multi"

    @property
    def restaurant_id(self) -> str | None:
        """The value to send onward to the customer's API as ?restaurantId=."""
        return self.sub_tenant.slug if self.sub_tenant else None

    @property
    def sub_tenant_id(self):
        return self.sub_tenant.id if self.sub_tenant else None

    def refresh_config(self) -> dict:
        self.display_config = resolve_display_config(self.account, self.sub_tenant)
        return self.display_config

    async def attach_sub_tenant(self, slug: str | None) -> None:
        """
        Resolve and validate a sub-tenant slug for this request.

        Called by routes that receive the slug in the body rather than a header.
        A slug already resolved from the header wins and this becomes a no-op.

        Raises 401 when the account is multi-tenant and no valid sub-tenant can
        be determined — an unvalidated slug is exactly the bug this prevents.
        """
        if self.sub_tenant is not None:
            return

        if not self.is_multi:
            # Single-tenant accounts have no sub-tenants. A stray slug is a
            # misconfigured embed, not something to silently accept.
            if slug:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=(
                        "This account is single-tenant and has no sub-tenants. "
                        "Remove data-sub-tenant-id from your embed code."
                    ),
                )
            self.refresh_config()
            return

        if not slug:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "This account is multi-tenant, so every request must identify "
                    "a sub-tenant. Add data-sub-tenant-id to your embed code."
                ),
            )

        service = SubTenantService(db=self.db, redis=self.redis)
        row = await service.get_by_slug(self.account.id, slug)

        if row is None:
            logger.warning(
                "sub_tenant_resolution_failed",
                reason="not_found_for_account",
                account_id=str(self.account.id),
                slug=slug,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Unknown sub-tenant '{slug}' for this account.",
            )

        if not row.is_active:
            logger.warning(
                "sub_tenant_resolution_failed",
                reason="inactive",
                account_id=str(self.account.id),
                slug=slug,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Sub-tenant '{slug}' is no longer active.",
            )

        self.sub_tenant = row
        self.refresh_config()

        logger.info(
            "sub_tenant_resolved",
            account_id=str(self.account.id),
            sub_tenant_id=str(row.id),
            slug=row.slug,
        )


async def get_account_context(
    x_sub_tenant_id: str | None = Header(default=None, alias="X-Sub-Tenant-ID"),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AccountContext:
    """
    FastAPI dependency: resolve account + sub-tenant for a widget request.

    The header is resolved here when present. When it is absent, resolution is
    deferred so the route can supply the slug from its body — the route must
    then call `await context.attach_sub_tenant(slug)`, which enforces the rest.
    """
    context = AccountContext(account=account, db=db, redis=redis)
    context.refresh_config()

    if x_sub_tenant_id:
        await context.attach_sub_tenant(x_sub_tenant_id.strip())

    return context
