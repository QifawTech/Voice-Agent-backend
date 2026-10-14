"""
Voice Agent Platform — Services Registry

Re-exports all service modules for convenient imports.
"""

from app.services.security import (
    create_access_token,
    decrypt_token,
    encrypt_token,
    generate_api_key,
    hash_password,
    verify_access_token,
    verify_password,
)
from app.services.tenant_service import TenantService
from app.services.deepseek_client import DeepSeekClient, DeepSeekError
from app.services.prompt_builder import PromptBuilder
from app.services.tool_definitions import TOOL_DEFINITIONS, TOOL_MAP, VALID_TOOL_NAMES
from app.services.tool_presets import PRESETS, PRESET_SPECS
from app.services.tool_registry import ToolConflictError, ToolRegistry
from app.services.tool_spec import ToolSpec, ToolTemplateError
from app.services.action_router import ActionRouter, ActionRouterError
from app.services.conversation_engine import ConversationEngine, ConversationEngineError
from app.services.speech_service import SpeechService, SpeechServiceError

__all__ = [
    # Security (Day 4)
    "hash_password",
    "verify_password",
    "create_access_token",
    "verify_access_token",
    "generate_api_key",
    "encrypt_token",
    "decrypt_token",
    # Tenant Service (Day 5)
    "TenantService",
    # DeepSeek AI (Day 8)
    "DeepSeekClient",
    "DeepSeekError",
    "PromptBuilder",
    "TOOL_DEFINITIONS",
    "TOOL_MAP",
    "VALID_TOOL_NAMES",
    # Tool Builder — per-tenant, DB-driven tools
    "ToolRegistry",
    "ToolConflictError",
    "ToolSpec",
    "ToolTemplateError",
    "PRESETS",
    "PRESET_SPECS",
    # Action Router (Day 9)
    "ActionRouter",
    "ActionRouterError",
    # Conversation Engine (Day 10)
    "ConversationEngine",
    "ConversationEngineError",
    # Speech Service — STT/TTS (Day 11)
    "SpeechService",
    "SpeechServiceError",
]
