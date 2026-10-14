"""
Voice Agent Platform — Conversation Engine

The core orchestrator that processes voice messages through the full AI pipeline:

    User message
        → SessionManager (load/create session)
            → PromptBuilder (build system prompt + messages)
                → DeepSeekClient (send to AI with tools)
                    → ActionRouter (execute tool calls)
                        → Restaurant API (real or mock)
                    → DeepSeek (feed results back, loop)
                → Final AI text response
            → SessionManager (save history + cart)
        → Log to PostgreSQL (conversation + order events)
    → Return response to caller

Features:
    - Multi-turn function calling (DeepSeek calls tool → result → calls another)
    - Session management with Redis (create, load, save history)
    - Conversation logging to PostgreSQL
    - Order event tracking (created, checked, cancelled)
    - Cart state management per session
    - Configurable max tool-call loop depth (prevents infinite loops)

Usage:
    from app.services.conversation_engine import ConversationEngine

    engine = ConversationEngine(db=db_session, redis=redis_client)
    response = await engine.process_message(
        tenant=tenant,
        message="What's on the menu?",
        session_id=None,           # auto-creates new session
        restaurant_id="spice-garden",
    )
"""

import json
import time
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.logger import get_logger
from app.models.conversation_log import ConversationLog
from app.models.order_event import OrderEvent
from app.services.action_router import ActionRouter, ActionRouterError
from app.services.deepseek_client import DeepSeekClient, DeepSeekError
from app.services.prompt_builder import PromptBuilder
from app.services.session_manager import SessionManager
from app.services.tool_registry import ToolRegistry
from app.services.tool_spec import ToolSpec

logger = get_logger(__name__)

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────

MAX_TOOL_CALL_ROUNDS = 5  # Max consecutive tool-call loops before forcing a text response
MAX_TOOL_CALLS_PER_ROUND = 3  # Max parallel tool calls per AI response


class ConversationEngineError(Exception):
    """Base exception for conversation engine errors."""

    def __init__(self, message: str, session_id: str | None = None):
        super().__init__(message)
        self.session_id = session_id


class ConversationEngine:
    """
    Orchestrates the full voice conversation pipeline.

    Wires together: SessionManager, PromptBuilder, DeepSeekClient, and ActionRouter
    to process a user's message and return an AI response.
    """

    def __init__(self, db: AsyncSession, redis: Redis):
        """
        Initialize the Conversation Engine with all dependencies.

        Args:
            db: Async SQLAlchemy session for PostgreSQL logging.
            redis: Redis client for session management.
        """
        self.db = db
        self.redis = redis

        # Initialize sub-services
        self.session_manager = SessionManager(redis)
        self.prompt_builder = PromptBuilder()
        self.deepseek_client = DeepSeekClient()
        self.action_router = ActionRouter()
        self.tool_registry = ToolRegistry(db=db, redis=redis)

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    async def process_message(
        self,
        tenant: Any,
        message: str,
        session_id: str | None = None,
        restaurant_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Process a user's voice message through the full AI pipeline.

        This is the main entry point — called by the voice API route.

        Args:
            tenant: Tenant model or dict with config (name, api_base_url, etc.).
            message: The user's text message.
            session_id: Existing session ID (None = create new session).
            restaurant_id: Restaurant ID for multi-restaurant platforms.

        Returns:
            Dict with keys:
                - text (str): AI's response text.
                - session_id (str): Session ID (new or existing).
                - actions (list): Actions triggered (e.g., order_created).
                - message_count (int): Total messages in this session.

        Raises:
            ConversationEngineError: On critical failures.
        """
        start_time = time.time()
        tenant_id = self._get_tenant_field(tenant, "id")
        actions: list[dict[str, Any]] = []

        logger.info(
            "conversation_engine_processing",
            tenant_id=str(tenant_id),
            session_id=session_id,
            restaurant_id=restaurant_id,
            message_length=len(message),
        )

        # ── Step 1: Session Management ──────────────────────────
        session_id, session, is_new = await self._resolve_session(
            session_id=session_id,
            tenant_id=str(tenant_id),
        )

        # ── Step 2: Build Messages for DeepSeek ─────────────────
        history = session.get("history", [])
        messages = self.prompt_builder.build_messages(
            tenant=tenant,
            user_message=message,
            conversation_history=history,
        )

        # ── Step 3: AI Processing Loop (with tool calling) ──────
        # Resolve the tenant's enabled tools once per turn rather than per
        # round: a tool toggled mid-conversation takes effect on the next
        # message, and the model sees a stable tool list within one turn.
        tool_specs = await self.tool_registry.load_specs(tenant_id)

        ai_response_text, round_actions, total_usage = await self._ai_loop(
            messages=messages,
            tenant=tenant,
            restaurant_id=restaurant_id,
            tool_specs=tool_specs,
        )
        actions.extend(round_actions)

        # ── Step 4: Save to Session ─────────────────────────────
        # Add user message and AI response to history
        await self.session_manager.add_message(session_id, "user", message)
        await self.session_manager.add_message(session_id, "assistant", ai_response_text)

        # Update cart if any order actions occurred
        await self._update_session_cart(session_id, actions)

        # Get updated session for message count
        updated_session = await self.session_manager.get_session(session_id)
        message_count = len(updated_session.get("history", [])) if updated_session else 0

        # ── Step 5: Log to PostgreSQL ───────────────────────────
        await self._log_conversation(
            tenant_id=tenant_id,
            session_id=session_id,
            restaurant_id=restaurant_id,
            message=message,
            ai_response=ai_response_text,
            actions=actions,
            history=updated_session.get("history", []) if updated_session else [],
            duration_ms=int((time.time() - start_time) * 1000),
        )

        # Log order events if any
        await self._log_order_events(
            tenant_id=tenant_id,
            session_id=session_id,
            restaurant_id=restaurant_id,
            actions=actions,
        )

        elapsed_ms = int((time.time() - start_time) * 1000)

        logger.info(
            "conversation_engine_complete",
            tenant_id=str(tenant_id),
            session_id=session_id,
            is_new_session=is_new,
            message_count=message_count,
            action_count=len(actions),
            total_tokens=total_usage.get("total_tokens", 0),
            elapsed_ms=elapsed_ms,
        )

        return {
            "text": ai_response_text,
            "session_id": session_id,
            "restaurant_id": restaurant_id,
            "actions": actions,
            "message_count": message_count,
        }

    # ──────────────────────────────────────────────────────────────
    # AI Processing Loop (Multi-Turn Tool Calling)
    # ──────────────────────────────────────────────────────────────

    async def _ai_loop(
        self,
        messages: list[dict],
        tenant: Any,
        restaurant_id: str | None,
        tool_specs: list[ToolSpec],
    ) -> tuple[str, list[dict], dict]:
        """
        Run the DeepSeek AI loop with tool-call support.

        The loop handles multi-turn function calling:
        1. Send messages to DeepSeek with tool definitions
        2. If DeepSeek returns tool_calls → execute them via ActionRouter
        3. Append tool results to messages and call DeepSeek again
        4. Repeat until DeepSeek returns a text response (or max rounds reached)

        Returns:
            Tuple of (ai_response_text, actions_list, total_usage_dict).
        """
        actions: list[dict[str, Any]] = []
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        tool_definitions = [spec.to_openai_tool() for spec in tool_specs]
        spec_map = {spec.name: spec for spec in tool_specs}

        for round_num in range(1, MAX_TOOL_CALL_ROUNDS + 1):
            try:
                # Call DeepSeek
                ai_result = await self.deepseek_client.chat_completion(
                    messages=messages,
                    tools=tool_definitions or None,
                    tool_choice="auto",
                    temperature=0.7,
                    max_tokens=1024,
                )
            except DeepSeekError as e:
                logger.error(
                    "conversation_engine_ai_error",
                    round=round_num,
                    error=str(e),
                )
                return (
                    "I'm sorry, I'm having trouble connecting right now. "
                    "Please try again in a moment.",
                    actions,
                    total_usage,
                )

            # Accumulate token usage
            usage = ai_result.get("usage", {})
            total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
            total_usage["total_tokens"] += usage.get("total_tokens", 0)

            tool_calls = ai_result.get("tool_calls", [])

            # If no tool calls → we have the final text response
            if not tool_calls:
                response_text = ai_result.get("content", "")
                if not response_text:
                    response_text = "I'm sorry, I couldn't process that. Could you try again?"
                return response_text, actions, total_usage

            # ── Tool Call Execution ──────────────────────────────
            logger.info(
                "conversation_engine_tool_calls",
                round=round_num,
                tool_count=len(tool_calls),
                tools=[tc["function_name"] for tc in tool_calls],
            )

            # Add the assistant's tool-call message to the conversation
            # This preserves the full message structure DeepSeek expects
            assistant_msg = ai_result["message"]
            messages.append({
                "role": "assistant",
                "content": assistant_msg.get("content") or "",
                "tool_calls": assistant_msg.get("tool_calls"),
            })

            # Execute each tool call (limit per round for safety)
            for tc in tool_calls[:MAX_TOOL_CALLS_PER_ROUND]:
                tool_result, tool_action = await self._execute_tool_call(
                    function_name=tc["function_name"],
                    arguments=tc["arguments"],
                    tenant=tenant,
                    restaurant_id=restaurant_id,
                    tool_specs=spec_map,
                )

                # Add tool result as a "tool" message for DeepSeek
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_result, default=str),
                })

                # Track actions (order events)
                if tool_action:
                    actions.append(tool_action)

            logger.debug(
                "conversation_engine_round_complete",
                round=round_num,
                messages_count=len(messages),
            )

        # Max rounds reached — force a response
        logger.warning(
            "conversation_engine_max_rounds",
            max_rounds=MAX_TOOL_CALL_ROUNDS,
        )
        return (
            "I've gathered all the information. How can I help you further?",
            actions,
            total_usage,
        )

    # ──────────────────────────────────────────────────────────────
    # Tool Call Execution
    # ──────────────────────────────────────────────────────────────

    async def _execute_tool_call(
        self,
        function_name: str,
        arguments: str | dict,
        tenant: Any,
        restaurant_id: str | None,
        tool_specs: dict[str, ToolSpec],
    ) -> tuple[dict, dict | None]:
        """
        Execute a single tool call via the ActionRouter.

        Returns:
            Tuple of (tool_result_dict, action_dict_or_none).
            action_dict is populated for order-related events.
        """
        action: dict | None = None

        try:
            result = await self.action_router.execute(
                function_name=function_name,
                arguments=arguments,
                tenant=tenant,
                restaurant_id=restaurant_id,
                tools=tool_specs,
            )

            # Track order-related actions for logging
            action = self._extract_action(function_name, result)

            logger.info(
                "conversation_engine_tool_result",
                function_name=function_name,
                success=result.get("success", False),
                has_action=action is not None,
            )

            return result, action

        except ActionRouterError as e:
            logger.error(
                "conversation_engine_tool_error",
                function_name=function_name,
                error=str(e),
            )
            return {
                "success": False,
                "error": f"Failed to execute {function_name}: {str(e)}",
            }, None

        except Exception as e:
            logger.error(
                "conversation_engine_unexpected_tool_error",
                function_name=function_name,
                error_type=type(e).__name__,
                error=str(e),
            )
            return {
                "success": False,
                "error": "An unexpected error occurred while processing your request.",
            }, None

    def _extract_action(self, function_name: str, result: dict) -> dict | None:
        """
        Extract a trackable action from a tool call result.

        Only order-related functions produce actions:
        - create_order → action type "order_created"
        - get_order    → action type "order_checked"
        - cancel_order → action type "order_cancelled"
        """
        if not result.get("success"):
            return None

        if function_name == "create_order":
            order_data = result.get("order", result)
            return {
                "type": "order_created",
                "data": {
                    "order_id": order_data.get("order_id"),
                    "total": order_data.get("total"),
                    "status": order_data.get("status"),
                    "estimated_time": order_data.get("estimated_time", order_data.get("estimated_prep_time_mins")),
                    "items_count": len(order_data.get("details", {}).get("items", [])),
                },
            }

        if function_name == "get_order":
            order_data = result.get("order", result)
            return {
                "type": "order_checked",
                "data": {
                    "order_id": order_data.get("order_id"),
                    "status": order_data.get("status"),
                    "total": order_data.get("total"),
                },
            }

        if function_name == "cancel_order":
            order_data = result.get("order", result)
            return {
                "type": "order_cancelled",
                "data": {
                    "order_id": order_data.get("order_id"),
                    "status": order_data.get("status", "cancelled"),
                    "refund": order_data.get("refund"),
                },
            }

        return None

    # ──────────────────────────────────────────────────────────────
    # Session Management
    # ──────────────────────────────────────────────────────────────

    async def _resolve_session(
        self,
        session_id: str | None,
        tenant_id: str,
    ) -> tuple[str, dict, bool]:
        """
        Resolve or create a session.

        Returns:
            Tuple of (session_id, session_data, is_new_session).
        """
        is_new = False

        if session_id:
            session = await self.session_manager.get_session(session_id)
            if session:
                logger.debug("session_resumed", session_id=session_id)
                return session_id, session, False
            else:
                # Session expired or invalid — create a new one
                logger.info(
                    "session_expired_creating_new",
                    old_session_id=session_id,
                    tenant_id=tenant_id,
                )

        # Create new session
        session_id = await self.session_manager.create_session(tenant_id)
        session = await self.session_manager.get_session(session_id)
        is_new = True

        logger.info("session_created_for_engine", session_id=session_id, tenant_id=tenant_id)
        return session_id, session or {}, is_new

    async def _update_session_cart(
        self,
        session_id: str,
        actions: list[dict],
    ) -> None:
        """Update session cart state based on order actions."""
        for action in actions:
            if action["type"] == "order_created":
                order_data = action["data"]
                await self.session_manager.update_session(session_id, {
                    "cart": {
                        "order_id": order_data.get("order_id"),
                        "total": order_data.get("total", 0),
                        "status": order_data.get("status", "confirmed"),
                        "items_count": order_data.get("items_count", 0),
                    },
                    "status": "ordered",
                })

            elif action["type"] == "order_cancelled":
                await self.session_manager.update_session(session_id, {
                    "cart": {"items": [], "total": 0},
                    "status": "cancelled",
                })

    # ──────────────────────────────────────────────────────────────
    # PostgreSQL Logging
    # ──────────────────────────────────────────────────────────────

    async def _log_conversation(
        self,
        tenant_id: Any,
        session_id: str,
        restaurant_id: str | None,
        message: str,
        ai_response: str,
        actions: list[dict],
        history: list[dict],
        duration_ms: int,
    ) -> None:
        """
        Log the conversation to PostgreSQL.

        Creates or updates a ConversationLog entry for this session.
        """
        try:
            # Determine outcome based on actions
            outcome = self._determine_outcome(actions)

            # Calculate order value if any
            order_value = None
            for action in actions:
                if action["type"] == "order_created":
                    order_value = action["data"].get("total")
                    break

            log_entry = ConversationLog(
                tenant_id=tenant_id,
                restaurant_id=restaurant_id,
                session_id=session_id,
                messages=history,
                outcome=outcome,
                order_value=order_value,
                message_count=len(history),
                duration_seconds=max(duration_ms // 1000, 1),
                started_at=datetime.now(timezone.utc),
            )

            self.db.add(log_entry)
            await self.db.flush()

            logger.debug(
                "conversation_logged",
                session_id=session_id,
                outcome=outcome,
                message_count=len(history),
            )

        except Exception as e:
            logger.error(
                "conversation_log_failed",
                session_id=session_id,
                error=str(e),
            )
            # Don't raise — logging failure shouldn't break the conversation

    async def _log_order_events(
        self,
        tenant_id: Any,
        session_id: str,
        restaurant_id: str | None,
        actions: list[dict],
    ) -> None:
        """
        Log order-related events to PostgreSQL.

        Creates OrderEvent entries for order_created, order_checked, order_cancelled.
        """
        for action in actions:
            try:
                event_type_map = {
                    "order_created": "created",
                    "order_checked": "checked",
                    "order_cancelled": "cancelled",
                }

                event_type = event_type_map.get(action["type"])
                if not event_type:
                    continue

                data = action.get("data", {})
                order_id = data.get("order_id", "unknown")

                event = OrderEvent(
                    tenant_id=tenant_id,
                    restaurant_id=restaurant_id,
                    session_id=session_id,
                    external_order_id=order_id,
                    event_type=event_type,
                    order_value=data.get("total"),
                    items=data.get("items"),
                )

                self.db.add(event)
                await self.db.flush()

                logger.info(
                    "order_event_logged",
                    session_id=session_id,
                    event_type=event_type,
                    order_id=order_id,
                )

            except Exception as e:
                logger.error(
                    "order_event_log_failed",
                    session_id=session_id,
                    action_type=action.get("type"),
                    error=str(e),
                )
                # Don't raise — logging failure shouldn't break the conversation

    @staticmethod
    def _determine_outcome(actions: list[dict]) -> str:
        """
        Determine the conversation outcome based on actions taken.

        Priority: cancelled > ordered > browsed
        """
        has_order = any(a["type"] == "order_created" for a in actions)
        has_cancel = any(a["type"] == "order_cancelled" for a in actions)

        if has_cancel:
            return "cancelled"
        if has_order:
            return "ordered"
        return "browsed"

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _get_tenant_field(tenant: Any, field: str, default=None) -> Any:
        """Get a field from tenant (model or dict)."""
        if isinstance(tenant, dict):
            return tenant.get(field, default)
        return getattr(tenant, field, default)
