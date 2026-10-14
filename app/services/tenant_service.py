"""
Voice Agent Platform — Tenant Service

Business logic for tenant management:
- CRUD operations (create, read, update, list)
- Widget API key generation
- Restaurant API connection testing
- Widget embed code generation
- Redis caching for tenant config (5-min TTL)

Usage:
    from app.services.tenant_service import TenantService

    service = TenantService(db, redis)
    tenant = await service.create_tenant(payload, admin_user)
"""

import json
from uuid import UUID

import httpx
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logger import get_logger
from app.models.admin_user import AdminUser
from app.models.sub_tenant import SubTenant
from app.models.tenant import Tenant
from app.schemas.tenant import TenantCreate, TenantUpdate
from app.services.security import encrypt_token, generate_api_key

logger = get_logger(__name__)

# Redis key patterns
TENANT_CACHE_PREFIX = "tenant_config:"
TENANT_CACHE_TTL = 300  # 5 minutes


class TenantService:
    """
    Service layer for tenant management operations.

    Encapsulates all tenant business logic, keeping routes thin.
    """

    def __init__(self, db: AsyncSession, redis: Redis | None = None):
        self.db = db
        self.redis = redis

    # ----------------------------------------------------------------
    # CREATE
    # ----------------------------------------------------------------

    async def create_tenant(
        self, payload: TenantCreate, admin_user: AdminUser
    ) -> Tenant:
        """
        Create a new tenant and associate it with the admin user.

        Steps:
            1. Reject the call if this admin already has a tenant
            2. Check slug uniqueness
            3. Encrypt api_auth_token if provided
            4. Generate widget API key
            5. Create tenant record
            6. Associate admin user with the tenant
            7. Seed the built-in restaurant tools

        Args:
            payload: Validated tenant creation data.
            admin_user: The authenticated admin creating this tenant.

        Returns:
            The created Tenant instance.

        Raises:
            ValueError: If the admin already has a tenant, or the slug is taken.
        """
        # One account, one tenant. A multi-tenant customer's businesses are
        # sub-tenants, and those are only ever created by syncing the customer's
        # own API (SubTenantService.sync_from_api) — never by hand. So an admin
        # who already has a tenant has no legitimate reason to create a second
        # one: wanting more businesses means switching tenancy_type, not adding
        # accounts. Enforced here rather than in the UI because this endpoint is
        # callable directly.
        #
        # Super-admins are exempt: they create tenants on behalf of customers,
        # so the limit is one tenant per customer admin, not one per person.
        if admin_user.role != "super_admin" and admin_user.tenant_id is not None:
            logger.warning(
                "tenant_create_rejected",
                reason="admin_already_has_tenant",
                admin_user_id=str(admin_user.id),
                existing_tenant_id=str(admin_user.tenant_id),
            )
            raise ValueError(
                "Your account already has a product. To manage multiple "
                "businesses, switch to Multi Tenant in Settings."
            )

        # Check slug uniqueness
        existing = await self.db.execute(
            select(Tenant).where(Tenant.slug == payload.slug)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(f"Slug '{payload.slug}' is already taken")

        # Encrypt the API auth token if provided
        encrypted_token = None
        if payload.api_auth_token:
            encrypted_token = encrypt_token(payload.api_auth_token)

        # Generate a widget API key
        widget_key = generate_api_key(prefix="sk_live")

        # Create the tenant
        tenant = Tenant(
            name=payload.name,
            slug=payload.slug,
            platform=payload.platform,
            tenancy_type=payload.tenancy_type,
            api_base_url=payload.api_base_url,
            api_auth_token=encrypted_token,
            widget_api_key=widget_key,
            upi_id=payload.upi_id,
            voice_config=payload.voice_config or {
                "greeting": "Welcome! How can I help you today?",
                "language": "en",
                "tone": "friendly",
            },
        )

        self.db.add(tenant)
        await self.db.flush()
        await self.db.refresh(tenant)

        # Associate the admin user with this tenant (if not already assigned).
        # Never do this for a super-admin: tenant_id IS NULL is exactly what
        # marks them as having access to all tenants, so back-filling it would
        # silently demote them to an admin of whichever tenant they happened to
        # create first.
        if admin_user.role != "super_admin" and admin_user.tenant_id is None:
            admin_user.tenant_id = tenant.id
            await self.db.flush()

        # Seed the built-in restaurant tools so a new tenant's agent can take
        # an order the moment its API is connected, with the tools visible and
        # editable in the dashboard rather than buried in Python.
        # Folded into this transaction: a tenant and its tools appear together.
        from app.services.tool_registry import ToolRegistry

        await ToolRegistry(db=self.db, redis=self.redis).seed_presets(
            tenant.id, commit=False
        )

        logger.info(
            "tenant_created",
            tenant_id=str(tenant.id),
            name=tenant.name,
            slug=tenant.slug,
            platform=tenant.platform,
            created_by=str(admin_user.id),
        )

        return tenant

    # ----------------------------------------------------------------
    # READ
    # ----------------------------------------------------------------

    async def get_tenant(self, tenant_id: UUID, admin_user: AdminUser) -> Tenant | None:
        """
        Get a single tenant by ID.

        Tenant admins can only access their own tenant, and an admin with no
        tenant yet can access none.
        Super admins can access any tenant.
        """
        # tenant_id IS NULL means two different things, and only the role tells
        # them apart: for a super_admin it means "every tenant", for a
        # tenant_admin it means "no tenant yet" — a fresh signup, before they
        # create their own. So branch on the role first and never let a NULL
        # tenant_id skip the scoping clause: this method is the authorization
        # gate for update, regenerate-key, test-connection, embed-code, tools
        # and sub-tenants, so an unscoped query here hands a brand-new account
        # write access to every other customer's tenant.
        if admin_user.role != "super_admin":
            if admin_user.tenant_id is None:
                return None
            if admin_user.tenant_id != tenant_id:
                return None

        result = await self.db.execute(select(Tenant).where(Tenant.id == tenant_id))
        return result.scalar_one_or_none()

    async def list_tenants(
        self,
        admin_user: AdminUser,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Tenant], int]:
        """
        List tenants with pagination.

        Tenant admins see only their own tenant, and an admin with no tenant
        yet sees an empty list.
        Super admins see all tenants.

        Returns:
            Tuple of (tenants list, total count).
        """
        # See get_tenant for why the role has to be checked before tenant_id:
        # a tenant_admin whose tenant_id is NULL has no tenant yet, and must
        # see nothing rather than falling through to an unscoped query. The
        # dashboard also treats a non-empty list as "you already have a
        # product" and hides the create button, so leaking rows here locks a
        # new account out of creating its own tenant.
        if admin_user.role != "super_admin" and admin_user.tenant_id is None:
            return [], 0

        base_query = select(Tenant)
        count_query = select(func.count(Tenant.id))

        if admin_user.role != "super_admin":
            base_query = base_query.where(Tenant.id == admin_user.tenant_id)
            count_query = count_query.where(Tenant.id == admin_user.tenant_id)

        # Get total count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # Paginate
        offset = (page - 1) * page_size
        query = (
            base_query
            .order_by(Tenant.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        result = await self.db.execute(query)
        tenants = list(result.scalars().all())

        return tenants, total

    # ----------------------------------------------------------------
    # UPDATE
    # ----------------------------------------------------------------

    async def update_tenant(
        self,
        tenant_id: UUID,
        payload: TenantUpdate,
        admin_user: AdminUser,
    ) -> Tenant | None:
        """
        Update a tenant's configuration.

        Only updates fields that are explicitly provided (not None).
        Encrypts api_auth_token if it's being updated.
        Invalidates Redis cache after update.
        """
        tenant = await self.get_tenant(tenant_id, admin_user)
        if tenant is None:
            return None

        # Apply updates (only non-None fields)
        update_data = payload.model_dump(exclude_unset=True)

        # Guard the tenancy mode switch.
        #
        # single -> multi is safe: the sub-tenant list is empty and syncing
        # fills it. multi -> single is not. Existing sub-tenants own
        # conversation_logs and order_events history, and the moment the mode
        # flips, TenantContext.attach_sub_tenant() starts rejecting every
        # widget request that sends a slug — so a live integration breaks and
        # its history is orphaned. Require the sub-tenants to be deactivated
        # first, which makes the consequence explicit.
        if (
            update_data.get("tenancy_type") == "single"
            and tenant.tenancy_type == "multi"
        ):
            result = await self.db.execute(
                select(func.count())
                .select_from(SubTenant)
                .where(
                    SubTenant.tenant_id == tenant_id,
                    SubTenant.is_active.is_(True),
                )
            )
            active_sub_tenants = result.scalar_one()
            if active_sub_tenants:
                logger.warning(
                    "tenancy_switch_rejected",
                    tenant_id=str(tenant_id),
                    active_sub_tenants=active_sub_tenants,
                    attempted_by=str(admin_user.id),
                )
                raise ValueError(
                    f"This account still has {active_sub_tenants} active "
                    "sub-tenant(s). Switching to Single Tenant would stop "
                    "their widgets from working. Deactivate them first."
                )

        for field, value in update_data.items():
            if field == "api_auth_token" and value is not None:
                # Encrypt the new API token
                value = encrypt_token(value)
            setattr(tenant, field, value)

        await self.db.flush()
        await self.db.refresh(tenant)

        # Invalidate Redis cache
        await self._invalidate_cache(tenant_id)

        logger.info(
            "tenant_updated",
            tenant_id=str(tenant_id),
            updated_fields=list(update_data.keys()),
            updated_by=str(admin_user.id),
        )

        return tenant

    # ----------------------------------------------------------------
    # WIDGET API KEY
    # ----------------------------------------------------------------

    async def regenerate_api_key(
        self, tenant_id: UUID, admin_user: AdminUser
    ) -> Tenant | None:
        """
        Generate a new widget API key for a tenant.

        Replaces the existing key. The old key becomes immediately invalid.
        """
        tenant = await self.get_tenant(tenant_id, admin_user)
        if tenant is None:
            return None

        old_key_prefix = tenant.widget_api_key[:15] if tenant.widget_api_key else "none"
        tenant.widget_api_key = generate_api_key(prefix="sk_live")

        await self.db.flush()
        await self.db.refresh(tenant)

        # Invalidate cache (old key is no longer valid)
        await self._invalidate_cache(tenant_id)

        logger.info(
            "api_key_regenerated",
            tenant_id=str(tenant_id),
            old_key_prefix=old_key_prefix,
            regenerated_by=str(admin_user.id),
        )

        return tenant

    # ----------------------------------------------------------------
    # TEST API CONNECTION
    # ----------------------------------------------------------------

    async def test_api_connection(
        self, tenant_id: UUID, admin_user: AdminUser
    ) -> dict:
        """
        Test if the tenant's restaurant API is reachable.

        Makes a GET request to {api_base_url}/voice-agent/menu to verify:
        1. The URL is reachable
        2. The auth token works
        3. The response has the expected format

        Returns:
            Dict with status, response_time_ms, and details.
        """
        tenant = await self.get_tenant(tenant_id, admin_user)
        if tenant is None:
            return {
                "success": False,
                "error": "Tenant not found",
                "response_time_ms": 0,
            }

        if not tenant.api_base_url:
            return {
                "success": False,
                "error": "No API base URL configured for this tenant",
                "response_time_ms": 0,
            }

        # Build the test request
        # /restaurants, not /menu: a menu request must name a restaurant
        # (?restaurantId=<slug>), and before the first sync we know no slugs.
        # /restaurants needs no parameters and is the endpoint sync itself
        # calls, so a green test here means sync will work.
        test_url = f"{tenant.api_base_url.rstrip('/')}/voice-agent/restaurants"
        headers = {"Content-Type": "application/json"}

        # Add auth token if available
        if tenant.api_auth_token:
            from app.services.security import decrypt_token, looks_encrypted

            # A value that was never encrypted (hand-seeded row, or written by
            # a path that bypassed encrypt_token) and a value we cannot open
            # (rotated/mismatched ENCRYPTION_KEY) are different problems with
            # different fixes, so report them differently.
            if not looks_encrypted(tenant.api_auth_token):
                logger.error(
                    "api_token_not_encrypted",
                    tenant_id=str(tenant_id),
                )
                return {
                    "success": False,
                    "error": (
                        "The stored API token is not encrypted, so it cannot be used. "
                        "Re-save this tenant's API token to store it correctly."
                    ),
                    "response_time_ms": 0,
                }

            try:
                decrypted_token = decrypt_token(tenant.api_auth_token)
                headers["Authorization"] = f"Bearer {decrypted_token}"
            except ValueError:
                logger.error(
                    "api_token_decrypt_failed",
                    tenant_id=str(tenant_id),
                )
                return {
                    "success": False,
                    "error": (
                        "Could not decrypt the stored API token. This usually means "
                        "ENCRYPTION_KEY changed since the token was saved. Re-save the "
                        "tenant's API token, or restore the previous ENCRYPTION_KEY."
                    ),
                    "response_time_ms": 0,
                }

        # Make the test request
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(test_url, headers=headers)

            response_time_ms = int(response.elapsed.total_seconds() * 1000)

            if response.status_code == 200:
                # Update tenant's api_validated status
                tenant.api_validated = True
                await self.db.flush()

                logger.info(
                    "api_connection_test_passed",
                    tenant_id=str(tenant_id),
                    url=test_url,
                    status_code=response.status_code,
                    response_time_ms=response_time_ms,
                )

                return {
                    "success": True,
                    "status_code": response.status_code,
                    "response_time_ms": response_time_ms,
                    "message": "API connection successful",
                }
            else:
                logger.warning(
                    "api_connection_test_failed",
                    tenant_id=str(tenant_id),
                    url=test_url,
                    status_code=response.status_code,
                )

                return {
                    "success": False,
                    "status_code": response.status_code,
                    "response_time_ms": response_time_ms,
                    "error": f"API returned status {response.status_code}",
                }

        except httpx.TimeoutException:
            logger.warning(
                "api_connection_test_timeout",
                tenant_id=str(tenant_id),
                url=test_url,
            )
            return {
                "success": False,
                "error": "Connection timed out (10s limit)",
                "response_time_ms": 10000,
            }
        except httpx.ConnectError:
            return {
                "success": False,
                "error": f"Cannot connect to {test_url}",
                "response_time_ms": 0,
            }
        except Exception as e:
            logger.error(
                "api_connection_test_error",
                tenant_id=str(tenant_id),
                error=str(e),
            )
            return {
                "success": False,
                "error": f"Connection error: {str(e)}",
                "response_time_ms": 0,
            }

    # ----------------------------------------------------------------
    # EMBED CODE GENERATOR
    # ----------------------------------------------------------------

    def generate_embed_code(
        self, tenant: Tenant, widget_host: str, sub_tenant=None
    ) -> dict:
        """
        Generate the widget embed code snippet.

        Multi-tenant accounts get one snippet per sub-tenant. They all carry the
        SAME tenant id and api key — the sub-tenant is identified by the extra
        data-sub-tenant-id attribute, which the widget sends as X-Sub-Tenant-ID.

        Args:
            tenant: The parent tenant (owns the API key).
            widget_host: The base URL where the widget JS is hosted.
            sub_tenant: Optional SubTenant for a multi-tenant account.

        Returns:
            Dict with embed_code (HTML snippet) and configuration details.
        """
        display_name = sub_tenant.name if sub_tenant is not None else tenant.name
        sub_line = (
            f'  data-sub-tenant-id="{sub_tenant.slug}"\n'
            if sub_tenant is not None
            else ""
        )

        embed_code = (
            f'<!-- Voice Agent Widget -->\n'
            f'<script\n'
            f'  src="{widget_host}/widget.js"\n'
            f'  data-tenant-id="{tenant.id}"\n'
            f'  data-api-key="{tenant.widget_api_key}"\n'
            f'{sub_line}'
            f'  data-restaurant-name="{display_name}"\n'
            f'  data-theme="dark"\n'
            f'  async\n'
            f'></script>'
        )

        return {
            "embed_code": embed_code,
            "tenant_id": str(tenant.id),
            "sub_tenant_id": str(sub_tenant.id) if sub_tenant is not None else None,
            "sub_tenant_slug": sub_tenant.slug if sub_tenant is not None else None,
            "api_key": tenant.widget_api_key,
            "widget_url": f"{widget_host}/widget.js",
            "instructions": (
                "Copy and paste the embed code above into your website's HTML, "
                "just before the closing </body> tag."
            ),
        }

    # ----------------------------------------------------------------
    # REDIS CACHE HELPERS
    # ----------------------------------------------------------------

    async def get_cached_tenant_config(self, tenant_id: str) -> dict | None:
        """
        Get tenant config from Redis cache.

        Returns None if not cached or Redis is unavailable.
        """
        if self.redis is None:
            return None

        try:
            cache_key = f"{TENANT_CACHE_PREFIX}{tenant_id}"
            cached = await self.redis.get(cache_key)
            if cached:
                logger.debug("tenant_config_cache_hit", tenant_id=tenant_id)
                return json.loads(cached)
            return None
        except Exception as e:
            logger.warning("tenant_cache_read_error", error=str(e))
            return None

    async def cache_tenant_config(self, tenant: Tenant) -> None:
        """
        Cache tenant config in Redis with 5-minute TTL.
        """
        if self.redis is None:
            return

        try:
            cache_key = f"{TENANT_CACHE_PREFIX}{tenant.id}"
            config = {
                "id": str(tenant.id),
                "name": tenant.name,
                "slug": tenant.slug,
                "platform": tenant.platform,
                "api_base_url": tenant.api_base_url,
                "api_auth_token": tenant.api_auth_token,
                "api_validated": tenant.api_validated,
                "widget_api_key": tenant.widget_api_key,
                "upi_id": tenant.upi_id,
                "voice_config": tenant.voice_config,
                "is_active": tenant.is_active,
            }
            await self.redis.set(
                cache_key,
                json.dumps(config),
                ex=TENANT_CACHE_TTL,
            )
            logger.debug("tenant_config_cached", tenant_id=str(tenant.id))
        except Exception as e:
            logger.warning("tenant_cache_write_error", error=str(e))

    async def _invalidate_cache(self, tenant_id: UUID) -> None:
        """Invalidate a tenant's Redis cache entry."""
        if self.redis is None:
            return

        try:
            cache_key = f"{TENANT_CACHE_PREFIX}{tenant_id}"
            await self.redis.delete(cache_key)
            logger.debug("tenant_config_cache_invalidated", tenant_id=str(tenant_id))
        except Exception as e:
            logger.warning("tenant_cache_invalidate_error", error=str(e))

    # ----------------------------------------------------------------
    # TENANT LOOKUP BY API KEY (for middleware)
    # ----------------------------------------------------------------

    async def get_tenant_by_api_key(self, api_key: str) -> Tenant | None:
        """
        Look up a tenant by its widget API key.

        Used by the tenant resolver middleware.
        First checks Redis cache, then falls back to database.
        """
        # Try cache first (keyed by API key for fast widget lookups)
        if self.redis:
            try:
                cached_tenant_id = await self.redis.get(f"apikey:{api_key}")
                if cached_tenant_id:
                    # Get full config from tenant cache
                    config = await self.get_cached_tenant_config(cached_tenant_id)
                    if config:
                        # Fetch the actual tenant from DB for a real object
                        result = await self.db.execute(
                            select(Tenant).where(Tenant.id == UUID(cached_tenant_id))
                        )
                        tenant = result.scalar_one_or_none()
                        if tenant:
                            return tenant
            except Exception as e:
                logger.warning("apikey_cache_lookup_error", error=str(e))

        # Database lookup
        result = await self.db.execute(
            select(Tenant).where(
                Tenant.widget_api_key == api_key,
                Tenant.is_active == True,
            )
        )
        tenant = result.scalar_one_or_none()

        # Cache the API key → tenant_id mapping
        if tenant and self.redis:
            try:
                await self.redis.set(
                    f"apikey:{api_key}",
                    str(tenant.id),
                    ex=TENANT_CACHE_TTL,
                )
                await self.cache_tenant_config(tenant)
            except Exception as e:
                logger.warning("apikey_cache_set_error", error=str(e))

        return tenant


# --------------------------------------------------------------------
# DISPLAY CONFIG RESOLUTION
# --------------------------------------------------------------------


def resolve_display_config(tenant, sub_tenant=None) -> dict:
    """
    Merge a sub-tenant's display overrides onto its parent tenant's settings.

    The rule: the sub-tenant's value wins; if it is empty, inherit the parent's.

    This covers ONLY what is shown or spoken — voice_config and upi_id. The API
    connection (api_base_url, api_auth_token) is never resolved here because it
    is never overridden: there is exactly one API per account, and the
    sub-tenant is selected by passing its slug to that API.

    Args:
        tenant: The parent Tenant (model or dict).
        sub_tenant: The resolved SubTenant, or None for single-tenant accounts.

    Returns:
        Dict with name, slug, upi_id, voice_config, and the API connection
        taken from the parent — ready to hand to the prompt builder.
    """

    def field(obj, attr, default=None):
        if obj is None:
            return default
        if isinstance(obj, dict):
            value = obj.get(attr, default)
        else:
            value = getattr(obj, attr, default)
        return default if value is None else value

    # Parent values are the baseline.
    resolved = {
        "name": field(tenant, "name"),
        "slug": field(tenant, "slug"),
        "platform": field(tenant, "platform", "standalone"),
        "upi_id": field(tenant, "upi_id"),
        "voice_config": field(tenant, "voice_config", {}) or {},
        # Always the parent's — see the docstring.
        "api_base_url": field(tenant, "api_base_url"),
        "api_auth_token": field(tenant, "api_auth_token"),
        "restaurant_id": None,
        "sub_tenant_id": None,
    }

    if sub_tenant is None:
        return resolved

    # The sub-tenant's identity replaces the parent's for anything user-facing.
    resolved["name"] = field(sub_tenant, "name", resolved["name"])
    resolved["slug"] = field(sub_tenant, "slug", resolved["slug"])
    resolved["restaurant_id"] = field(sub_tenant, "slug")
    sub_id = field(sub_tenant, "id")
    resolved["sub_tenant_id"] = str(sub_id) if sub_id else None

    # Overrides: only replace when actually set.
    sub_upi = field(sub_tenant, "upi_id")
    if sub_upi:
        resolved["upi_id"] = sub_upi

    sub_voice = field(sub_tenant, "voice_config")
    if sub_voice:
        # Merge key-by-key so a sub-tenant that overrides only the greeting
        # still inherits the parent's language and tone.
        resolved["voice_config"] = {**resolved["voice_config"], **sub_voice}

    return resolved
