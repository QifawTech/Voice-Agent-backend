"""
Voice Agent Platform — Rate Limiter Middleware

Redis-based sliding window rate limiter for tenant API requests.
Enforces per-tenant limits to prevent abuse:
    - Messages: 100 per minute
    - Orders: 10 per minute

Uses Redis sorted sets with timestamps for a true sliding window
implementation (more accurate than fixed-window counters).

Usage:
    from app.middleware.rate_limiter import check_rate_limit, RateLimitType

    @router.post("/voice/message")
    async def voice_message(
        tenant: Tenant = Depends(get_current_tenant),
        _rate_limit: None = Depends(check_message_rate_limit),
    ):
        ...
"""

import time
from enum import Enum
from uuid import UUID

from fastapi import Depends, HTTPException, Header, status
from redis.asyncio import Redis

from app.logger import get_logger
from app.redis_client import get_redis

logger = get_logger(__name__)


class RateLimitType(str, Enum):
    """Types of rate-limited operations."""

    MESSAGE = "msg"
    ORDER = "order"


# Rate limit configurations: (max_requests, window_seconds)
RATE_LIMITS = {
    RateLimitType.MESSAGE: (100, 60),   # 100 messages per minute
    RateLimitType.ORDER: (10, 60),      # 10 orders per minute
}


async def _check_rate_limit(
    tenant_id: str,
    limit_type: RateLimitType,
    redis: Redis,
) -> None:
    """
    Check if a tenant has exceeded their rate limit.

    Uses a Redis sorted set as a sliding window:
    1. Remove entries older than the window
    2. Count remaining entries
    3. If under limit, add the current timestamp
    4. If over limit, raise 429 Too Many Requests

    Args:
        tenant_id: The tenant UUID string.
        limit_type: The type of operation being rate-limited.
        redis: The Redis connection.

    Raises:
        HTTPException 429: If rate limit is exceeded.
    """
    max_requests, window_seconds = RATE_LIMITS[limit_type]

    # Redis key: rate_limit:{type}:{tenant_id}
    key = f"rate_limit:{limit_type.value}:{tenant_id}"

    now = time.time()
    window_start = now - window_seconds

    # Use a pipeline for atomicity
    pipe = redis.pipeline()

    # 1. Remove entries older than the window
    pipe.zremrangebyscore(key, 0, window_start)

    # 2. Count entries in the current window
    pipe.zcard(key)

    # 3. Add current timestamp (score = timestamp, member = unique timestamp)
    pipe.zadd(key, {f"{now}": now})

    # 4. Set expiry on the key (cleanup if tenant goes idle)
    pipe.expire(key, window_seconds + 10)

    results = await pipe.execute()

    current_count = results[1]  # zcard result

    if current_count >= max_requests:
        # Remove the entry we just added (we're rejecting this request)
        await redis.zrem(key, f"{now}")

        retry_after = int(window_seconds - (now - window_start))

        logger.warning(
            "rate_limit_exceeded",
            tenant_id=tenant_id,
            limit_type=limit_type.value,
            current_count=current_count,
            max_requests=max_requests,
            retry_after=retry_after,
        )

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "Rate limit exceeded",
                "limit_type": limit_type.value,
                "max_requests": max_requests,
                "window_seconds": window_seconds,
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    logger.debug(
        "rate_limit_check_passed",
        tenant_id=tenant_id,
        limit_type=limit_type.value,
        current_count=current_count + 1,
        max_requests=max_requests,
    )


async def check_message_rate_limit(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    redis: Redis = Depends(get_redis),
) -> None:
    """
    FastAPI dependency: enforce message rate limit (100/min per tenant).

    Use on voice message endpoints:
        @router.post("/voice/message")
        async def send_message(
            _rate_limit: None = Depends(check_message_rate_limit),
        ):
            ...
    """
    if x_tenant_id:
        await _check_rate_limit(x_tenant_id, RateLimitType.MESSAGE, redis)


async def check_order_rate_limit(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    redis: Redis = Depends(get_redis),
) -> None:
    """
    FastAPI dependency: enforce order rate limit (10/min per tenant).

    Use on order creation endpoints:
        @router.post("/voice/order")
        async def create_order(
            _rate_limit: None = Depends(check_order_rate_limit),
        ):
            ...
    """
    if x_tenant_id:
        await _check_rate_limit(x_tenant_id, RateLimitType.ORDER, redis)


async def check_rate_limit_for_tenant(
    tenant_id: UUID,
    limit_type: RateLimitType,
    redis: Redis,
) -> None:
    """
    Programmatic rate limit check (not a FastAPI dependency).

    Use this when you have the tenant_id directly (e.g., inside the
    conversation engine) instead of extracting it from headers.

    Args:
        tenant_id: The tenant UUID.
        limit_type: MESSAGE or ORDER.
        redis: The Redis connection.
    """
    await _check_rate_limit(str(tenant_id), limit_type, redis)
