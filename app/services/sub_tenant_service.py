"""
Voice Agent Platform — SubTenant Service

Sub-tenants are SYNCED from the customer's own API, never typed by hand.

Why: the sub-tenant slug is sent straight back to their API as
`?restaurantId=<slug>`. A hand-typed slug that differs by one character
(spice-garden vs spice_garden) looks perfectly fine in our dashboard while
every real call fails silently with "I couldn't find the menu". Reading the
list from the source removes that entire class of bug.

The contract their API must implement is in docs/integration-guide.md:

    GET {api_base_url}/voice-agent/restaurants
    Authorization: Bearer <api_auth_token>
    -> [{"id": "spice-garden", "name": "Spice Garden"}, ...]

Usage:
    service = SubTenantService(db, redis)
    result = await service.sync_from_api(tenant)
"""

from datetime import datetime, timezone
from uuid import UUID

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logger import get_logger
from app.models.sub_tenant import SubTenant
from app.models.tenant import Tenant
from app.schemas.tenant import SubTenantUpdate
from app.services.security import decrypt_token, looks_encrypted

logger = get_logger(__name__)

# The path we append to the tenant's api_base_url.
LIST_ENDPOINT_PATH = "/voice-agent/restaurants"

# How long we wait before giving up on their API.
SYNC_TIMEOUT_SECONDS = 10.0

# How much of their response we echo back in a diagnostic. Enough to spot a
# shape mismatch, small enough not to dump their whole database into a UI.
RAW_RESPONSE_CHARS = 200


class SyncError(Exception):
    """
    A sync failed for a reason the customer's developer can act on.

    Carries the structured diagnostic rather than a bare message, because
    "Sync failed" tells a developer nothing — they need the URL we called and
    what came back.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        url_called: str | None = None,
        status_code: int | None = None,
        raw_response: str | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.url_called = url_called
        self.status_code = status_code
        self.raw_response = raw_response

    def as_dict(self) -> dict:
        return {
            "reason": self.reason,
            "message": self.message,
            "url_called": self.url_called,
            "status_code": self.status_code,
            "raw_response": self.raw_response,
        }


class SubTenantService:
    """Sync and manage the sub-tenants of a multi-tenant account."""

    def __init__(self, db: AsyncSession, redis: Redis | None = None):
        self.db = db
        self.redis = redis

    # ----------------------------------------------------------------
    # FETCH — talk to the customer's API
    # ----------------------------------------------------------------

    def _build_list_url(self, tenant: Tenant) -> str:
        if not tenant.api_base_url:
            raise SyncError(
                reason="not_configured",
                message=(
                    "No API base URL is set for this account. Add your API URL "
                    "before syncing sub-tenants."
                ),
            )
        return f"{tenant.api_base_url.rstrip('/')}{LIST_ENDPOINT_PATH}"

    def _build_headers(self, tenant: Tenant, url: str) -> dict:
        headers = {"Content-Type": "application/json"}

        if not tenant.api_auth_token:
            return headers

        # A token that was never encrypted and a token we cannot decrypt are
        # different problems with different fixes, so they get different errors.
        if not looks_encrypted(tenant.api_auth_token):
            raise SyncError(
                reason="not_configured",
                message=(
                    "The stored API token is not encrypted, so it cannot be used. "
                    "Re-save this tenant's API token to store it correctly."
                ),
                url_called=url,
            )

        try:
            headers["Authorization"] = f"Bearer {decrypt_token(tenant.api_auth_token)}"
        except ValueError:
            raise SyncError(
                reason="not_configured",
                message=(
                    "Could not decrypt the stored API token. This usually means "
                    "ENCRYPTION_KEY changed since the token was saved. Re-save the "
                    "tenant's API token, or restore the previous ENCRYPTION_KEY."
                ),
                url_called=url,
            )

        return headers

    async def fetch_list(self, tenant: Tenant) -> list[dict]:
        """
        Call the customer's listing endpoint and return validated [{id, name}].

        Raises SyncError with a developer-actionable diagnostic on any failure.
        Writes nothing — this is also what the "Test my endpoint" button runs.
        """
        url = self._build_list_url(tenant)
        headers = self._build_headers(tenant, url)

        try:
            async with httpx.AsyncClient(timeout=SYNC_TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            raise SyncError(
                reason="timeout",
                message=(
                    f"No response within {int(SYNC_TIMEOUT_SECONDS)} seconds. "
                    "Check the endpoint returns quickly even with many restaurants."
                ),
                url_called=url,
            )
        except httpx.ConnectError:
            raise SyncError(
                reason="unreachable",
                message=(
                    "We could not reach your server at this address. Check the API "
                    "base URL is correct and publicly reachable."
                ),
                url_called=url,
            )
        except httpx.HTTPError as e:
            raise SyncError(
                reason="unreachable",
                message=f"Could not complete the request: {e}",
                url_called=url,
            )

        return self._parse_response(response, url)

    @staticmethod
    def _parse_response(response: httpx.Response, url: str) -> list[dict]:
        """Turn their HTTP response into [{id, name}] or a precise SyncError."""
        snippet = response.text[:RAW_RESPONSE_CHARS] if response.text else ""

        if response.status_code == 404:
            raise SyncError(
                reason="not_found",
                message=(
                    f"No endpoint at GET {url}. We called exactly this URL — check "
                    "the path is correct and deployed."
                ),
                url_called=url,
                status_code=404,
                raw_response=snippet,
            )

        if response.status_code in (401, 403):
            raise SyncError(
                reason="unauthorized",
                message=(
                    "Your API rejected our token. Check this endpoint accepts the "
                    "same Authorization: Bearer header as /voice-agent/menu."
                ),
                url_called=url,
                status_code=response.status_code,
                raw_response=snippet,
            )

        if response.status_code >= 400:
            raise SyncError(
                reason="server_error",
                message=f"Your API returned status {response.status_code}.",
                url_called=url,
                status_code=response.status_code,
                raw_response=snippet,
            )

        bad_shape = SyncError(
            reason="bad_shape",
            message=(
                "We reached your API but could not read the list. We expect a bare "
                'JSON array like [{"id": "spice-garden", "name": "Spice Garden"}] — '
                'not an object wrapping it, and the field must be "id", not '
                '"restaurant_id".'
            ),
            url_called=url,
            status_code=response.status_code,
            raw_response=snippet,
        )

        try:
            payload = response.json()
        except ValueError:
            raise bad_shape

        if not isinstance(payload, list):
            raise bad_shape

        if not payload:
            raise SyncError(
                reason="empty",
                message=(
                    "Your API returned zero sub-tenants. Is the list filtered by the "
                    "token we are using, or are all your restaurants inactive?"
                ),
                url_called=url,
                status_code=response.status_code,
                raw_response=snippet,
            )

        entries: list[dict] = []
        for raw in payload:
            if not isinstance(raw, dict):
                raise bad_shape
            slug = raw.get("id")
            if slug is None or str(slug).strip() == "":
                raise bad_shape
            slug = str(slug).strip()
            # Name is nice-to-have; fall back to the slug rather than failing,
            # since a missing label should not block a working integration.
            name = str(raw.get("name") or slug).strip()
            entries.append({"slug": slug, "name": name})

        # Their API returning the same id twice would make the upsert ambiguous.
        seen = [e["slug"] for e in entries]
        duplicates = {s for s in seen if seen.count(s) > 1}
        if duplicates:
            raise SyncError(
                reason="bad_shape",
                message=(
                    "Your API returned the same id more than once: "
                    f"{', '.join(sorted(duplicates))}. Each id must be unique."
                ),
                url_called=url,
                status_code=response.status_code,
                raw_response=snippet,
            )

        return entries

    # ----------------------------------------------------------------
    # SYNC — reconcile their list with our rows
    # ----------------------------------------------------------------

    async def sync_from_api(self, tenant: Tenant) -> dict:
        """
        Fetch their list and reconcile it with our sub_tenants rows.

            in their API, not ours   -> create
            in both                  -> update the name, keep our overrides
            ours, gone from theirs   -> DEACTIVATE, never delete

        Deactivating matters: conversation_logs and order_events point at these
        rows, so deleting one would destroy call history.

        Returns {created, updated, deactivated, total_active}.
        Raises SyncError if their endpoint could not be read.
        """
        if tenant.tenancy_type != "multi":
            raise SyncError(
                reason="not_configured",
                message=(
                    "This account is set up as single-tenant, so it has no "
                    "sub-tenants to sync."
                ),
            )

        entries = await self.fetch_list(tenant)
        incoming = {e["slug"]: e["name"] for e in entries}

        existing_rows = (
            await self.db.execute(
                select(SubTenant).where(SubTenant.tenant_id == tenant.id)
            )
        ).scalars().all()
        existing = {row.slug: row for row in existing_rows}

        now = datetime.now(timezone.utc)
        created = updated = deactivated = 0

        for slug, name in incoming.items():
            row = existing.get(slug)
            if row is None:
                self.db.add(
                    SubTenant(
                        tenant_id=tenant.id,
                        name=name,
                        slug=slug,
                        is_active=True,
                        last_synced_at=now,
                    )
                )
                created += 1
            else:
                # Their API owns name; our overrides are left untouched.
                # Re-activate a row that came back after disappearing.
                if row.name != name or not row.is_active:
                    row.name = name
                    row.is_active = True
                    updated += 1
                row.last_synced_at = now

        for slug, row in existing.items():
            if slug not in incoming and row.is_active:
                row.is_active = False
                row.last_synced_at = now
                deactivated += 1

        await self.db.flush()
        await self._invalidate_cache(tenant.id)

        logger.info(
            "sub_tenants_synced",
            tenant_id=str(tenant.id),
            created=created,
            updated=updated,
            deactivated=deactivated,
            total_from_api=len(incoming),
        )

        return {
            "created": created,
            "updated": updated,
            "deactivated": deactivated,
            "total_active": len(incoming),
        }

    # ----------------------------------------------------------------
    # READ
    # ----------------------------------------------------------------

    async def list_sub_tenants(
        self, tenant_id: UUID, include_inactive: bool = True
    ) -> list[SubTenant]:
        query = select(SubTenant).where(SubTenant.tenant_id == tenant_id)
        if not include_inactive:
            query = query.where(SubTenant.is_active.is_(True))
        result = await self.db.execute(query.order_by(SubTenant.name))
        return list(result.scalars().all())

    async def get_sub_tenant(
        self, tenant_id: UUID, sub_tenant_id: UUID
    ) -> SubTenant | None:
        result = await self.db.execute(
            select(SubTenant).where(
                SubTenant.id == sub_tenant_id,
                SubTenant.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, tenant_id: UUID, slug: str) -> SubTenant | None:
        """Used by the request resolver to validate an incoming sub-tenant."""
        result = await self.db.execute(
            select(SubTenant).where(
                SubTenant.tenant_id == tenant_id,
                SubTenant.slug == slug,
            )
        )
        return result.scalar_one_or_none()

    # ----------------------------------------------------------------
    # UPDATE — display overrides only
    # ----------------------------------------------------------------

    async def update_overrides(
        self,
        tenant_id: UUID,
        sub_tenant_id: UUID,
        payload: SubTenantUpdate,
    ) -> SubTenant | None:
        """
        Update a sub-tenant's display overrides.

        name and slug are deliberately absent from SubTenantUpdate — they are
        owned by the customer's API and would be overwritten on the next sync.
        """
        row = await self.get_sub_tenant(tenant_id, sub_tenant_id)
        if row is None:
            return None

        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(row, field, value)

        await self.db.flush()
        await self.db.refresh(row)
        await self._invalidate_cache(tenant_id)

        logger.info(
            "sub_tenant_updated",
            tenant_id=str(tenant_id),
            sub_tenant_id=str(sub_tenant_id),
            fields=list(payload.model_dump(exclude_unset=True).keys()),
        )
        return row

    # ----------------------------------------------------------------
    # CACHE
    # ----------------------------------------------------------------

    async def _invalidate_cache(self, tenant_id: UUID) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.delete(f"sub_tenants:{tenant_id}")
        except Exception as e:
            logger.warning("sub_tenant_cache_invalidate_error", error=str(e))
