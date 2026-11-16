"""
Voice Agent Platform — Middleware Registry

Re-exports all middleware dependencies for convenient imports.

Usage:
    from app.middleware import get_current_user, get_current_account
"""

from app.middleware.auth import get_current_user, require_role
from app.middleware.account_resolver import get_current_account
from app.middleware.rate_limiter import (
    check_message_rate_limit,
    check_order_rate_limit,
    check_rate_limit_for_account,
    RateLimitType,
)

__all__ = [
    # Auth (Day 4)
    "get_current_user",
    "require_role",
    # Account Resolver (Day 5)
    "get_current_account",
    # Rate Limiter (Day 5)
    "check_message_rate_limit",
    "check_order_rate_limit",
    "check_rate_limit_for_account",
    "RateLimitType",
]
