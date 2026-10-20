"""
Voice Agent Platform — Built-in Tool Definitions (compatibility layer)

Tool definitions are no longer written here. Each tenant owns its own tools in
the tenant_tools table, and the schemas sent to the model are built from those
rows by ToolRegistry.load_specs().

What remains is the built-in restaurant set, derived from
app/services/tool_presets.py, in the OpenAI tool shape. It is the fallback for
a tenant with no rows at all, and the definition any caller gets when it does
not pass tenant-specific tools.

To change what the built-in restaurant tools do, edit tool_presets.py — it is
the single source for both the schema the model sees and the HTTP request the
Action Router makes, which is the pair that used to drift apart when they lived
in two files.

Usage:
    from app.services.tool_registry import ToolRegistry

    specs = await ToolRegistry(db, redis).load_specs(tenant.id)
    response = await deepseek.chat_completion(
        messages=messages,
        tools=[s.to_openai_tool() for s in specs],
    )
"""

from app.services.tool_presets import PRESET_SPECS

# The built-in restaurant tools in OpenAI/DeepSeek `tools` format.
TOOL_DEFINITIONS: list[dict] = [spec.to_openai_tool() for spec in PRESET_SPECS]

# Tool name to definition mapping (useful for validation)
TOOL_MAP: dict[str, dict] = {
    tool["function"]["name"]: tool for tool in TOOL_DEFINITIONS
}

# Names of the built-in tools. NOT the set of names that is valid at runtime —
# a tenant may have added its own, so validation belongs to the specs the
# Action Router is given, not to this list.
BUILTIN_TOOL_NAMES: set[str] = set(TOOL_MAP.keys())

# Retained under its old name for existing imports.
VALID_TOOL_NAMES: set[str] = BUILTIN_TOOL_NAMES
