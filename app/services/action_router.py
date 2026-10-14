"""
Voice Agent Platform — Action Router

Routes a model's function calls to real HTTP requests against the tenant's API.
Acts as the bridge between AI decisions and real-world actions.

The Action Router:
    1. Receives a tool call (function_name + arguments)
    2. Looks up that tool's spec among the ones the tenant has enabled
    3. Builds the request from the spec's templates and the call's arguments
    4. Makes the HTTP request with tenant auth
    5. Returns the result back to the conversation engine

There is no per-tool handler any more. Which tools exist, where they point and
what they send all come from the tenant's rows in tenant_tools, resolved into
ToolSpecs by ToolRegistry and passed in by the caller. A tenant can add a tool
from the dashboard and this file does not change.

Callers that have no registry to hand — the standalone scripts, and anything
mid-migration — can omit `tools` and get the built-in restaurant presets, which
is the behaviour the seven hardcoded handlers used to have.

Usage:
    from app.services.action_router import ActionRouter

    router = ActionRouter()
    result = await router.execute(
        function_name="check_menu",
        arguments={"category": "starters"},
        tenant=tenant,
        restaurant_id="rest_001",
        tools=await registry.load_spec_map(tenant.id),
    )
"""

import json
from typing import Any, Sequence

import httpx

from app.logger import get_logger
from app.services.security import decrypt_token, looks_encrypted
from app.services.tool_presets import PRESET_SPEC_MAP
from app.services.tool_spec import ToolSpec, ToolTemplateError, build_request

logger = get_logger(__name__)

# HTTP client configuration
REQUEST_TIMEOUT = 15.0  # seconds per restaurant API call
MAX_RETRIES = 2
RETRY_DELAY = 0.5  # seconds

# Custom headers sent with every restaurant API request
VOICE_AGENT_HEADER = "qifaw/1.0"


class ActionRouterError(Exception):
    """Base exception for Action Router errors."""

    def __init__(
        self,
        message: str,
        function_name: str | None = None,
        status_code: int | None = None,
        original_error: Exception | None = None,
    ):
        super().__init__(message)
        self.function_name = function_name
        self.status_code = status_code
        self.original_error = original_error


class ActionRouter:
    """
    Routes tool calls to the tenant's API endpoints.

    Every call follows the same path:
    1. Resolve the tool name against the tenant's enabled specs
    2. Build URL, query, body and headers from the spec's templates
    3. Add auth headers (decrypted Bearer token)
    4. Make the async HTTP request
    5. Return a structured result dict

    The result dict is always in the format:
        {"success": True/False, ...data or error...}
    so the model can incorporate it into its response.
    """

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    async def execute(
        self,
        function_name: str,
        arguments: str | dict,
        tenant: Any,
        restaurant_id: str | None = None,
        tools: dict[str, ToolSpec] | Sequence[ToolSpec] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a tool call against the tenant's API.

        This is the main entry point — called by the Conversation Engine and by
        the LiveKit toolset whenever the model returns a tool call.

        Args:
            function_name: The tool name the model emitted (e.g., "check_menu").
            arguments: Function arguments as a JSON string or dict.
            tenant: The Tenant model/dict with api_base_url and api_auth_token.
            restaurant_id: Sub-tenant slug, for multi-restaurant platforms.
            tools: The tenant's enabled specs. Defaults to the built-in presets.

        Returns:
            Result dict with at minimum {"success": bool, ...}.

        Raises:
            ActionRouterError: On an unknown function name.
        """
        spec_map = _as_spec_map(tools)

        spec = spec_map.get(function_name)
        if spec is None:
            logger.error(
                "action_router_invalid_function",
                function_name=function_name,
                valid_functions=sorted(spec_map),
            )
            raise ActionRouterError(
                f"Unknown function: {function_name}",
                function_name=function_name,
            )

        # Parse arguments if JSON string
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as e:
                logger.error(
                    "action_router_invalid_arguments",
                    function_name=function_name,
                    arguments=arguments,
                    error=str(e),
                )
                return {
                    "success": False,
                    "error": f"Invalid arguments format: {str(e)}",
                }

        arguments = dict(arguments or {})

        # Validate tenant API config
        api_base_url = self._get_api_base_url(tenant)
        if not api_base_url:
            logger.warning(
                "action_router_no_api_url",
                function_name=function_name,
                tenant_id=self._get_tenant_id(tenant),
            )
            return {
                "success": False,
                "error": "Restaurant API is not configured for this tenant.",
            }

        # Every tool gets restaurant_id for free, whether or not it declares it
        # — it identifies which sub-tenant of a platform account is being served.
        if restaurant_id:
            arguments["restaurant_id"] = restaurant_id

        logger.info(
            "action_router_executing",
            function_name=function_name,
            tenant_id=self._get_tenant_id(tenant),
            restaurant_id=restaurant_id,
            argument_keys=list(arguments.keys()) if arguments else [],
        )

        try:
            request = build_request(
                spec=spec,
                api_base_url=api_base_url,
                args=arguments,
                base_headers=self._build_headers(tenant),
            )
        except ToolTemplateError as e:
            # The tool is configured in a way this call cannot satisfy — a
            # missing required path value, or a template that escapes the base
            # URL. Surfaced as a failed call rather than an exception so the
            # model can ask the customer for what is missing.
            logger.error(
                "action_router_template_error",
                function_name=function_name,
                tenant_id=self._get_tenant_id(tenant),
                error=str(e),
            )
            return {
                "success": False,
                "error": f"The '{function_name}' tool is misconfigured: {e}",
            }

        result = await self._make_request(
            method=request.method,
            url=request.url,
            headers=request.headers,
            params=request.params or None,
            json_data=request.json_body,
        )

        logger.info(
            "action_router_result",
            function_name=function_name,
            tenant_id=self._get_tenant_id(tenant),
            success=result.get("success", False),
        )

        return result

    # ──────────────────────────────────────────────────────────────
    # HTTP Client — Core Request Method
    # ──────────────────────────────────────────────────────────────

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: dict,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict[str, Any]:
        """
        Make an HTTP request to the restaurant's API with error handling.

        Handles:
            - Timeout errors (restaurant API too slow)
            - Connection errors (restaurant API unreachable)
            - HTTP error responses (4xx, 5xx)
            - Invalid JSON responses

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE).
            url: Full request URL.
            headers: Request headers (including auth).
            params: Query parameters.
            json_data: JSON request body.

        Returns:
            Parsed JSON response dict, or error dict on failure.
        """
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                )

            response_time_ms = int(response.elapsed.total_seconds() * 1000)

            # Log the request
            logger.debug(
                "action_router_http_request",
                method=method,
                url=url,
                status_code=response.status_code,
                response_time_ms=response_time_ms,
            )

            # Parse response
            if response.status_code in (200, 201):
                try:
                    data = response.json()
                    # A tool may point at an endpoint that answers with a list
                    # or a bare value; wrap it so the result is always a dict.
                    if not isinstance(data, dict):
                        data = {"data": data}
                    # Ensure success flag exists
                    if "success" not in data:
                        data["success"] = True
                    data["_response_time_ms"] = response_time_ms
                    return data
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "action_router_invalid_json_response",
                        method=method,
                        url=url,
                        status_code=response.status_code,
                        body_preview=response.text[:200],
                    )
                    return {
                        "success": False,
                        "error": "Restaurant API returned an invalid response format.",
                    }

            # Handle specific HTTP error codes
            return self._handle_http_error(response, method, url)

        except httpx.TimeoutException:
            logger.warning(
                "action_router_timeout",
                method=method,
                url=url,
                timeout_seconds=REQUEST_TIMEOUT,
            )
            return {
                "success": False,
                "error": "The restaurant's system is taking too long to respond. Please try again.",
            }

        except httpx.ConnectError:
            logger.warning(
                "action_router_connection_error",
                method=method,
                url=url,
            )
            return {
                "success": False,
                "error": "Unable to connect to the restaurant's system. It may be temporarily unavailable.",
            }

        except Exception as e:
            logger.error(
                "action_router_unexpected_error",
                method=method,
                url=url,
                error_type=type(e).__name__,
                error=str(e),
            )
            return {
                "success": False,
                "error": "An unexpected error occurred while contacting the restaurant.",
            }

    def _handle_http_error(
        self, response: httpx.Response, method: str, url: str
    ) -> dict[str, Any]:
        """Map HTTP error status codes to user-friendly error messages."""
        status = response.status_code

        # Try to extract error message from response body
        error_detail = None
        try:
            body = response.json()
            error_detail = body.get("error") or body.get("message") or body.get("detail")
        except (json.JSONDecodeError, ValueError, AttributeError):
            error_detail = None

        logger.warning(
            "action_router_http_error",
            method=method,
            url=url,
            status_code=status,
            error_detail=error_detail,
        )

        # User-friendly error messages per status code
        error_messages = {
            400: error_detail or "The restaurant couldn't process this request. Please check your input.",
            401: "Authentication with the restaurant's system failed. The admin should reconfigure the API connection.",
            403: "Access to the restaurant's system was denied.",
            404: "The requested information was not found in the restaurant's system.",
            422: error_detail or "The request data was invalid. Please try again with different input.",
            429: "The restaurant's system is busy. Please wait a moment and try again.",
            500: "The restaurant's system encountered an internal error. Please try again.",
            502: "The restaurant's system is temporarily unreachable.",
            503: "The restaurant's system is currently under maintenance. Please try again later.",
        }

        error_msg = error_messages.get(
            status,
            f"The restaurant's system returned an error (status {status}).",
        )

        return {
            "success": False,
            "error": error_msg,
            "status_code": status,
        }

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    def _build_headers(self, tenant: Any) -> dict[str, str]:
        """
        Build HTTP headers for the restaurant API request.

        Includes:
            - Content-Type: application/json
            - Authorization: Bearer {decrypted_token} (if available)
            - X-Voice-Agent: qifaw/1.0 (identifies our platform)

        A tool's own headers_template is merged on top of these by
        build_request(), which refuses to overwrite the auth header.
        """
        headers = {
            "Content-Type": "application/json",
            "X-Voice-Agent": VOICE_AGENT_HEADER,
        }

        # Get encrypted auth token from tenant
        auth_token = self._get_tenant_attr(tenant, "api_auth_token")
        if auth_token:
            try:
                decrypted = decrypt_token(auth_token)
                headers["Authorization"] = f"Bearer {decrypted}"
            except Exception as e:
                # Name the cause: a never-encrypted value needs the token
                # re-saved, while an unopenable ciphertext points at a changed
                # ENCRYPTION_KEY. Previously both vanished into one line.
                logger.warning(
                    "action_router_token_decrypt_failed",
                    tenant_id=self._get_tenant_id(tenant),
                    reason=(
                        "token_not_encrypted"
                        if not looks_encrypted(auth_token)
                        else "decrypt_failed"
                    ),
                    error=str(e),
                )
                # Proceed without auth — the request will likely fail with 401,
                # which is handled gracefully in _handle_http_error

        return headers

    def _get_api_base_url(self, tenant: Any) -> str | None:
        """Extract api_base_url from tenant (model or dict), stripping trailing slash."""
        url = self._get_tenant_attr(tenant, "api_base_url")
        return url.rstrip("/") if url else None

    def _get_tenant_id(self, tenant: Any) -> str:
        """Extract tenant ID as string from tenant model or dict."""
        return str(self._get_tenant_attr(tenant, "id") or "unknown")

    @staticmethod
    def _get_tenant_attr(tenant: Any, attr: str) -> Any:
        """
        Get an attribute from a tenant, supporting both model objects and dicts.

        This makes the router work with both SQLAlchemy Tenant models
        and plain dicts (e.g., from Redis cache).
        """
        if isinstance(tenant, dict):
            return tenant.get(attr)
        return getattr(tenant, attr, None)


def _as_spec_map(
    tools: dict[str, ToolSpec] | Sequence[ToolSpec] | None,
) -> dict[str, ToolSpec]:
    """Accept specs as a map or a list, and fall back to the built-in presets."""
    if tools is None:
        return PRESET_SPEC_MAP
    if isinstance(tools, dict):
        return tools
    return {spec.name: spec for spec in tools}
