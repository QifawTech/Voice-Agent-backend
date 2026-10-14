"""
Voice Agent Platform — Schema Registry

Re-exports all Pydantic schemas for convenient imports.

Usage:
    from app.schemas import TenantCreate, TenantResponse, UserLogin, TokenResponse
"""

from app.schemas.analytics import (
    AnalyticsOverview,
    ConversationLogListResponse,
    ConversationLogResponse,
    DailyAnalyticsListResponse,
    DailyAnalyticsResponse,
    OrderEventListResponse,
    OrderEventResponse,
)
from app.schemas.auth import (
    TokenResponse,
    UserLogin,
    UserRegister,
    UserResponse,
)
from app.schemas.tenant import (
    TenantCreate,
    TenantListResponse,
    TenantResponse,
    TenantUpdate,
)
from app.schemas.voice import (
    SessionResponse,
    SupportedLanguagesResponse,
    VoiceAction,
    VoiceAudioRequest,
    VoiceAudioResponse,
    VoiceMessageRequest,
    VoiceMessageResponse,
)

__all__ = [
    # Tenant
    "TenantCreate",
    "TenantUpdate",
    "TenantResponse",
    "TenantListResponse",
    # Auth
    "UserRegister",
    "UserLogin",
    "UserResponse",
    "TokenResponse",
    # Voice
    "VoiceMessageRequest",
    "VoiceMessageResponse",
    "VoiceAudioRequest",
    "VoiceAudioResponse",
    "VoiceAction",
    "SessionResponse",
    "SupportedLanguagesResponse",
    # Analytics
    "ConversationLogResponse",
    "ConversationLogListResponse",
    "OrderEventResponse",
    "OrderEventListResponse",
    "DailyAnalyticsResponse",
    "DailyAnalyticsListResponse",
    "AnalyticsOverview",
]
