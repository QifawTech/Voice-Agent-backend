"""
Voice Agent Platform — Tool Routes

CRUD for the function tools a tenant's voice agent can call, plus the two
things that make the tool builder usable rather than just possible: a catalogue
of built-in presets to start from, and a test runner.

    GET    /api/v1/tenants/{id}/tools                  — List
    POST   /api/v1/tenants/{id}/tools                  — Add
    GET    /api/v1/tenants/{id}/tools/presets          — Built-in catalogue
    POST   /api/v1/tenants/{id}/tools/install-presets  — Install presets
    GET    /api/v1/tenants/{id}/tools/{tool_id}        — Get one
    PUT    /api/v1/tenants/{id}/tools/{tool_id}        — Edit
    DELETE /api/v1/tenants/{id}/tools/{tool_id}        — Remove
    POST   /api/v1/tenants/{id}/tools/{tool_id}/test   — Run it for real
    POST   /api/v1/tenants/{id}/tools/{tool_id}/reset  — Back to the preset

The test route is the one to keep honest. A tool is a template over an HTTP
call, and a template typo is invisible until a customer is on the line — so the
response hands back the fully resolved request alongside the result, and what
the agent would have said about it.

Route order matters: /presets and /install-presets are declared before
/{tool_id}, or FastAPI would try to parse "presets" as a UUID.

All routes require JWT authentication and are scoped to the caller's tenant.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logger import get_logger
from app.middleware.auth import get_current_user
from app.models.admin_user import AdminUser
from app.models.tenant import Tenant
from app.redis_client import get_redis
from app.schemas.tool import (
    PresetInstallRequest,
    ToolCreate,
    ToolListResponse,
    ToolPreset,
    ToolPresetListResponse,
    ToolResponse,
    ToolTestRequest,
    ToolTestResponse,
    ToolUpdate,
    _check_placeholders,
    _validate_parameters,
)
from app.services.action_router import ActionRouter
from app.services.tenant_service import TenantService
from app.services.tool_presets import PRESET_MAP, PRESETS
from app.services.tool_registry import ToolConflictError, ToolRegistry
from app.services.tool_result import format_tool_result
from app.services.tool_spec import ToolSpec, ToolTemplateError, build_request

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/tenants", tags=["Tools"])


def _get_registry(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ToolRegistry:
    return ToolRegistry(db=db, redis=redis)


def _get_tenant_service(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TenantService:
    return TenantService(db=db, redis=redis)


async def _require_tenant(
    tenant_id: UUID,
    current_user: AdminUser,
    tenant_service: TenantService,
) -> Tenant:
    """Load the tenant, enforcing the caller's access scope."""
    tenant = await tenant_service.get_tenant(tenant_id, current_user)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    return tenant


# ----------------------------------------------------------------
# LIST / CREATE
# ----------------------------------------------------------------


@router.get(
    "/{tenant_id}/tools",
    response_model=ToolListResponse,
    summary="List Tools",
    description=(
        "Every tool configured for this tenant, enabled or not. A tenant with "
        "no tools of its own runs on the built-in restaurant set, which the "
        "`using_preset_fallback` flag reports."
    ),
)
async def list_tools(
    tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    registry: ToolRegistry = Depends(_get_registry),
):
    await _require_tenant(tenant_id, current_user, tenant_service)
    rows = await registry.list_tools(tenant_id)
    return ToolListResponse(
        tools=[ToolResponse.model_validate(r) for r in rows],
        total=len(rows),
        enabled=sum(1 for r in rows if r.is_enabled),
        using_preset_fallback=not rows,
    )


@router.post(
    "/{tenant_id}/tools",
    response_model=ToolResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Tool",
)
async def create_tool(
    tenant_id: UUID,
    payload: ToolCreate,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    registry: ToolRegistry = Depends(_get_registry),
):
    await _require_tenant(tenant_id, current_user, tenant_service)
    try:
        tool = await registry.create_tool(tenant_id, payload.model_dump())
    except ToolConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return tool


# ----------------------------------------------------------------
# PRESETS  (declared before /{tool_id} so the literal paths win)
# ----------------------------------------------------------------


@router.get(
    "/{tenant_id}/tools/presets",
    response_model=ToolPresetListResponse,
    summary="List Built-in Tool Presets",
    description=(
        "The standard restaurant tools — menu lookups, ordering, order status, "
        "cancellation and payment details — ready to install. `installed` says "
        "which this tenant already has."
    ),
)
async def list_presets(
    tenant_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    registry: ToolRegistry = Depends(_get_registry),
):
    await _require_tenant(tenant_id, current_user, tenant_service)
    installed = {r.name for r in await registry.list_tools(tenant_id)}

    presets = [
        ToolPreset(
            name=p["name"],
            display_name=p.get("display_name"),
            description=p["description"],
            parameters=p["parameters"],
            http_method=p["http_method"],
            path_template=p["path_template"],
            installed=p["name"] in installed,
        )
        for p in PRESETS
    ]
    return ToolPresetListResponse(
        presets=presets,
        total=len(presets),
        installed=sum(1 for p in presets if p.installed),
    )


@router.post(
    "/{tenant_id}/tools/install-presets",
    response_model=ToolListResponse,
    summary="Install Built-in Tool Presets",
    description=(
        "Install the named presets, or every missing one if no names are given. "
        "Already-installed tools are left exactly as they are — this never "
        "overwrites an edit. Use the reset route for that."
    ),
)
async def install_presets(
    tenant_id: UUID,
    payload: PresetInstallRequest,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    registry: ToolRegistry = Depends(_get_registry),
):
    await _require_tenant(tenant_id, current_user, tenant_service)

    if payload.names:
        unknown = [n for n in payload.names if n not in PRESET_MAP]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown preset(s): {', '.join(unknown)}",
            )

    await registry.seed_presets(tenant_id, payload.names)

    rows = await registry.list_tools(tenant_id)
    return ToolListResponse(
        tools=[ToolResponse.model_validate(r) for r in rows],
        total=len(rows),
        enabled=sum(1 for r in rows if r.is_enabled),
        using_preset_fallback=not rows,
    )


# ----------------------------------------------------------------
# READ / UPDATE / DELETE
# ----------------------------------------------------------------


@router.get(
    "/{tenant_id}/tools/{tool_id}",
    response_model=ToolResponse,
    summary="Get Tool",
)
async def get_tool(
    tenant_id: UUID,
    tool_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    registry: ToolRegistry = Depends(_get_registry),
):
    await _require_tenant(tenant_id, current_user, tenant_service)
    tool = await registry.get_tool(tenant_id, tool_id)
    if tool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found"
        )
    return tool


@router.put(
    "/{tenant_id}/tools/{tool_id}",
    response_model=ToolResponse,
    summary="Update Tool",
    description=(
        "Partial update — only the fields you send change. The merged result is "
        "re-validated, so a patch that leaves a {placeholder} without its "
        "parameter is rejected even though neither half is wrong alone."
    ),
)
async def update_tool(
    tenant_id: UUID,
    tool_id: UUID,
    payload: ToolUpdate,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    registry: ToolRegistry = Depends(_get_registry),
):
    await _require_tenant(tenant_id, current_user, tenant_service)

    tool = await registry.get_tool(tenant_id, tool_id)
    if tool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found"
        )

    changes = payload.model_dump(exclude_unset=True)
    _validate_merged(tool, changes)

    try:
        updated = await registry.update_tool(tenant_id, tool_id, changes)
    except ToolConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found"
        )
    return updated


@router.delete(
    "/{tenant_id}/tools/{tool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Tool",
    description=(
        "Removes the tool outright. To stop the agent using it while keeping "
        "the configuration, set is_enabled to false instead."
    ),
)
async def delete_tool(
    tenant_id: UUID,
    tool_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    registry: ToolRegistry = Depends(_get_registry),
):
    await _require_tenant(tenant_id, current_user, tenant_service)
    if not await registry.delete_tool(tenant_id, tool_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found"
        )
    return None


@router.post(
    "/{tenant_id}/tools/{tool_id}/reset",
    response_model=ToolResponse,
    summary="Reset Tool To Preset",
    description=(
        "Discard edits and restore the shipped definition. Only applies to a "
        "tool whose name matches a built-in preset; the enabled/disabled state "
        "is left alone."
    ),
)
async def reset_tool(
    tenant_id: UUID,
    tool_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    registry: ToolRegistry = Depends(_get_registry),
):
    await _require_tenant(tenant_id, current_user, tenant_service)

    tool = await registry.get_tool(tenant_id, tool_id)
    if tool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found"
        )
    if tool.name not in PRESET_MAP:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{tool.name}' is not a built-in tool, so there is no preset to "
                "reset to."
            ),
        )

    return await registry.reset_to_preset(tenant_id, tool_id)


# ----------------------------------------------------------------
# TEST
# ----------------------------------------------------------------


@router.post(
    "/{tenant_id}/tools/{tool_id}/test",
    response_model=ToolTestResponse,
    summary="Test Tool",
    description=(
        "Run the tool against the tenant's real API with the arguments you "
        "supply, exactly as the agent would. The response includes the resolved "
        "request, so a template mistake is visible immediately.\n\n"
        "This is a real call: testing create_order places an order."
    ),
)
async def test_tool(
    tenant_id: UUID,
    tool_id: UUID,
    payload: ToolTestRequest,
    current_user: AdminUser = Depends(get_current_user),
    tenant_service: TenantService = Depends(_get_tenant_service),
    registry: ToolRegistry = Depends(_get_registry),
):
    tenant = await _require_tenant(tenant_id, current_user, tenant_service)

    tool = await registry.get_tool(tenant_id, tool_id)
    if tool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found"
        )

    if not tenant.api_base_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This tenant has no API base URL configured, so there is nothing "
                "to call. Set it under API Connection first."
            ),
        )

    spec = ToolSpec.from_row(tool)
    router_svc = ActionRouter()

    args = dict(payload.arguments or {})
    if payload.restaurant_id:
        args["restaurant_id"] = payload.restaurant_id

    # Resolve the request first and report it whatever happens next — the whole
    # point of the test route is seeing what the template produced.
    try:
        request = build_request(
            spec=spec,
            api_base_url=tenant.api_base_url.rstrip("/"),
            args=args,
        )
    except ToolTemplateError as e:
        return ToolTestResponse(
            success=False,
            request={"error": "Could not build the request from this template."},
            error=str(e),
        )

    resolved = {
        "method": request.method,
        "url": request.url,
        "params": request.params,
        "body": request.json_body,
    }

    result = await router_svc.execute(
        function_name=spec.name,
        arguments=args,
        tenant=tenant,
        restaurant_id=payload.restaurant_id,
        tools={spec.name: spec},
    )

    succeeded = bool(result.get("success"))

    logger.info(
        "tool_tested",
        tenant_id=str(tenant_id),
        name=spec.name,
        success=succeeded,
        tested_by=str(current_user.id),
    )

    return ToolTestResponse(
        success=succeeded,
        request=resolved,
        result=result,
        spoken_response=format_tool_result(spec, result) if succeeded else None,
        error=None if succeeded else result.get("error"),
    )


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------


def _validate_merged(tool, changes: dict) -> None:
    """
    Validate a patch against the row it will produce.

    ToolUpdate cannot do this: a request that changes only `path_template` has
    no parameters to check it against. Merging first is the only way to catch a
    placeholder whose parameter was removed in an earlier edit.
    """
    merged = {
        "parameters": tool.parameters,
        "path_template": tool.path_template,
        "query_template": tool.query_template,
        "body_template": tool.body_template,
        "headers_template": tool.headers_template,
        **changes,
    }

    try:
        if "parameters" in changes:
            merged["parameters"] = _validate_parameters(merged["parameters"])
            # Store the normalised version, not the raw one we were handed.
            changes["parameters"] = merged["parameters"]
        _check_placeholders(
            merged["parameters"] or {},
            merged["path_template"],
            merged["query_template"],
            merged["body_template"],
            merged["headers_template"],
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        )
