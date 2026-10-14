"""
Mock Restaurant API — For Testing the Action Router

A standalone FastAPI server that simulates a real restaurant's API endpoints.
Follows the exact API contract from 01_system_design.md.

Run this server on port 9000, then point a test tenant's api_base_url to
http://localhost:9000 to test the full Action Router flow.

Usage:
    python mock_restaurant_api.py

    Server starts at http://localhost:9000
    Endpoints:
        GET  /voice-agent/menu
        GET  /voice-agent/menu/:itemId
        POST /voice-agent/orders
        GET  /voice-agent/orders/:orderId
        DELETE /voice-agent/orders/:orderId
        GET  /voice-agent/payment-info
        GET  /health
"""

import random
import string
from datetime import datetime

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse
import uvicorn
from sqlalchemy import text
from app.database import engine

app = FastAPI(
    title="Mock Restaurant API — Spice Garden",
    description="Simulates a restaurant API for Voice Agent testing",
    version="1.0.0",
)

# ──────────────────────────────────────────────────────────────
# In-Memory Data Store
# ──────────────────────────────────────────────────────────────

MENU_ITEMS = [
    # Starters
    {
        "id": "item_001",
        "name": "Paneer Tikka",
        "price": 250,
        "category": "Starters",
        "available": True,
        "description": "Grilled cottage cheese marinated in spices and yogurt",
        "is_veg": True,
        "variants": [
            {"name": "Half", "price": 150},
            {"name": "Full", "price": 250},
        ],
    },
    {
        "id": "item_002",
        "name": "Chicken 65",
        "price": 280,
        "category": "Starters",
        "available": True,
        "description": "Crispy fried chicken with spicy South Indian seasoning",
        "is_veg": False,
        "variants": [],
    },
    {
        "id": "item_003",
        "name": "Veg Spring Roll",
        "price": 180,
        "category": "Starters",
        "available": True,
        "description": "Crispy rolls stuffed with mixed vegetables",
        "is_veg": True,
        "variants": [],
    },
    # Main Course
    {
        "id": "item_004",
        "name": "Butter Chicken",
        "price": 350,
        "category": "Main Course",
        "available": True,
        "description": "Tender chicken in rich, creamy tomato-butter gravy",
        "is_veg": False,
        "variants": [
            {"name": "Half", "price": 220},
            {"name": "Full", "price": 350},
        ],
    },
    {
        "id": "item_005",
        "name": "Dal Makhani",
        "price": 220,
        "category": "Main Course",
        "available": True,
        "description": "Slow-cooked black lentils in butter and cream",
        "is_veg": True,
        "variants": [],
    },
    {
        "id": "item_006",
        "name": "Palak Paneer",
        "price": 240,
        "category": "Main Course",
        "available": True,
        "description": "Cottage cheese cubes in creamy spinach gravy",
        "is_veg": True,
        "variants": [],
    },
    {
        "id": "item_007",
        "name": "Chicken Biryani",
        "price": 320,
        "category": "Main Course",
        "available": True,
        "description": "Fragrant basmati rice layered with spiced chicken",
        "is_veg": False,
        "variants": [
            {"name": "Half", "price": 200},
            {"name": "Full", "price": 320},
        ],
    },
    {
        "id": "item_008",
        "name": "Veg Biryani",
        "price": 250,
        "category": "Main Course",
        "available": False,
        "description": "Fragrant basmati rice with mixed vegetables and spices",
        "is_veg": True,
        "variants": [],
    },
    # Breads
    {
        "id": "item_009",
        "name": "Butter Naan",
        "price": 50,
        "category": "Breads",
        "available": True,
        "description": "Soft leavened bread brushed with butter",
        "is_veg": True,
        "variants": [],
    },
    {
        "id": "item_010",
        "name": "Garlic Naan",
        "price": 60,
        "category": "Breads",
        "available": True,
        "description": "Naan bread topped with garlic and coriander",
        "is_veg": True,
        "variants": [],
    },
    {
        "id": "item_011",
        "name": "Tandoori Roti",
        "price": 30,
        "category": "Breads",
        "available": True,
        "description": "Whole wheat bread baked in tandoor oven",
        "is_veg": True,
        "variants": [],
    },
    # Beverages
    {
        "id": "item_012",
        "name": "Mango Lassi",
        "price": 120,
        "category": "Beverages",
        "available": True,
        "description": "Chilled yogurt smoothie with fresh mango pulp",
        "is_veg": True,
        "variants": [],
    },
    {
        "id": "item_013",
        "name": "Masala Chai",
        "price": 50,
        "category": "Beverages",
        "available": True,
        "description": "Indian spiced tea with cardamom and ginger",
        "is_veg": True,
        "variants": [],
    },
    # Desserts
    {
        "id": "item_014",
        "name": "Gulab Jamun",
        "price": 100,
        "category": "Desserts",
        "available": True,
        "description": "Deep-fried milk dumplings soaked in rose-flavored syrup",
        "is_veg": True,
        "variants": [
            {"name": "2 pcs", "price": 100},
            {"name": "4 pcs", "price": 180},
        ],
    },
    {
        "id": "item_015",
        "name": "Rasmalai",
        "price": 130,
        "category": "Desserts",
        "available": True,
        "description": "Soft cottage cheese dumplings in sweetened, flavored milk",
        "is_veg": True,
        "variants": [],
    },
]

# We will use Postgres instead of the ORDERS dict.
import uuid

def _generate_order_id() -> str:
    """Generate a unique order ID."""
    return f"ORD-{uuid.uuid4().hex[:8].upper()}"


@app.on_event("startup")
async def startup_event():
    """Create the mock_orders table if it doesn't exist."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS mock_orders (
                order_id VARCHAR PRIMARY KEY,
                status VARCHAR,
                items JSONB,
                total FLOAT,
                customer_name VARCHAR,
                customer_phone VARCHAR,
                order_type VARCHAR,
                notes TEXT,
                estimated_time VARCHAR,
                created_at TIMESTAMP
            )
        """))
    # Plain ASCII on purpose: a Windows console defaults to cp1252, which
    # cannot encode a check mark. This runs inside the startup handler, so
    # the UnicodeEncodeError took the whole server down before it could
    # serve a single request.
    print("Initialized mock_orders table in Postgres")


# ──────────────────────────────────────────────────────────────
# Auth Middleware (simple Bearer token check)
# ──────────────────────────────────────────────────────────────

# Accept any of these tokens for testing
VALID_TOKENS = {
    "test-token-123",
    "mock-restaurant-key",
    "dev-token",
}


def verify_auth(authorization: str | None = Header(None)):
    """
    Simple auth check — accepts known test tokens.
    Also passes if no auth header is provided (for easy testing).
    """
    if authorization is None:
        # Allow unauthenticated requests for easy local testing
        return True

    if authorization.startswith("Bearer "):
        token = authorization[7:]
        if token in VALID_TOKENS:
            return True

    raise HTTPException(
        status_code=401,
        detail={"success": False, "error": "Invalid authentication token"},
    )


# ──────────────────────────────────────────────────────────────
# Endpoint 1: GET /voice-agent/menu
# ──────────────────────────────────────────────────────────────

@app.get("/voice-agent/menu")
async def get_menu(
    category: str | None = Query(None, description="Filter by menu category"),
    search: str | None = Query(None, description="Search items by name"),
    restaurantId: str | None = Query(None, description="Restaurant ID (multi-restaurant)"),
    authorization: str | None = Header(None),
):
    """
    Fetch the restaurant menu.

    Supports optional filtering by category and searching by name.
    """
    verify_auth(authorization)

    items = MENU_ITEMS.copy()

    # Filter by category (case-insensitive)
    if category:
        items = [
            item for item in items
            if item["category"].lower() == category.lower()
        ]

    # Search by name (partial, case-insensitive)
    if search:
        search_lower = search.lower()
        items = [
            item for item in items
            if search_lower in item["name"].lower()
        ]

    # Return summary view (only name and category as requested)
    menu_items = [
        {
            "name": item["name"],
            "category": item["category"],
        }
        for item in items
    ]

    return {"success": True, "items": menu_items}


# ──────────────────────────────────────────────────────────────
# Endpoint 2: GET /voice-agent/menu/:itemId
# ──────────────────────────────────────────────────────────────

@app.get("/voice-agent/menu/{item_id}")
async def get_item_details(
    item_id: str,
    authorization: str | None = Header(None),
):
    """Get detailed information about a specific menu item."""
    verify_auth(authorization)

    item = next((i for i in MENU_ITEMS if i["id"] == item_id), None)
    if not item:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": f"Item '{item_id}' not found"},
        )

    return {"success": True, "item": item}


# ──────────────────────────────────────────────────────────────
# Endpoint: GET /voice-agent/price
# ──────────────────────────────────────────────────────────────

@app.get("/voice-agent/price")
async def get_item_price(
    item_name: str = Query(..., description="Name of the menu item"),
    authorization: str | None = Header(None),
):
    """Get the price of a specific menu item by name."""
    verify_auth(authorization)

    item = next((i for i in MENU_ITEMS if i["name"].lower() == item_name.lower()), None)
    if not item:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": f"Item '{item_name}' not found"},
        )

    return {"success": True, "name": item["name"], "price": item["price"]}


# ──────────────────────────────────────────────────────────────
# Endpoint 3: POST /voice-agent/orders
# ──────────────────────────────────────────────────────────────

@app.post("/voice-agent/orders", status_code=201)
async def create_order(
    request: Request,
    authorization: str | None = Header(None),
):
    """Place a new order."""
    verify_auth(authorization)

    body = await request.json()
    items = body.get("items", [])

    if not items:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "No items provided in the order"},
        )

    # Calculate total from menu prices
    total = 0
    order_items = []
    for order_item in items:
        item_name = order_item.get("name", "")
        quantity = order_item.get("quantity", 1)

        # Find item in menu (by name, case-insensitive)
        menu_item = next(
            (i for i in MENU_ITEMS if i["name"].lower() == item_name.lower()),
            None,
        )

        if menu_item:
            if not menu_item["available"]:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "success": False,
                        "error": f"'{menu_item['name']}' is currently unavailable",
                    },
                )
            item_total = menu_item["price"] * quantity
            total += item_total
            order_items.append({
                "id": menu_item["id"],
                "name": menu_item["name"],
                "quantity": quantity,
                "price": menu_item["price"],
                "item_total": item_total,
                "special_instructions": order_item.get("special_instructions", ""),
            })
        else:
            # Item not found in menu — still accept but flag it
            order_items.append({
                "id": "unknown",
                "name": item_name,
                "quantity": quantity,
                "price": 0,
                "item_total": 0,
                "special_instructions": order_item.get("special_instructions", ""),
            })

    # Create the order
    order_id = _generate_order_id()
    estimated_minutes = random.randint(15, 35)
    created_at = datetime.now()

    # Insert order into Postgres
    import json
    async with engine.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO mock_orders (
                    order_id, status, items, total, customer_name, customer_phone,
                    order_type, notes, estimated_time, created_at
                ) VALUES (
                    :order_id, :status, :items, :total, :customer_name, :customer_phone,
                    :order_type, :notes, :estimated_time, :created_at
                )
            """),
            {
                "order_id": order_id,
                "status": "confirmed",
                "items": json.dumps(order_items),
                "total": total,
                "customer_name": body.get("customer_name", "Guest"),
                "customer_phone": body.get("customer_phone", ""),
                "order_type": body.get("order_type", "dine_in"),
                "notes": body.get("notes", ""),
                "estimated_time": f"{estimated_minutes} min",
                "created_at": created_at
            }
        )

    return {
        "success": True,
        "order_id": order_id,
        "total": total,
        "status": "confirmed",
        "estimated_time": f"{estimated_minutes} min",
        "items_count": len(order_items),
    }


# ──────────────────────────────────────────────────────────────
# Endpoint 4: GET /voice-agent/orders/:orderId
# ──────────────────────────────────────────────────────────────

@app.get("/voice-agent/orders/{order_id}")
async def get_order(
    order_id: str,
    authorization: str | None = Header(None),
):
    """Check the status of an existing order."""
    verify_auth(authorization)

    # Fetch order from Postgres
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT * FROM mock_orders WHERE order_id = :order_id"),
            {"order_id": order_id}
        )
        order = result.mappings().first()

    if not order:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": f"Order '{order_id}' not found"},
        )

    # Ensure items is treated as a list, since it's JSONB it comes back as list/dict automatically or string in some asyncpg setups
    import json
    items = order["items"]
    if isinstance(items, str):
        items = json.loads(items)

    return {
        "success": True,
        "order_id": order["order_id"],
        "status": order["status"],
        "items": [
            {
                "name": item["name"],
                "quantity": item["quantity"],
                "price": item["price"],
            }
            for item in items
        ],
        "total": order["total"],
        "estimated_time": order["estimated_time"],
    }


# ──────────────────────────────────────────────────────────────
# Endpoint 5: DELETE /voice-agent/orders/:orderId
# ──────────────────────────────────────────────────────────────

@app.delete("/voice-agent/orders/{order_id}")
async def cancel_order(
    order_id: str,
    request: Request,
    authorization: str | None = Header(None),
):
    """Cancel an existing order."""
    verify_auth(authorization)

    # Fetch order from Postgres
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT status FROM mock_orders WHERE order_id = :order_id"),
            {"order_id": order_id}
        )
        order = result.mappings().first()

    if not order:
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": f"Order '{order_id}' not found"},
        )

    # Check if order can be cancelled
    if order["status"] in ("preparing", "ready", "delivered"):
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": f"Order already {order['status']}, cannot cancel",
            },
        )

    if order["status"] == "cancelled":
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "Order is already cancelled"},
        )

    # Cancel the order
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    cancel_reason = body.get("reason", "Customer requested cancellation")

    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mock_orders SET status = 'cancelled', notes = :notes WHERE order_id = :order_id"),
            {"notes": cancel_reason, "order_id": order_id}
        )

    return {
        "success": True,
        "order_id": order_id,
        "status": "cancelled",
        "refund": "initiated",
    }


# ──────────────────────────────────────────────────────────────
# Endpoint 6: GET /voice-agent/payment-info
# ──────────────────────────────────────────────────────────────

@app.get("/voice-agent/payment-info")
async def get_payment_info(
    authorization: str | None = Header(None),
    restaurantId: str | None = Query(None),
):
    """Get payment information for the restaurant."""
    verify_auth(authorization)

    return {
        "success": True,
        "upi_id": "spicegarden@upi",
        "payment_methods": ["UPI", "Cash on Delivery", "Card on Delivery"],
        "instructions": "Pay via any UPI app (GPay, PhonePe, Paytm). Show payment confirmation to delivery person.",
        "minimum_order": 150,
        "delivery_charge": 30,
        "free_delivery_above": 500,
    }


# ──────────────────────────────────────────────────────────────
# Health Check
# ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint."""
    active_orders = 0
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT COUNT(*) as c FROM mock_orders WHERE status != 'cancelled'"))
            row = result.mappings().first()
            if row:
                active_orders = row["c"]
    except Exception:
        pass

    return {
        "status": "healthy",
        "restaurant": "Spice Garden",
        "menu_items": len(MENU_ITEMS),
        "active_orders": active_orders,
    }


# ──────────────────────────────────────────────────────────────
# Run Server
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Mock Restaurant API - Spice Garden")
    print("  URL:  http://localhost:9000")
    print("  Docs: http://localhost:9000/docs")
    print("=" * 60)
    print()
    print("  Endpoints:")
    print("    GET    /voice-agent/menu")
    print("    GET    /voice-agent/menu/{item_id}")
    print("    GET    /voice-agent/price?item_name=...")
    print("    POST   /voice-agent/orders")
    print("    GET    /voice-agent/orders/{order_id}")
    print("    DELETE /voice-agent/orders/{order_id}")
    print("    GET    /voice-agent/payment-info")
    print("    GET    /health")
    print()
    print("  Auth tokens (for testing): test-token-123, mock-restaurant-key, dev-token")
    print("=" * 60)

    uvicorn.run(app, host="0.0.0.0", port=9000)
