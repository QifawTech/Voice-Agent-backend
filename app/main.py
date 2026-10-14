"""
Voice Agent Platform — FastAPI Application Entry Point

Main application module that:
- Creates the FastAPI app with metadata
- Configures lifespan events (startup/shutdown)
- Sets up CORS middleware
- Registers the /health endpoint
- Mounts route modules (added in later sprints)
"""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import close_database, verify_database_connection
from app.logger import get_logger, setup_logging
from app.redis_client import close_redis, init_redis, verify_redis_connection

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.

    Startup:
        - Initialize structured logging
        - Verify PostgreSQL connection
        - Initialize and verify Redis connection

    Shutdown:
        - Close database engine and connection pool
        - Close Redis connection pool
    """
    settings = get_settings()

    # ---- Startup ----
    setup_logging(debug=settings.DEBUG)
    logger.info(
        "application_starting",
        app_name=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
    )

    # Verify database
    db_ok = await verify_database_connection()
    if not db_ok:
        logger.error("startup_failed", reason="Cannot connect to PostgreSQL")
        # We don't crash — the health endpoint will report the issue

    # Initialize and verify Redis
    await init_redis()
    redis_ok = await verify_redis_connection()
    if not redis_ok:
        logger.error("startup_failed", reason="Cannot connect to Redis")

    logger.info(
        "application_started",
        database="connected" if db_ok else "disconnected",
        redis="connected" if redis_ok else "disconnected",
        host=settings.HOST,
        port=settings.PORT,
    )

    yield

    # ---- Shutdown ----
    logger.info("application_shutting_down")
    await close_database()
    await close_redis()
    logger.info("application_stopped")


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    Returns a fully configured FastAPI app with:
    - Metadata (title, description, version)
    - CORS middleware
    - Health endpoint
    - Lifespan events
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        description=(
            "AI-powered voice ordering agent platform for restaurants. "
            "Enables customers to browse menus, place orders, and check "
            "order status via natural voice conversations."
        ),
        version=settings.APP_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ---- CORS Middleware ----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Widget CORS ----
    # The widget runs on our customers' own websites, whose addresses we do not
    # know and should never have to know: requiring each one in CORS_ORIGINS
    # would mean an env edit and a restart to onboard every new customer.
    #
    # So for the widget's own routes the answer to "may this page call you?" is
    # decided by the credentials the request carries (X-Tenant-ID + X-API-Key),
    # not by where it came from. An Origin header is self-reported and proves
    # nothing; the API key is the real check, and it still applies.
    #
    # Everything else -- tenants, tools, analytics, auth -- keeps the strict
    # allowlist above, so the dashboard's surface is unchanged.
    #
    # Next step when it is wanted: an allowed-origins list per tenant, set by
    # the customer in the dashboard and checked here, the way an API key can be
    # restricted to one site. That belongs to the tenant, not to this file.
    WIDGET_PATHS = ("/api/v1/livekit/token", "/api/v1/voice", "/widget.js", "/livekit-client.js")

    @app.middleware("http")
    async def widget_cors(request: Request, call_next):
        origin = request.headers.get("origin")
        is_widget_path = request.url.path.startswith(WIDGET_PATHS)

        if not origin or not is_widget_path:
            return await call_next(request)

        # Answer the browser's permission question ourselves, before the strict
        # CORSMiddleware below rejects an origin it has never heard of.
        if request.method == "OPTIONS":
            response = Response(status_code=200)
        else:
            response = await call_next(request)

        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            request.headers.get("access-control-request-headers")
            or "Content-Type, X-Tenant-ID, X-API-Key, X-Sub-Tenant-ID"
        )
        response.headers["Access-Control-Max-Age"] = "600"
        # No Allow-Credentials: the widget authenticates with headers, never
        # cookies, and echoing an arbitrary origin with credentials enabled is
        # exactly the combination browsers forbid.
        response.headers["Vary"] = "Origin"
        return response

    # ---- Health Check ----
    @app.get(
        "/health",
        tags=["System"],
        summary="Health Check",
        description="Returns the health status of the application and its dependencies.",
    )
    async def health_check():
        """
        Health check endpoint.

        Returns the connection status of PostgreSQL and Redis,
        along with application metadata.
        """
        db_ok = await verify_database_connection()
        redis_ok = await verify_redis_connection()

        status = "ok" if (db_ok and redis_ok) else "degraded"

        return {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "services": {
                "database": "connected" if db_ok else "disconnected",
                "redis": "connected" if redis_ok else "disconnected",
            },
        }

    # ---- Root Endpoint ----
    @app.get(
        "/",
        tags=["System"],
        summary="API Root",
        description="Returns basic API information.",
    )
    async def root():
        return {
            "name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "docs": "/docs",
            "health": "/health",
        }

    # ---- LiveKit browser SDK ----
    # Vendored rather than pulled from a CDN: the widget runs on customers'
    # shop pages, so the version can never shift underneath them and the
    # widget keeps working without reaching a third party.
    @app.get("/livekit-client.js", include_in_schema=False)
    async def livekit_sdk():
        from fastapi.responses import FileResponse
        import os

        sdk_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "Voice-Widget",
            "livekit-client.umd.js",
        )
        if not os.path.exists(sdk_path):
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="LiveKit SDK not found")
        return FileResponse(
            sdk_path,
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # ---- Widget Script ----
    # The embed snippet generated for every tenant points here. Without this
    # route that snippet 404s, so no customer could ever load the widget.
    # The file lives beside the backend, in the Voice-Widget folder.
    @app.get("/widget.js", include_in_schema=False)
    async def widget_script():
        from fastapi.responses import FileResponse
        import os

        widget_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "Voice-Widget",
            "voice-widget.js",
        )
        if not os.path.exists(widget_path):
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Widget script not found")
        return FileResponse(
            widget_path,
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=300"},
        )

    # ---- LiveKit Test Page (development only) ----
    if settings.is_development:
        from fastapi.responses import FileResponse
        import os

        @app.get("/test-livekit", include_in_schema=False)
        async def test_livekit_page():
            html_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "test_livekit.html")
            return FileResponse(html_path, media_type="text/html")

    # ---- Register Route Modules ----
    from app.routes.auth import router as auth_router
    from app.routes.tenant import router as tenant_router
    from app.routes.sub_tenant import router as sub_tenant_router
    from app.routes.tenant_tool import router as tenant_tool_router
    from app.routes.voice import router as voice_router
    from app.routes.analytics import router as analytics_router
    from app.routes.livekit import router as livekit_router

    app.include_router(auth_router)
    app.include_router(tenant_router)
    app.include_router(sub_tenant_router)
    app.include_router(tenant_tool_router)
    app.include_router(voice_router)
    app.include_router(analytics_router)
    app.include_router(livekit_router)

    logger.info("fastapi_app_created", routes=len(app.routes))

    return app


# Create the app instance — this is what uvicorn imports
app = create_app()
