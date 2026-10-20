"""
Voice Agent Platform — LiveKit Conversation & Order Logging

The LiveKit voice path (livekit_agent.py + livekit_tools.py) is a different
pipeline from the WebSocket widget path (voice.py + conversation_engine.py).
conversation_engine.py is what writes conversation_logs and order_events, and
the LiveKit agent never calls it — so until this module existed, a voice test
produced a perfect live conversation that left no trace in the database, and
neither the Conversations tab nor the dashboard cards reflected it.

This module gives the LiveKit path its own logging, writing the same two tables
the dashboard reads:

    log_order_event    — one order_events row per create/cancel/check
    log_conversation   — one conversation_logs row per call (at session end)

Each function opens its own short-lived engine. The LiveKit worker runs the job
on its own event loop and disposes the engine between steps (see the comments in
livekit_agent.py), so a session bound to a long-lived engine would raise once
that loop moves on. Building and disposing per write mirrors what
resolve_tenant_from_metadata and load_tenant_tool_specs already do.

Logging never raises into the call: a failed insert is logged and swallowed, so
a database blip degrades analytics rather than dropping the caller mid-sentence.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from app.models.conversation_log import ConversationLog
from app.models.order_event import OrderEvent

logger = logging.getLogger("livekit-logging")


# The preset tool names that correspond to an order lifecycle event, mapped to
# the order_events.event_type they produce. Tools a tenant has renamed in the
# dashboard won't match and simply aren't logged as order events — the canonical
# names are what the restaurant API contract (and the mock API) use.
ORDER_EVENT_TYPES: dict[str, str] = {
    "create_order": "created",
    "cancel_order": "cancelled",
    "get_order": "checked",
}


def _to_uuid(value: object) -> UUID | None:
    """Coerce a metadata value to UUID, or None if it isn't one."""
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _new_session():
    """
    Build a fresh engine + session bound to the current event loop.

    Returns (session_factory, engine); the caller disposes the engine.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import create_engine

    engine = create_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return factory, engine


async def log_order_event(
    *,
    tenant_id: object,
    session_id: str,
    restaurant_id: str | None,
    sub_tenant_id: object,
    function_name: str,
    result: dict,
) -> None:
    """
    Write one order_events row for a successful order-lifecycle tool call.

    `function_name` is the tool that fired; only create_order / cancel_order /
    get_order map to an event (see ORDER_EVENT_TYPES) — anything else is a
    no-op. `result` is the restaurant API response the ActionRouter returned.

    Returns the order_value and event_type it recorded via the caller's own
    bookkeeping is not needed here — the conversation logger reads its outcome
    from the tools instance, not from this row.
    """
    event_type = ORDER_EVENT_TYPES.get(function_name)
    if event_type is None:
        return

    tid = _to_uuid(tenant_id)
    if tid is None:
        logger.warning("order_event_skipped_no_tenant", extra={"function": function_name})
        return

    # The mock/real API returns the order id as "order_id"; fall back through a
    # couple of common spellings rather than losing the row over a key name.
    order_id = (
        result.get("order_id")
        or result.get("orderId")
        or result.get("id")
        or "unknown"
    )
    order_value = result.get("total")
    items = result.get("items")

    try:
        factory, engine = _new_session()
        async with factory() as db:
            db.add(
                OrderEvent(
                    tenant_id=tid,
                    restaurant_id=restaurant_id,
                    sub_tenant_id=_to_uuid(sub_tenant_id),
                    session_id=session_id,
                    external_order_id=str(order_id),
                    event_type=event_type,
                    order_value=order_value,
                    items=items if isinstance(items, list) else None,
                )
            )
            await db.commit()
        await engine.dispose()
        logger.info(
            "order_event_logged",
            extra={"event_type": event_type, "order_id": order_id, "session_id": session_id},
        )
    except Exception as e:  # noqa: BLE001
        logger.error("order_event_log_failed", extra={"function": function_name, "error": str(e)})


async def log_conversation(
    *,
    tenant_id: object,
    session_id: str,
    restaurant_id: str | None,
    sub_tenant_id: object,
    messages: list[dict],
    outcome: str,
    order_value: float | None,
    duration_seconds: int,
    started_at: datetime | None = None,
) -> None:
    """
    Write one conversation_logs row for a finished call.

    Called once, at session end, with the whole transcript — so a single call
    is one row, not one row per turn (the per-turn duplication the WebSocket
    path has). `outcome` is ordered | cancelled | browsed.
    """
    tid = _to_uuid(tenant_id)
    if tid is None:
        logger.warning("conversation_skipped_no_tenant", extra={"session_id": session_id})
        return

    now = datetime.now(timezone.utc)
    start = started_at or now

    try:
        factory, engine = _new_session()
        async with factory() as db:
            db.add(
                ConversationLog(
                    tenant_id=tid,
                    restaurant_id=restaurant_id,
                    sub_tenant_id=_to_uuid(sub_tenant_id),
                    session_id=session_id,
                    messages=messages or [],
                    outcome=outcome,
                    order_value=order_value,
                    message_count=len(messages or []),
                    duration_seconds=max(duration_seconds, 1),
                    started_at=start,
                    ended_at=now,
                )
            )
            await db.commit()
        await engine.dispose()
        logger.info(
            "conversation_logged",
            extra={
                "session_id": session_id,
                "outcome": outcome,
                "message_count": len(messages or []),
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.error("conversation_log_failed", extra={"session_id": session_id, "error": str(e)})
