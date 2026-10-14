"""
Voice Agent Platform — Tool Schemas

Request and response models for the tool builder, and the validation that keeps
a saved tool from failing at the worst possible moment — mid-call, in front of
a customer.

Everything checkable at save time is checked at save time:

    * the name is a legal function identifier the model can emit
    * `parameters` is a JSON Schema object, with `required` naming real properties
    * the path is relative, so a tool cannot point away from the tenant's API
    * every {placeholder} refers to a declared parameter (or restaurant_id)

That last one is the rule that earns its keep. A typo like {order_Id} used to
mean a request with a literal "{order_Id}" in the URL and a confused 404; now
it is a 422 the moment someone presses Save, naming the typo and listing what
they could have meant.

Placeholders inside an optional [] path group are exempt: they exist precisely
to be filled by callers other than the model, which is how get_item_details
supports a lookup by id that it never asks the model for.
"""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.tool_spec import (
    ALLOWED_HTTP_METHODS,
    IMPLICIT_ARGUMENTS,
    PLACEHOLDER_RE,
    RESERVED_HEADERS,
    TOOL_NAME_RE,
    _OPTIONAL_GROUP_RE,
)

# A parameter name has to be usable as a {placeholder}.
PLACEHOLDER_SAFE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


# ---- Shared validation helpers ----


def _validate_name(value: str) -> str:
    value = value.strip()
    if not TOOL_NAME_RE.match(value):
        raise ValueError(
            "Tool name must be lowercase snake_case, start with a letter, and be "
            "2-63 characters (e.g. 'check_menu', 'book_table')."
        )
    return value


def _validate_parameters(value: dict | None) -> dict:
    if value is None:
        return {"type": "object", "properties": {}}
    if not isinstance(value, dict):
        raise ValueError("parameters must be a JSON Schema object.")

    if value.get("type") != "object":
        raise ValueError(
            "parameters must have \"type\": \"object\" — function arguments are "
            "always a named set."
        )

    props = value.get("properties")
    if props is None:
        value = {**value, "properties": {}}
        props = value["properties"]
    if not isinstance(props, dict):
        raise ValueError("parameters.properties must be an object.")

    for prop_name, schema in props.items():
        if not PLACEHOLDER_SAFE_NAME.match(prop_name):
            raise ValueError(
                f"Parameter name '{prop_name}' is not usable in a template. Use "
                "letters, digits and underscores, starting with a letter."
            )
        if not isinstance(schema, dict) or "type" not in schema:
            raise ValueError(
                f"Parameter '{prop_name}' needs a JSON Schema type "
                "(string, number, integer, boolean, array or object)."
            )

    required = value.get("required", [])
    if not isinstance(required, list):
        raise ValueError("parameters.required must be a list of parameter names.")
    unknown = [r for r in required if r not in props]
    if unknown:
        raise ValueError(
            f"parameters.required names parameters that do not exist: "
            f"{', '.join(sorted(unknown))}."
        )

    return value


def _validate_path(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Path is required, e.g. '/voice-agent/orders'.")
    if "://" in value or value.startswith("//"):
        raise ValueError(
            "Path must be relative to the tenant's API base URL — drop the "
            "scheme and host (use '/voice-agent/orders', not a full URL)."
        )
    if ".." in value:
        raise ValueError("Path may not contain '..'.")
    if not value.startswith("/"):
        value = "/" + value
    if value.count("[") != value.count("]"):
        raise ValueError(
            "Unbalanced [] in path. An optional segment looks like "
            "'/menu[/{item_id}]'."
        )
    return value


def _validate_headers(value: dict | None) -> dict | None:
    if not value:
        return value
    for key in value:
        if key.lower() in RESERVED_HEADERS:
            raise ValueError(
                f"The '{key}' header is managed by the platform and cannot be "
                "set by a tool."
            )
    return value


def _collect_placeholders(value: Any) -> set[str]:
    """Every {placeholder} in a nested template structure."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(PLACEHOLDER_RE.findall(value))
    elif isinstance(value, dict):
        for sub in value.values():
            found |= _collect_placeholders(sub)
    elif isinstance(value, list):
        for sub in value:
            found |= _collect_placeholders(sub)
    return found


def _check_placeholders(
    parameters: dict,
    path_template: str | None,
    query_template: dict | None,
    body_template: dict | None,
    headers_template: dict | None,
) -> None:
    """
    Every placeholder must name something that will exist at call time.

    Optional path groups are stripped first — their whole purpose is to hold a
    placeholder the model is never offered.
    """
    declared = set((parameters or {}).get("properties", {}).keys())
    known = declared | set(IMPLICIT_ARGUMENTS)

    required_path = _OPTIONAL_GROUP_RE.sub("", path_template or "")

    used = (
        _collect_placeholders(required_path)
        | _collect_placeholders(query_template)
        | _collect_placeholders(body_template)
        | _collect_placeholders(headers_template)
    )

    unknown = sorted(used - known)
    if unknown:
        offenders = ", ".join("{" + name + "}" for name in unknown)
        hint = ", ".join(sorted(declared)) or "none declared yet"
        raise ValueError(
            f"Template uses {offenders}, which this tool does not declare as a "
            f"parameter. Declared parameters: {hint}. "
            f"(restaurant_id is always available.)"
        )


# ---- Request Schemas ----


class ToolCreate(BaseModel):
    """Request body for adding a tool to a tenant."""

    name: str = Field(..., max_length=64, examples=["book_table"])
    display_name: str | None = Field(default=None, max_length=120, examples=["Book a Table"])
    description: str = Field(
        ...,
        min_length=10,
        description=(
            "What this tool does, written for the model. This is the only thing "
            "it reads when deciding whether to call it, so say when to use it."
        ),
        examples=["Reserve a table. Use when the customer asks to book."],
    )
    parameters: dict | None = Field(
        default=None,
        description="JSON Schema for the arguments the model should supply.",
        examples=[
            {
                "type": "object",
                "properties": {
                    "party_size": {"type": "integer", "description": "Number of guests."}
                },
                "required": ["party_size"],
            }
        ],
    )
    http_method: str = Field(default="GET", examples=["POST"])
    path_template: str = Field(..., max_length=500, examples=["/voice-agent/reservations"])
    query_template: dict | None = Field(
        default=None, examples=[{"restaurantId": "{restaurant_id}"}]
    )
    body_template: dict | None = Field(
        default=None, examples=[{"party_size": "{party_size}"}]
    )
    headers_template: dict | None = Field(default=None, examples=[{"X-Source": "voice"}])
    success_template: str | None = Field(
        default=None,
        description=(
            "One sentence for the agent to say on success, with {fields} from the "
            "API response. Leave empty for a generic summary."
        ),
        examples=["Your table is booked. The reference is {reservation_id}."],
    )
    is_enabled: bool = True
    sort_order: int = 0

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _validate_name(v)

    @field_validator("parameters")
    @classmethod
    def _params(cls, v: dict | None) -> dict:
        return _validate_parameters(v)

    @field_validator("http_method")
    @classmethod
    def _method(cls, v: str) -> str:
        v = (v or "GET").upper()
        if v not in ALLOWED_HTTP_METHODS:
            raise ValueError(
                f"http_method must be one of {', '.join(ALLOWED_HTTP_METHODS)}."
            )
        return v

    @field_validator("path_template")
    @classmethod
    def _path(cls, v: str) -> str:
        return _validate_path(v)

    @field_validator("headers_template")
    @classmethod
    def _headers(cls, v: dict | None) -> dict | None:
        return _validate_headers(v)

    @model_validator(mode="after")
    def _placeholders(self) -> "ToolCreate":
        _check_placeholders(
            self.parameters or {},
            self.path_template,
            self.query_template,
            self.body_template,
            self.headers_template,
        )
        return self


class ToolUpdate(BaseModel):
    """
    Request body for editing a tool. Every field is optional; only what is sent
    is changed.

    Cross-field checks are skipped here on purpose — a patch that changes only
    the path cannot see the parameters it has to agree with. The route
    re-validates the merged result, which is the only version that can be
    checked honestly.
    """

    name: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, min_length=10)
    parameters: dict | None = None
    http_method: str | None = None
    path_template: str | None = Field(default=None, max_length=500)
    query_template: dict | None = None
    body_template: dict | None = None
    headers_template: dict | None = None
    success_template: str | None = None
    is_enabled: bool | None = None
    sort_order: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return None if v is None else _validate_name(v)

    @field_validator("http_method")
    @classmethod
    def _method(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.upper()
        if v not in ALLOWED_HTTP_METHODS:
            raise ValueError(
                f"http_method must be one of {', '.join(ALLOWED_HTTP_METHODS)}."
            )
        return v

    @field_validator("path_template")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        return None if v is None else _validate_path(v)

    @field_validator("headers_template")
    @classmethod
    def _headers(cls, v: dict | None) -> dict | None:
        return _validate_headers(v)


class ToolTestRequest(BaseModel):
    """Run one tool against the tenant's real API, with arguments we supply."""

    arguments: dict = Field(
        default_factory=dict,
        description="The arguments a model would have produced.",
        examples=[{"category": "starters"}],
    )
    restaurant_id: str | None = Field(
        default=None,
        description="Sub-tenant slug to test as, for a multi-tenant account.",
    )


class PresetInstallRequest(BaseModel):
    """Install built-in presets. Omit `names` to install everything missing."""

    names: list[str] | None = Field(
        default=None, examples=[["check_menu", "create_order"]]
    )


# ---- Response Schemas ----


class ToolResponse(BaseModel):
    """One configured tool."""

    id: UUID
    tenant_id: UUID
    name: str
    display_name: str | None = None
    description: str
    parameters: dict
    http_method: str
    path_template: str
    query_template: dict | None = None
    body_template: dict | None = None
    headers_template: dict | None = None
    success_template: str | None = None
    is_enabled: bool
    is_builtin: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ToolListResponse(BaseModel):
    """A tenant's tools, plus what the agent would actually be offered."""

    tools: list[ToolResponse]
    total: int
    enabled: int
    using_preset_fallback: bool = Field(
        default=False,
        description=(
            "True when this tenant has no tools of its own and the agent is "
            "running on the built-in restaurant set."
        ),
    )


class ToolPreset(BaseModel):
    """A built-in tool on offer, and whether it is already installed."""

    name: str
    display_name: str | None = None
    description: str
    parameters: dict
    http_method: str
    path_template: str
    installed: bool


class ToolPresetListResponse(BaseModel):
    presets: list[ToolPreset]
    total: int
    installed: int


class ToolTestResponse(BaseModel):
    """What the tool did when we ran it, in enough detail to debug a template."""

    success: bool
    request: dict = Field(
        description=(
            "The resolved method, URL, query and body — so a template typo is "
            "visible without reading logs."
        ),
    )
    result: dict | None = None
    spoken_response: str | None = Field(
        default=None,
        description="What the agent would have said, via success_template.",
    )
    error: str | None = None
