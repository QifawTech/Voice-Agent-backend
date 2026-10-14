"""
Voice Agent Platform — Tool Spec & Templating

A ToolSpec is one function tool, flattened out of its database row into a plain
frozen dataclass. Plain matters: a spec crosses an event loop boundary into the
LiveKit worker job and gets cached in Redis, where a live ORM object would
raise DetachedInstanceError or fail to serialise.

This module owns the template language that turns a tool call's arguments into
an HTTP request, and the checks that keep a tenant-authored template from
pointing anywhere but the tenant's own API.

──────────────────────────────────────────────────────────────────────────────
TEMPLATE LANGUAGE
──────────────────────────────────────────────────────────────────────────────

A {placeholder} is filled from the tool call's arguments, plus the implicit
`restaurant_id` of the sub-tenant being served.

    Exact match       "{items}"            → the raw argument, type intact.
                                             An array stays an array; this is
                                             what lets create_order post a real
                                             JSON list rather than a string of
                                             one.

    Interpolation     "Table {table_no}"   → substituted into the string.

    Unresolved        a placeholder whose argument is missing, None or ""
                      → the key is dropped from the query or body entirely,
                        rather than sent as the literal text "{table_no}" or as
                        a null the customer's API has to special-case.

    Optional path     "/menu[/{item_id}]"  → the bracketed group disappears
                                             unless item_id resolves. The only
                                             way to express a conditional
                                             sub-resource, and the reason
                                             get_item_details can look up by id
                                             or search by name through one row.

Literals (numbers, booleans, nested objects) pass through untouched, so a
template can carry constants the LLM never sees.

──────────────────────────────────────────────────────────────────────────────
WHY THE PATH IS CONSTRAINED
──────────────────────────────────────────────────────────────────────────────

These templates come from the dashboard, and the values poured into them come
from an LLM reading what a caller said out loud. Neither is trusted to name a
destination. A path template must be relative, and every placeholder value is
percent-encoded with no safe characters — so an `order_id` of "../../internal"
becomes one encoded segment instead of climbing out of the tenant's API root.
The request can only ever land under the tenant's own api_base_url.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

# ──────────────────────────────────────────────────────────────
# Grammar
# ──────────────────────────────────────────────────────────────

PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_EXACT_PLACEHOLDER_RE = re.compile(r"^\{([a-zA-Z_][a-zA-Z0-9_]*)\}$")
_OPTIONAL_GROUP_RE = re.compile(r"\[([^\[\]]*)\]")

# A tool name has to be a legal function identifier for the LLM provider.
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

ALLOWED_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# Methods that may carry a JSON body. A GET with a body is silently dropped by
# many servers and confuses the rest, so we never build one.
BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# Arguments every tool gets for free, whether or not it declares them.
IMPLICIT_ARGUMENTS = ("restaurant_id",)

# Header names a tool may not set: these are ours to control, and letting a
# template overwrite the Authorization header would leak or break tenant auth.
RESERVED_HEADERS = ("authorization", "host", "content-length", "x-voice-agent")


class ToolTemplateError(ValueError):
    """A template is malformed, or could not be resolved into a request."""


# ──────────────────────────────────────────────────────────────
# ToolSpec
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolSpec:
    """One function tool, ready to be offered to an LLM and then executed."""

    name: str
    description: str
    parameters: dict[str, Any]
    http_method: str
    path_template: str
    query_template: dict[str, Any] = field(default_factory=dict)
    body_template: dict[str, Any] = field(default_factory=dict)
    headers_template: dict[str, str] = field(default_factory=dict)
    success_template: str | None = None

    # ---- Construction ----

    @classmethod
    def from_row(cls, row: Any) -> ToolSpec:
        """Build a spec from a TenantTool ORM row (or anything with the same attrs)."""
        return cls(
            name=row.name,
            description=row.description,
            parameters=row.parameters or {"type": "object", "properties": {}},
            http_method=(row.http_method or "GET").upper(),
            path_template=row.path_template,
            query_template=row.query_template or {},
            body_template=row.body_template or {},
            headers_template=row.headers_template or {},
            success_template=row.success_template,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolSpec:
        """Rebuild a spec from its serialised form (Redis cache, preset table)."""
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            parameters=data.get("parameters") or {"type": "object", "properties": {}},
            http_method=(data.get("http_method") or "GET").upper(),
            path_template=data["path_template"],
            query_template=data.get("query_template") or {},
            body_template=data.get("body_template") or {},
            headers_template=data.get("headers_template") or {},
            success_template=data.get("success_template"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the Redis cache."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "http_method": self.http_method,
            "path_template": self.path_template,
            "query_template": self.query_template,
            "body_template": self.body_template,
            "headers_template": self.headers_template,
            "success_template": self.success_template,
        }

    # ---- LLM-facing views ----

    def to_openai_tool(self) -> dict[str, Any]:
        """The OpenAI/DeepSeek `tools` entry for this spec."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_raw_schema(self) -> dict[str, Any]:
        """The LiveKit `raw_schema` for llm.function_tool()."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    # ---- Introspection ----

    @property
    def declared_parameters(self) -> set[str]:
        props = (self.parameters or {}).get("properties") or {}
        return set(props.keys())


# ──────────────────────────────────────────────────────────────
# Resolution
# ──────────────────────────────────────────────────────────────


def _is_missing(value: Any) -> bool:
    """
    Whether an argument counts as absent for template purposes.

    None and "" are absent; 0 and False are not. The old handlers used a plain
    truthiness check, which quietly dropped a legitimate quantity of 0 or a
    flag of False.
    """
    return value is None or value == ""


def resolve_value(template: Any, args: dict[str, Any]) -> tuple[Any, bool]:
    """
    Resolve one template value against the arguments.

    Returns (value, resolved). resolved=False means a placeholder had no
    argument, and the caller should drop this key rather than send a blank.
    """
    if isinstance(template, str):
        exact = _EXACT_PLACEHOLDER_RE.match(template)
        if exact:
            # Pass the argument through with its type intact.
            value = args.get(exact.group(1))
            return (None, False) if _is_missing(value) else (value, True)

        names = PLACEHOLDER_RE.findall(template)
        if not names:
            return template, True
        if any(_is_missing(args.get(n)) for n in names):
            return None, False
        return PLACEHOLDER_RE.sub(lambda m: _as_text(args[m.group(1)]), template), True

    if isinstance(template, dict):
        out: dict[str, Any] = {}
        for key, sub in template.items():
            value, ok = resolve_value(sub, args)
            if ok:
                out[key] = value
        return out, True

    if isinstance(template, list):
        out_list: list[Any] = []
        for sub in template:
            value, ok = resolve_value(sub, args)
            if ok:
                out_list.append(value)
        return out_list, True

    # Numbers, booleans, None — a literal the LLM never sees.
    return template, True


def resolve_path(path_template: str, args: dict[str, Any]) -> str:
    """
    Build the request path.

    Optional [/{placeholder}] groups drop out when unresolved. A placeholder
    outside a group that cannot be resolved is fatal: there is no sensible URL
    to request without it.
    """

    def _group(match: re.Match[str]) -> str:
        inner = match.group(1)
        names = PLACEHOLDER_RE.findall(inner)
        if any(_is_missing(args.get(n)) for n in names):
            return ""
        return _substitute_path(inner, args)

    path = _OPTIONAL_GROUP_RE.sub(_group, path_template)
    path = _substitute_path(path, args)

    if not path.startswith("/"):
        path = "/" + path
    return path


def _substitute_path(template: str, args: dict[str, Any]) -> str:
    """Substitute placeholders into a path, percent-encoding every value."""

    def _one(match: re.Match[str]) -> str:
        name = match.group(1)
        value = args.get(name)
        if _is_missing(value):
            raise ToolTemplateError(
                f"Path placeholder '{{{name}}}' has no value. Either mark that "
                f"parameter required, or wrap that part of the path in [] to "
                f"make it optional."
            )
        # safe="" encodes slashes and dots too, so a value can never add a path
        # segment or climb above the tenant's API root.
        return quote(_as_text(value), safe="")

    return PLACEHOLDER_RE.sub(_one, template)


def _as_text(value: Any) -> str:
    """Render a value for use inside a string template or a query parameter."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


@dataclass(frozen=True)
class ResolvedRequest:
    """A fully built HTTP request, ready to hand to httpx."""

    method: str
    url: str
    params: dict[str, str]
    json_body: dict[str, Any] | None
    headers: dict[str, str]


def build_request(
    spec: ToolSpec,
    api_base_url: str,
    args: dict[str, Any],
    base_headers: dict[str, str] | None = None,
) -> ResolvedRequest:
    """
    Turn a spec plus a tool call's arguments into a concrete request.

    Raises:
        ToolTemplateError: if a required path placeholder is missing, or the
            resolved URL would leave the tenant's API root.
    """
    path = resolve_path(spec.path_template, args)

    if "://" in path or path.startswith("//"):
        raise ToolTemplateError(
            f"Tool '{spec.name}' resolved to an absolute URL ({path!r}). "
            "Tool paths must stay relative to the tenant's API base URL."
        )

    url = api_base_url.rstrip("/") + path

    resolved_query, _ = resolve_value(spec.query_template or {}, args)
    params = {k: _as_text(v) for k, v in resolved_query.items()}

    json_body: dict[str, Any] | None = None
    if spec.http_method in BODY_METHODS:
        resolved_body, _ = resolve_value(spec.body_template or {}, args)
        # An empty dict would post "{}" where the old handlers sent no body at
        # all; some APIs reject that, so send nothing instead.
        json_body = resolved_body or None

    headers = dict(base_headers or {})
    resolved_headers, _ = resolve_value(spec.headers_template or {}, args)
    for key, value in resolved_headers.items():
        if key.lower() in RESERVED_HEADERS:
            continue
        headers[key] = _as_text(value)

    return ResolvedRequest(
        method=spec.http_method,
        url=url,
        params=params,
        json_body=json_body,
        headers=headers,
    )
