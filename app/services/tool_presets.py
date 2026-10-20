"""
Voice Agent Platform — Restaurant Tool Presets

The seven restaurant tools, as data. These used to be two hand-written halves
that had to be kept in step: a JSON schema in tool_definitions.py and a Python
handler in action_router.py. They are now one record each, seeded into
tenant_tools when a tenant is created, and editable from the dashboard
afterwards.

Each preset is the exact behaviour the hardcoded handlers had, so seeding a
tenant changes nothing about what its agent does:

    check_menu        GET    /voice-agent/menu?category=&restaurantId=
    get_item_details  GET    /voice-agent/menu[/{item_id}]?search=&restaurantId=
    get_item_price    GET    /voice-agent/price?item_name=&restaurantId=
    create_order      POST   /voice-agent/orders
    get_order         GET    /voice-agent/orders/{order_id}?restaurantId=
    cancel_order      DELETE /voice-agent/orders/{order_id}?restaurantId=
    get_payment_info  GET    /voice-agent/payment-info?restaurantId=

Two details worth knowing before editing:

  * get_item_details keeps its optional [/{item_id}] segment even though the
    LLM is only ever offered item_name. The old handler had the same split —
    look up by id when given one, otherwise search by name — and dropping it
    would break any caller that passes an id directly.

  * create_order declares `items` as a real array. The LiveKit path used to ask
    the model for a JSON string and parse it by hand; a DB-driven raw schema
    lets both paths take the array straight, which is one less place for the
    model to produce something unparseable.

PRESETS is the catalogue the dashboard offers. DEFAULT_PRESET_NAMES is what a
new tenant gets seeded with — currently all of them, kept separate so a preset
can be offered without being installed by default.
"""

from __future__ import annotations

from typing import Any

from app.services.tool_spec import ToolSpec

# Every preset is a plain dict shaped like a TenantTool row, so seeding is a
# straight insert and the dashboard can show a preset and an installed tool
# with the same component.
PRESETS: list[dict[str, Any]] = [
    # ──────────────────────────────────────────────────────────
    {
        "name": "check_menu",
        "display_name": "Check Menu",
        "description": (
            "Retrieve the restaurant's menu. Returns available categories and items "
            "with prices. Use when the customer asks about the menu, what's available, "
            "food options, or wants to browse items."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        "Optional menu category to filter by "
                        "(e.g., 'starters', 'main course', 'beverages', 'desserts'). "
                        "If not provided, returns all categories."
                    ),
                },
            },
            "required": [],
        },
        "http_method": "GET",
        "path_template": "/voice-agent/menu",
        "query_template": {
            "category": "{category}",
            "restaurantId": "{restaurant_id}",
        },
        "body_template": None,
        "headers_template": None,
        "success_template": "The menu has these items: {items[*].name}",
        "sort_order": 10,
    },
    # ──────────────────────────────────────────────────────────
    {
        "name": "get_item_details",
        "display_name": "Get Item Details",
        "description": (
            "Get detailed information about a specific menu item including "
            "description, price, availability, dietary info (veg/non-veg), and "
            "customization options. Use when the customer asks about a specific dish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": (
                        "The name of the menu item to look up "
                        "(e.g., 'Butter Chicken', 'Paneer Tikka')."
                    ),
                },
            },
            "required": ["item_name"],
        },
        "http_method": "GET",
        # The optional segment is taken when a caller supplies an item_id; the
        # LLM never does, so it searches by name instead.
        "path_template": "/voice-agent/menu[/{item_id}]",
        "query_template": {
            "search": "{item_name}",
            "restaurantId": "{restaurant_id}",
        },
        "body_template": None,
        "headers_template": None,
        "success_template": None,
        "sort_order": 20,
    },
    # ──────────────────────────────────────────────────────────
    {
        "name": "get_item_price",
        "display_name": "Get Item Price",
        "description": (
            "Get the price of a specific menu item. Use when the customer asks "
            "for the price of a specific dish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item_name": {
                    "type": "string",
                    "description": (
                        "The name of the menu item to look up (e.g., 'Butter Chicken')."
                    ),
                },
            },
            "required": ["item_name"],
        },
        "http_method": "GET",
        "path_template": "/voice-agent/price",
        "query_template": {
            "item_name": "{item_name}",
            "restaurantId": "{restaurant_id}",
        },
        "body_template": None,
        "headers_template": None,
        "success_template": None,
        "sort_order": 30,
    },
    # ──────────────────────────────────────────────────────────
    {
        "name": "create_order",
        "display_name": "Create Order",
        "description": (
            "Place a new order with the restaurant. Call this when the customer "
            "confirms they want to order specific items. Always confirm the items "
            "and quantities with the customer before calling this function."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "List of items to order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Name of the menu item.",
                            },
                            "quantity": {
                                "type": "integer",
                                "description": "Number of this item to order.",
                                "minimum": 1,
                            },
                            "special_instructions": {
                                "type": "string",
                                "description": (
                                    "Any special requests for this item "
                                    "(e.g., 'extra spicy', 'no onions')."
                                ),
                            },
                        },
                        "required": ["name", "quantity"],
                    },
                },
                "customer_name": {
                    "type": "string",
                    "description": "Customer's name for the order.",
                },
                "customer_phone": {
                    "type": "string",
                    "description": "Customer's phone number for order updates.",
                },
                "delivery_type": {
                    "type": "string",
                    "enum": ["dine_in", "takeaway", "delivery"],
                    "description": "How the customer wants to receive the order.",
                },
            },
            "required": ["items"],
        },
        "http_method": "POST",
        "path_template": "/voice-agent/orders",
        "query_template": None,
        # delivery_type is renamed to order_type on the way out, which is what
        # the old handler did and what the restaurant API expects.
        "body_template": {
            "items": "{items}",
            "customer_name": "{customer_name}",
            "customer_phone": "{customer_phone}",
            "order_type": "{delivery_type}",
            "restaurant_id": "{restaurant_id}",
        },
        "headers_template": None,
        "success_template": "Order placed successfully. The order ID is {order_id}.",
        "sort_order": 40,
    },
    # ──────────────────────────────────────────────────────────
    {
        "name": "get_order",
        "display_name": "Get Order Status",
        "description": (
            "Check the status of an existing order. Use when the customer asks "
            "about their order status, delivery time, or wants to know if their "
            "order is ready."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID to look up (e.g., 'ORD-1042').",
                },
            },
            "required": ["order_id"],
        },
        "http_method": "GET",
        "path_template": "/voice-agent/orders/{order_id}",
        "query_template": {"restaurantId": "{restaurant_id}"},
        "body_template": None,
        "headers_template": None,
        "success_template": None,
        "sort_order": 50,
    },
    # ──────────────────────────────────────────────────────────
    {
        "name": "cancel_order",
        "display_name": "Cancel Order",
        "description": (
            "Cancel an existing order. Use when the customer explicitly requests "
            "to cancel their order. Always confirm with the customer before cancelling. "
            "Note: cancellation may not be possible if the order is already being prepared."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID to cancel.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Reason for cancellation (e.g., 'changed mind', "
                        "'wrong items', 'too long wait')."
                    ),
                },
            },
            "required": ["order_id"],
        },
        "http_method": "DELETE",
        "path_template": "/voice-agent/orders/{order_id}",
        "query_template": {"restaurantId": "{restaurant_id}"},
        # No reason given means no body at all, not a body of {}.
        "body_template": {"reason": "{reason}"},
        "headers_template": None,
        "success_template": None,
        "sort_order": 60,
    },
    # ──────────────────────────────────────────────────────────
    {
        "name": "get_payment_info",
        "display_name": "Get Payment Info",
        "description": (
            "Get payment information for the restaurant, including UPI ID, "
            "accepted payment methods, and payment instructions. Use when the "
            "customer asks how to pay or wants payment details."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "http_method": "GET",
        "path_template": "/voice-agent/payment-info",
        "query_template": {"restaurantId": "{restaurant_id}"},
        "body_template": None,
        "headers_template": None,
        "success_template": None,
        "sort_order": 70,
    },
]

PRESET_MAP: dict[str, dict[str, Any]] = {p["name"]: p for p in PRESETS}

# What a newly created tenant is seeded with.
DEFAULT_PRESET_NAMES: tuple[str, ...] = tuple(p["name"] for p in PRESETS)

# Specs for the presets, used as the fallback when a tenant has no tool rows at
# all — a tenant created before this table existed, or one whose seeding failed,
# should still be able to take orders.
PRESET_SPECS: list[ToolSpec] = [ToolSpec.from_dict(p) for p in PRESETS]

PRESET_SPEC_MAP: dict[str, ToolSpec] = {s.name: s for s in PRESET_SPECS}


def preset_rows(names: tuple[str, ...] | list[str] | None = None) -> list[dict[str, Any]]:
    """
    Preset records ready to insert as tenant_tools rows.

    Returns copies: the caller is about to attach these to a session, and the
    module-level dicts must not pick up a tenant_id.
    """
    chosen = DEFAULT_PRESET_NAMES if names is None else names
    rows: list[dict[str, Any]] = []
    for name in chosen:
        preset = PRESET_MAP.get(name)
        if preset is None:
            continue
        row = {k: v for k, v in preset.items()}
        row["is_builtin"] = True
        row["is_enabled"] = True
        rows.append(row)
    return rows
