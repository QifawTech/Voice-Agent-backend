"""
Voice Agent Platform — Redis Client

Async Redis connection for session management, caching, and rate limiting.
Uses redis-py with hiredis parser for maximum performance.

Usage:
    from app.redis_client import get_redis

    @router.get("/example")
    async def example(redis: Redis = Depends(get_redis)):
        await redis.set("key", "value", ex=300)
        value = await redis.get("key")
"""

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)

# Module-level Redis connection pool
_redis_pool: Redis | None = None


async def init_redis() -> Redis:
    """
    Initialize the Redis connection pool.

    Creates a connection pool with:
    - max_connections=50: Handle concurrent session operations
    - decode_responses=True: Auto-decode bytes to strings
    - socket_timeout=5: Fail fast on unresponsive Redis

    Called during application startup.
    """
    global _redis_pool

    settings = get_settings()

    _redis_pool = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=50,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )

    logger.info("redis_pool_initialized", url=settings.REDIS_URL)
    return _redis_pool


async def get_redis() -> AsyncGenerator[Redis, None]:
    """
    FastAPI dependency that provides a Redis connection.

    Usage:
        @router.get("/session")
        async def get_session(redis: Redis = Depends(get_redis)):
            data = await redis.hgetall("session:sess_001")
    """
    if _redis_pool is None:
        raise RuntimeError("Redis pool not initialized. Call init_redis() first.")
    yield _redis_pool


def get_redis_sync() -> Redis:
    """
    Get the Redis connection pool directly (non-generator).

    Useful for non-FastAPI contexts where you can't use Depends().
    """
    if _redis_pool is None:
        raise RuntimeError("Redis pool not initialized. Call init_redis() first.")
    return _redis_pool


async def verify_redis_connection() -> bool:
    """
    Verify that Redis is reachable.

    Used during application startup and health checks.
    Returns True if connection succeeds, False otherwise.
    """
    try:
        if _redis_pool is None:
            await init_redis()

        pong = await _redis_pool.ping()
        if pong:
            logger.info("redis_connection_verified")
            return True
        return False
    except Exception as e:
        logger.error("redis_connection_failed", error=str(e))
        return False


async def close_redis() -> None:
    """Close the Redis connection pool. Called on app shutdown."""
    global _redis_pool

    if _redis_pool is not None:
        await _redis_pool.close()
        _redis_pool = None
        logger.info("redis_pool_closed")
