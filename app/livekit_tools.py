"""
Voice Agent Platform — LiveKit Function Tools

Bridges LiveKit's tool interface with the ActionRouter, so the LiveKit voice
agent calls the same tenant API endpoints that the REST/WebSocket pipeline uses.

The toolset is built at runtime from the tenant's ToolSpecs rather than from
decorated methods. Each spec becomes a RawFunctionTool: LiveKit is handed the
tool's JSON Schema straight from the database and calls back with the raw
arguments, which go to the ActionRouter unchanged.

This is what makes a tool added in the dashboard callable on the next call
without a deploy. It also drops a workaround the decorated version needed:
create_order used to ask the model for its items as a JSON *string* and parse
it by hand, because a Python signature cannot express an array of objects. A
raw schema can, so the model now returns a real array.

We use a class based on `llm.Toolset` to cleanly inject the tenant,
restaurant_id, and room instances into the tools without relying on global state.
"""

import json
import logging
from contextlib import nullcontext
from typing import Any, Sequence

from livekit.agents import RunContext, llm

from app.services.tool_presets import PRESET_SPECS
from app.services.tool_result import format_tool_result
from app.services.tool_spec import ToolSpec

logger = logging.getLogger("livekit-tools")


# Spoken while a fetch is in flight, so the line is not silent for a second or
# two. Keyed by tool name: a tool with no entry here simply says nothing.
#
# These are handed to RunContext.with_filler rather than spoken before the
# fetch, so the request and the filler overlap instead of queueing. See
# _execute_action for why that matters.
_MENU_FILLER_PHRASES: dict[str, dict[str, str]] = {
    "check_menu": {
        "english": "Let me fetch the latest menu details from this restaurant.",
        "tamil": "ஒரு நிமிஷம், மெனு எடுக்கிறேன்.",
        "telugu": "ఒక్క నిమిషం, మెనూ చూస్తాను.",
        "kannada": "ಒಂದು ನಿಮಿಷ, ಮೆನು ತೆಗೆದುಕೊಳ್ಳುತ್ತೇನೆ.",
        "malayalam": "ഒരു നിമിഷം, മെനു നോക്കട്ടെ.",
    },
    "get_item_details": {
        "english": "Let me look up the details for that item.",
        "tamil": "அந்த ஐட்டத்தோட details பாக்கிறேன்.",
        "telugu": "ఆ ఐటమ్ వివరాలు చూస్తాను.",
        "kannada": "ಆ ಐಟಂ ವಿವರ ತೆಗೆದುಕೊಳ್ಳುತ್ತೇನೆ.",
        "malayalam": "ആ ഐറ്റത്തിന്റെ വിശദാംശങ്ങൾ നോക്കട്ടെ.",
    },
    "get_item_price": {
        "english": "Let me check the price for that.",
        "tamil": "அதோட விலை பாக்கிறேன்.",
        "telugu": "దాని ధర చూస్తాను.",
        "kannada": "ಆ ಐಟಂ ಬೆಲೆ ನೋಡುತ್ತೇನೆ.",
        "malayalam": "അതിന്റെ വില നോക്കട്ടെ.",
    },
}

# How long the line must stay quiet before a filler is worth playing. A fetch
# that returns inside this window plays nothing, which keeps the fast path
# silent instead of padding it with a sentence the customer doesn't need.
_FILLER_DELAY_SECONDS = 0.5


class RestaurantTools(llm.Toolset):
    """
    Toolset for a tenant's operations, built from its configured tools.

    Injected with the tenant config, the specs to expose and the LiveKit room
    upon initialization.
    """

    def __init__(
        self,
        tenant: dict | None,
        restaurant_id: str | None,
        room: Any,
        language: str = "english",
        specs: Sequence[ToolSpec] | None = None,
        session_id: str | None = None,
        sub_tenant_id: str | None = None,
    ):
        self.tenant = tenant
        self.restaurant_id = restaurant_id
        self.room = room
        self.language = language

        # Tie every row this call writes (order events, and the final
        # conversation log) to one session id, so the dashboard groups them.
        self.session_id = session_id
        self.sub_tenant_id = sub_tenant_id

        # Outcome bookkeeping read by the conversation logger at session end.
        # cancelled wins over ordered (matching conversation_engine's priority),
        # and the last order's total is what the conversation row records.
        self.ordered = False
        self.cancelled = False
        self.order_value: float | None = None

        # A tenant with no configured tools falls back to the built-in
        # restaurant set, matching ToolRegistry.load_specs().
        self._specs: dict[str, ToolSpec] = {
            spec.name: spec for spec in (specs if specs is not None else PRESET_SPECS)
        }

        super().__init__(
            id="restaurant_tools",
            tools=[self._build_tool(spec) for spec in self._specs.values()],
        )

        logger.info(
            f"Toolset built with {len(self._specs)} tools: "
            f"{', '.join(sorted(self._specs)) or 'none'}"
        )

    # ──────────────────────────────────────────────────────────────
    # Dynamic tool construction
    # ──────────────────────────────────────────────────────────────

    def _build_tool(self, spec: ToolSpec) -> Any:
        """
        Wrap one spec as a LiveKit raw function tool.

        The closure captures the spec so the callback knows which tool fired;
        LiveKit passes the model's arguments through as a dict.
        """

        async def _invoke(raw_arguments: dict[str, Any], context: RunContext) -> str:
            return await self._execute_action(spec.name, raw_arguments, context)

        # A readable name in logs and traces; LiveKit takes the real name from
        # the raw schema.
        _invoke.__name__ = spec.name

        return llm.function_tool(_invoke, raw_schema=spec.to_raw_schema())

    # ──────────────────────────────────────────────────────────────
    # Execution
    # ──────────────────────────────────────────────────────────────

    def _filler_phrase(self, tool_name: str) -> str:
        """This tool's filler line in the call's language, or "" if it has none."""
        return _MENU_FILLER_PHRASES.get(tool_name, {}).get(self.language, "")

    @property
    def outcome(self) -> str:
        """The call outcome for the conversation log: cancelled > ordered > browsed."""
        if self.cancelled:
            return "cancelled"
        if self.ordered:
            return "ordered"
        return "browsed"

    async def _record_order_outcome(self, function_name: str, result: dict) -> None:
        """
        Write an order_events row for an order action and update call state.

        Only create_order / cancel_order / get_order map to an event; other
        tools (menu, price, payment info) don't, and leave the outcome at
        'browsed'. A get_order on its own is a status check, not an order, so it
        logs the event but doesn't flip the outcome.
        """
        from app.services.livekit_logging import ORDER_EVENT_TYPES, log_order_event

        if function_name == "create_order":
            self.ordered = True
            total = result.get("total")
            if total is not None:
                self.order_value = total
        elif function_name == "cancel_order":
            self.cancelled = True

        # No session id means this run can't be tied to a conversation row
        # (shouldn't happen from the agent, but keeps a direct caller safe).
        if function_name in ORDER_EVENT_TYPES and self.session_id and self.tenant:
            await log_order_event(
                tenant_id=self.tenant.get("id"),
                session_id=self.session_id,
                restaurant_id=self.restaurant_id,
                sub_tenant_id=self.sub_tenant_id,
                function_name=function_name,
                result=result,
            )

    async def _execute_action(
        self,
        function_name: str,
        arguments: dict[str, Any],
        context: RunContext | None = None,
    ) -> str:
        """Bridge a LiveKit tool call to the existing ActionRouter."""
        from app.services.action_router import ActionRouter

        async def broadcast_debug(event_type: str, details: dict):
            if self.room:
                try:
                    payload = json.dumps({"type": "debug", "event": event_type, **details}).encode()
                    await self.room.local_participant.publish_data(payload, reliable=True)
                except Exception as e:
                    logger.error(f"Failed to broadcast debug info: {e}")

        arguments = dict(arguments or {})

        # Older models sometimes hand back a JSON string where the schema asks
        # for an array or object. Recover rather than failing the call.
        for key, value in list(arguments.items()):
            if isinstance(value, str) and value[:1] in ("[", "{"):
                try:
                    arguments[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass

        await broadcast_debug("tool_call", {"function": function_name, "args": arguments})

        if not self.tenant:
            logger.warning("livekit_tool_no_tenant", extra={"function": function_name})
            await broadcast_debug("tool_result", {"function": function_name, "error": "No tenant"})
            return "I'm sorry, I couldn't process that — the restaurant connection isn't set up yet."

        router = ActionRouter()

        # Schedule the filler alongside the fetch rather than speaking it first.
        # session.say() returns a SpeechHandle whose await waits for full audio
        # playout, and the scheduler authorises one speech at a time, so awaiting
        # a filler here held the HTTP request behind the LLM's preamble *and* the
        # filler itself — several seconds before the fetch even started.
        # with_filler instead runs a background scheduler: the request goes out
        # immediately, the phrase plays only if the line is still quiet after
        # _FILLER_DELAY_SECONDS, and it stays interruptible so the customer can
        # barge in.
        phrase = self._filler_phrase(function_name)
        filler = (
            context.with_filler(phrase, delay=_FILLER_DELAY_SECONDS)
            if phrase and context is not None
            else nullcontext()
        )

        try:
            async with filler:
                result = await router.execute(
                    function_name=function_name,
                    arguments=arguments,
                    tenant=self.tenant,
                    restaurant_id=self.restaurant_id,
                    tools=self._specs,
                )

            await broadcast_debug("tool_result", {"function": function_name, "result": result})

            if result.get("success"):
                # Persist order lifecycle events and track the call outcome, so
                # the Orders/Revenue cards and the conversation's outcome badge
                # reflect what happened on this voice call.
                await self._record_order_outcome(function_name, result)

                # Format as human-readable text to prevent raw JSON
                # from leaking into TTS if the LLM echoes tool output.
                return format_tool_result(self._specs.get(function_name), result)
            else:
                error_msg = result.get("error", "Unknown error")
                return f"Sorry, there was a problem: {error_msg}"

        except Exception as e:
            logger.error("livekit_tool_error", extra={"function": function_name, "error": str(e)})
            await broadcast_debug("tool_result", {"function": function_name, "error": str(e)})
            return "I'm sorry, I encountered an error while processing that request."
