"""
Voice Agent Platform — Tool Registry

The one place that reads and writes a tenant's function tools. Everything that
needs to know what the agent can do — the DeepSeek loop, the LiveKit worker,
the dashboard — comes through here.

    load_specs()     what the agent may call right now (cached, hot path)
    list_tools()     the rows behind it, for the dashboard
    create/update/delete/seed_presets/reset_to_preset

Caching mirrors TenantService: a short Redis TTL plus an explicit invalidation
on every write, so a tool enabled in the dashboard takes effect on the next
call rather than five minutes later.

Two deliberate behaviours:

  * A tenant with no tool rows at all falls back to the built-in presets.
    That covers tenants created before this table existed and any seeding that
    failed, and it means a missing row can never leave an agent unable to take
    an order.

  * A tenant whose rows all happen to be disabled gets no tools, and that is
    honoured. Disabling every tool is a thing someone may genuinely want, and
    silently re-enabling the presets would make the dashboard a lie.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logger import get_logger
from app.models.tenant_tool import TenantTool
from app.services.tool_presets import PRESET_MAP, PRESET_SPECS, preset_rows
from app.services.tool_spec import ToolSpec

logger = get_logger(__name__)

TOOLS_CACHE_PREFIX = "tenant_tools:"
TOOLS_CACHE_TTL = 300  # 5 minutes, matching the tenant config cache

# Columns a caller may set. Anything else on the model (id, tenant_id,
# timestamps, is_builtin) is ours.
_WRITABLE_FIELDS = (
    "name",
    "display_name",
    "description",
    "parameters",
    "http_method",
    "path_template",
    "query_template",
    "body_template",
    "headers_template",
    "success_template",
    "is_enabled",
    "sort_order",
)


class ToolConflictError(Exception):
    """A tool with that name already exists for this tenant."""


class ToolRegistry:
    """Reads and writes the tools a tenant's agent can call."""

    def __init__(self, db: AsyncSession, redis: Redis | None = None):
        self.db = db
        self.redis = redis

    # ----------------------------------------------------------------
    # HOT PATH — what the agent may call
    # ----------------------------------------------------------------

    async def load_specs(self, tenant_id: UUID | str) -> list[ToolSpec]:
        """
        The enabled tools for a tenant, as plain specs.

        Called once per conversation turn on the REST path and once per room on
        the LiveKit path, so it reads from Redis when it can.
        """
        tenant_id = str(tenant_id)

        cached = await self._read_cache(tenant_id)
        if cached is not None:
            return cached

        rows = await self._fetch_rows(tenant_id)

        if not rows:
            # No rows at all — see the module docstring.
            logger.info("tool_registry_preset_fallback", tenant_id=tenant_id)
            return list(PRESET_SPECS)

        specs = [ToolSpec.from_row(r) for r in rows if r.is_enabled]
        await self._write_cache(tenant_id, specs)
        return specs

    async def load_spec_map(self, tenant_id: UUID | str) -> dict[str, ToolSpec]:
        """The same specs, keyed by name for dispatch."""
        return {s.name: s for s in await self.load_specs(tenant_id)}

    # ----------------------------------------------------------------
    # READ
    # ----------------------------------------------------------------

    async def list_tools(
        self, tenant_id: UUID | str, include_disabled: bool = True
    ) -> list[TenantTool]:
        """All of a tenant's tools, in dashboard order."""
        rows = await self._fetch_rows(str(tenant_id))
        if not include_disabled:
            rows = [r for r in rows if r.is_enabled]
        return rows

    async def get_tool(
        self, tenant_id: UUID | str, tool_id: UUID
    ) -> TenantTool | None:
        result = await self.db.execute(
            select(TenantTool).where(
                TenantTool.id == tool_id,
                TenantTool.tenant_id == _as_uuid(tenant_id),
            )
        )
        return result.scalar_one_or_none()

    async def get_tool_by_name(
        self, tenant_id: UUID | str, name: str
    ) -> TenantTool | None:
        result = await self.db.execute(
            select(TenantTool).where(
                TenantTool.tenant_id == _as_uuid(tenant_id),
                TenantTool.name == name,
            )
        )
        return result.scalar_one_or_none()

    # ----------------------------------------------------------------
    # WRITE
    # ----------------------------------------------------------------

    async def create_tool(
        self, tenant_id: UUID | str, payload: dict[str, Any]
    ) -> TenantTool:
        """
        Add a tool.

        Raises:
            ToolConflictError: if the name is already taken for this tenant.
        """
        name = payload["name"]
        if await self.get_tool_by_name(tenant_id, name) is not None:
            raise ToolConflictError(
                f"A tool named '{name}' already exists for this tenant."
            )

        tool = TenantTool(
            tenant_id=_as_uuid(tenant_id),
            is_builtin=False,
            **{k: v for k, v in payload.items() if k in _WRITABLE_FIELDS},
        )
        self.db.add(tool)
        await self.db.commit()
        await self.db.refresh(tool)
        await self._invalidate(tenant_id)

        logger.info(
            "tool_created",
            tenant_id=str(tenant_id),
            tool_id=str(tool.id),
            name=tool.name,
        )
        return tool

    async def update_tool(
        self, tenant_id: UUID | str, tool_id: UUID, payload: dict[str, Any]
    ) -> TenantTool | None:
        """
        Apply a partial update. Only the keys present in `payload` are touched,
        so clearing a template means sending it explicitly as null.
        """
        tool = await self.get_tool(tenant_id, tool_id)
        if tool is None:
            return None

        new_name = payload.get("name")
        if new_name and new_name != tool.name:
            clash = await self.get_tool_by_name(tenant_id, new_name)
            if clash is not None:
                raise ToolConflictError(
                    f"A tool named '{new_name}' already exists for this tenant."
                )

        for key, value in payload.items():
            if key in _WRITABLE_FIELDS:
                setattr(tool, key, value)

        await self.db.commit()
        await self.db.refresh(tool)
        await self._invalidate(tenant_id)

        logger.info(
            "tool_updated",
            tenant_id=str(tenant_id),
            tool_id=str(tool.id),
            name=tool.name,
            fields=sorted(k for k in payload if k in _WRITABLE_FIELDS),
        )
        return tool

    async def delete_tool(self, tenant_id: UUID | str, tool_id: UUID) -> bool:
        tool = await self.get_tool(tenant_id, tool_id)
        if tool is None:
            return False

        name = tool.name
        await self.db.delete(tool)
        await self.db.commit()
        await self._invalidate(tenant_id)

        logger.info(
            "tool_deleted", tenant_id=str(tenant_id), tool_id=str(tool_id), name=name
        )
        return True

    # ----------------------------------------------------------------
    # PRESETS
    # ----------------------------------------------------------------

    async def seed_presets(
        self,
        tenant_id: UUID | str,
        names: list[str] | tuple[str, ...] | None = None,
        commit: bool = True,
    ) -> list[TenantTool]:
        """
        Install presets, skipping any already present.

        Idempotent, so it is safe to call on tenant creation and again later
        from the dashboard. `commit=False` lets a caller fold the seeding into
        its own transaction — tenant creation does this, so a tenant and its
        tools appear together or not at all.
        """
        existing = {r.name for r in await self._fetch_rows(str(tenant_id))}

        created: list[TenantTool] = []
        for row in preset_rows(names):
            if row["name"] in existing:
                continue
            tool = TenantTool(tenant_id=_as_uuid(tenant_id), **row)
            self.db.add(tool)
            created.append(tool)

        if not created:
            return []

        if commit:
            await self.db.commit()
            for tool in created:
                await self.db.refresh(tool)
        else:
            await self.db.flush()

        await self._invalidate(tenant_id)
        logger.info(
            "tool_presets_seeded",
            tenant_id=str(tenant_id),
            count=len(created),
            names=[t.name for t in created],
        )
        return created

    async def reset_to_preset(
        self, tenant_id: UUID | str, tool_id: UUID
    ) -> TenantTool | None:
        """
        Put an edited built-in tool back to its shipped definition.

        Returns None if the tool does not exist or has no preset to go back to
        (a hand-written tool), which the route turns into a 404 or 409.
        """
        tool = await self.get_tool(tenant_id, tool_id)
        if tool is None:
            return None

        preset = PRESET_MAP.get(tool.name)
        if preset is None:
            return None

        for key, value in preset.items():
            if key in _WRITABLE_FIELDS and key != "is_enabled":
                setattr(tool, key, value)

        await self.db.commit()
        await self.db.refresh(tool)
        await self._invalidate(tenant_id)

        logger.info(
            "tool_reset_to_preset", tenant_id=str(tenant_id), name=tool.name
        )
        return tool

    # ----------------------------------------------------------------
    # INTERNALS
    # ----------------------------------------------------------------

    async def _fetch_rows(self, tenant_id: str) -> list[TenantTool]:
        result = await self.db.execute(
            select(TenantTool)
            .where(TenantTool.tenant_id == _as_uuid(tenant_id))
            .order_by(TenantTool.sort_order, TenantTool.name)
        )
        return list(result.scalars().all())

    async def _read_cache(self, tenant_id: str) -> list[ToolSpec] | None:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(f"{TOOLS_CACHE_PREFIX}{tenant_id}")
            if not raw:
                return None
            logger.debug("tool_specs_cache_hit", tenant_id=tenant_id)
            return [ToolSpec.from_dict(d) for d in json.loads(raw)]
        except Exception as e:
            logger.warning("tool_specs_cache_read_error", error=str(e))
            return None

    async def _write_cache(self, tenant_id: str, specs: list[ToolSpec]) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(
                f"{TOOLS_CACHE_PREFIX}{tenant_id}",
                json.dumps([s.to_dict() for s in specs]),
                ex=TOOLS_CACHE_TTL,
            )
        except Exception as e:
            logger.warning("tool_specs_cache_write_error", error=str(e))

    async def _invalidate(self, tenant_id: UUID | str) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.delete(f"{TOOLS_CACHE_PREFIX}{tenant_id}")
            logger.debug("tool_specs_cache_invalidated", tenant_id=str(tenant_id))
        except Exception as e:
            logger.warning("tool_specs_cache_invalidate_error", error=str(e))


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
