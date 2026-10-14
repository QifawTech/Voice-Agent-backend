"""
Voice Agent Platform — Spoken Result Formatting

The restaurant API answers in JSON. The agent answers out loud. Something has
to turn one into the other, and it cannot be the raw dict: a JSON dump read by
a TTS engine is unlistenable, and when the LLM echoes a tool result verbatim
that is exactly what the caller hears.

Each tool carries an optional `success_template` for this. It is a sentence
with {placeholders} filled from the API's response body:

    {order_id}              a top-level field
    {order.id}              a nested field
    {items[*].name}         every item's name, joined with commas — the one
                            shape that actually comes up, because menu and
                            order endpoints return lists of objects

A template that cannot be filled (the field is absent, the shape is not what
the author expected) falls back to the generic summary rather than reading out
a half-finished sentence with a literal {order_id} in it. Tools with no
template use the generic summary directly.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.tool_spec import ToolSpec, _as_text

# {items[*].name} — a list field, then the attribute to pluck from each entry.
_LIST_FIELD_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\[\*\]\.([a-zA-Z_][a-zA-Z0-9_.]*)\}")
# {order_id} or {order.id}
_SCALAR_FIELD_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\}")

# Response keys that are our own bookkeeping, not something to read out.
_INTERNAL_KEYS = ("success", "_response_time_ms", "status_code")

_MAX_LIST_ITEMS = 12


class _Unresolved(Exception):
    """A template referenced something the response does not contain."""


def format_tool_result(spec: ToolSpec | None, result: dict[str, Any]) -> str:
    """
    Render a successful tool result as one speakable sentence.

    Never raises: a broken template degrades to the generic summary.
    """
    template = spec.success_template if spec else None
    if template:
        try:
            return _render(template, result)
        except _Unresolved:
            pass  # fall through to the generic summary
    return _generic_summary(result)


def _render(template: str, result: dict[str, Any]) -> str:
    def _list_field(match: re.Match[str]) -> str:
        values = _lookup(result, match.group(1))
        if not isinstance(values, list):
            raise _Unresolved(match.group(1))
        attr = match.group(2)
        plucked = [
            _as_text(_lookup(entry, attr))
            for entry in values[:_MAX_LIST_ITEMS]
            if isinstance(entry, dict) and _lookup_safe(entry, attr) is not None
        ]
        if not plucked:
            raise _Unresolved(match.group(0))
        joined = ", ".join(plucked)
        if len(values) > _MAX_LIST_ITEMS:
            joined += f", and {len(values) - _MAX_LIST_ITEMS} more"
        return joined

    def _scalar_field(match: re.Match[str]) -> str:
        return _as_text(_lookup(result, match.group(1)))

    rendered = _LIST_FIELD_RE.sub(_list_field, template)
    return _SCALAR_FIELD_RE.sub(_scalar_field, rendered)


def _lookup(source: Any, dotted: str) -> Any:
    value = _lookup_safe(source, dotted)
    if value is None:
        raise _Unresolved(dotted)
    return value


def _lookup_safe(source: Any, dotted: str) -> Any:
    current = source
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _generic_summary(result: dict[str, Any]) -> str:
    """
    Describe a response no template covers, in prose rather than JSON.

    Nested values are summarised by shape instead of being spelled out — "3
    items" is useful to hear, a serialised array of objects is not.
    """
    parts: list[str] = []
    for key, value in result.items():
        if key in _INTERNAL_KEYS:
            continue
        label = key.replace("_", " ")
        if isinstance(value, list):
            parts.append(f"{len(value)} {label}")
        elif isinstance(value, dict):
            parts.append(label)
        else:
            parts.append(f"{label} is {_as_text(value)}")

    if not parts:
        return "Done."
    return "Action succeeded: " + ", ".join(parts)
