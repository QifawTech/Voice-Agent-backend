"""
Voice Agent Platform — Dynamic Prompt Builder

Builds per-tenant system prompts for DeepSeek by injecting tenant-specific
details (restaurant name, greeting, UPI ID, language, tone, etc.) into a
base template.

This ensures each restaurant's voice agent has a personalized personality
and accurate information without hardcoding.

Usage:
    from app.services.prompt_builder import PromptBuilder

    builder = PromptBuilder()
    system_prompt = builder.build_system_prompt(tenant)
"""

from app.logger import get_logger

logger = get_logger(__name__)

# ──────────────────────────────────────────────────────────────
# Base System Prompt Template
# ──────────────────────────────────────────────────────────────
# Placeholders use {curly_braces} and are replaced at runtime.
# ──────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are a friendly and helpful voice ordering assistant for {restaurant_name}.

{custom_greeting}

## Your Role
- Help customers browse the menu, ask about items, and place orders.
- Be warm, conversational, and efficient. Keep responses concise since they will be spoken aloud.
- Always confirm order details (items, quantities, special instructions) before placing an order.
- If a customer is unsure, suggest popular items or ask about their preferences.

## Restaurant Information
- **Restaurant:** {restaurant_name}
- **Platform:** {platform}
{payment_section}

## Conversation Guidelines
- Speak in {language}. Keep responses short (2-3 sentences max) since this is a voice conversation.
- Use a {tone} tone.
- When listing menu items, mention the name and price. Don't list more than 5-6 items at once — offer to tell more.
- For orders, always repeat back the full order summary before confirming.
- If you don't know something, say so honestly — don't make up menu items or prices.
- If the customer asks something unrelated to food ordering, politely redirect them.
- **NEVER use markdown formatting** (like asterisks ** for bold, or dashes - for lists). Write in plain, conversational text because your response will be read aloud by a text-to-speech engine. For example, instead of "- Item", say "We have Item".
- **Always spell out numbers as words** since this is a voice conversation and TTS reads digits literally. For example, say "sixty five" instead of "65", "one hundred" instead of "100", "two hundred and fifty" instead of "250". Apply this to prices, quantities, and order numbers — but NOT to dish names (e.g., keep "Chicken 65" as "Chicken sixty five" only in speech context).
{language_rules}

## Available Actions
You have access to these functions:
1. **check_menu** — Browse the restaurant's menu (optionally filter by category)
2. **get_item_details** — Get details about a specific menu item
3. **create_order** — Place a new order (confirm with customer first!)
4. **get_order** — Check the status of an existing order
5. **cancel_order** — Cancel an order (confirm with customer first!)
6. **get_payment_info** — Get payment/UPI details

## Important Rules
- NEVER make up menu items or prices. Always use check_menu or get_item_details to get real data.
- ALWAYS confirm the order summary with the customer before calling create_order.
- When a customer says "order" or "I want to order", gather all items first, then confirm, then place the order.
- If the customer provides their name or phone, remember it for the order.
- Handle errors gracefully — if a function fails, apologize and suggest trying again.
{extra_instructions}"""

# ──────────────────────────────────────────────────────────────
# Section templates
# ──────────────────────────────────────────────────────────────

PAYMENT_SECTION_TEMPLATE = """- **Payment (UPI):** {upi_id}
- When asked about payment, share the UPI ID and let the customer know they can pay via any UPI app."""

NO_PAYMENT_SECTION = """- Payment information will be provided by the restaurant after order placement."""

# ──────────────────────────────────────────────────────────────
# Language-specific rules injected dynamically into the prompt
# ──────────────────────────────────────────────────────────────
# Maps language name (as stored in voice_config) → extra rules
# English is the default and needs no special handling.
# For non-English languages:
#   - Menu item names stay in English (they are proper nouns)
#   - All conversation (greetings, questions, confirmations) in the chosen language
# ──────────────────────────────────────────────────────────────

LANGUAGE_RULES: dict[str, str] = {
    "tamil": (
        "You are speaking in Tamil. Follow these strict rules:\n"
        "1. Respond entirely in Tamil for all greetings, questions, confirmations, and explanations.\n"
        "2. Menu item names (dish names) must always be kept in English as-is — do NOT translate or "
        "transliterate them. For example, say 'Chicken Biryani', 'Paneer Tikka', 'Dal Makhani' in English, "
        "not in Tamil script.\n"
        "3. Category names (Starters, Main Course, Breads, Beverages, Desserts) also stay in English.\n"
        "4. Prices, quantities, and order numbers should be spoken as Tamil words.\n"
        "5. Example: Say 'நம்ம மெனுல Paneer Tikka, Chicken 65 மாதிரி Starters இருக்கு' — "
        "NOT 'பன்னீர் டிக்கா, சிக்கன் அறுபத்தைந்து'."
    ),
    "telugu": (
        "You are speaking in Telugu. Follow these strict rules:\n"
        "1. Respond entirely in Telugu for all greetings, questions, confirmations, and explanations.\n"
        "2. Menu item names (dish names) must always be kept in English as-is — do NOT translate or "
        "transliterate them. For example, say 'Chicken Biryani', 'Paneer Tikka' in English.\n"
        "3. Category names (Starters, Main Course, Breads, Beverages, Desserts) also stay in English."
    ),
    "kannada": (
        "You are speaking in Kannada. Follow these strict rules:\n"
        "1. Respond entirely in Kannada for all greetings, questions, confirmations, and explanations.\n"
        "2. Menu item names (dish names) must always be kept in English as-is — do NOT translate or "
        "transliterate them. For example, say 'Chicken Biryani', 'Paneer Tikka' in English.\n"
        "3. Category names (Starters, Main Course, Breads, Beverages, Desserts) also stay in English."
    ),
    "malayalam": (
        "You are speaking in Malayalam. Follow these strict rules:\n"
        "1. Respond entirely in Malayalam for all greetings, questions, confirmations, and explanations.\n"
        "2. Menu item names (dish names) must always be kept in English as-is — do NOT translate or "
        "transliterate them. For example, say 'Chicken Biryani', 'Paneer Tikka' in English.\n"
        "3. Category names (Starters, Main Course, Breads, Beverages, Desserts) also stay in English."
    ),
    "hindi": (
        "You are speaking in Hindi. Follow these strict rules:\n"
        "1. Respond entirely in Hindi for all greetings, questions, confirmations, and explanations.\n"
        "2. Menu item names (dish names) must always be kept in English as-is — do NOT translate or "
        "transliterate them. For example, say 'Chicken Biryani', 'Paneer Tikka' in English.\n"
        "3. Category names (Starters, Main Course, Breads, Beverages, Desserts) also stay in English."
    ),
}


# ──────────────────────────────────────────────────────────────
# Per-language custom prompts
# ──────────────────────────────────────────────────────────────
# Tenants can write their own greeting + instructions for each language from
# the dashboard. These live in voice_config["prompts"], keyed by the lowercased
# language name:
#
#   voice_config["prompts"] = {
#       "english": {"greeting": "...", "instructions": "..."},
#       "tamil":   {"greeting": "...", "instructions": "..."},
#   }
#
# We currently let tenants edit prompts for these languages only. The agent can
# still *run* in any language — unlisted ones just fall back to the defaults.
# ──────────────────────────────────────────────────────────────

SUPPORTED_PROMPT_LANGUAGES = ("english", "tamil")


def resolve_language_config(voice_config: dict | None, language: str) -> dict:
    """
    Pull a tenant's custom prompt + greeting for a given language.

    Reads voice_config["prompts"][<language>] and falls back to the flat legacy
    ``greeting`` field (which applied to every language) so tenants configured
    before this feature keep working unchanged.

    Args:
        voice_config: The tenant's voice_config dict (or None).
        language: Runtime language name, e.g. "english" or "Tamil".

    Returns:
        {"instructions": str, "greeting": str} — empty strings when unset.
    """
    voice_config = voice_config or {}
    key = (language or "english").lower().strip()

    prompts = voice_config.get("prompts") or {}
    entry = prompts.get(key) or {}

    instructions = (entry.get("instructions") or "").strip()
    greeting = (entry.get("greeting") or "").strip()

    # Legacy fallback: a single flat greeting that applied to all languages.
    if not greeting:
        greeting = (voice_config.get("greeting") or "").strip()

    return {"instructions": instructions, "greeting": greeting}


class PromptBuilder:
    """
    Builds personalized system prompts for DeepSeek based on tenant configuration.

    Reads tenant model fields (name, platform, upi_id, voice_config) and injects
    them into the base template to create a unique persona for each restaurant.
    """

    def build_system_prompt(self, tenant, sub_tenant=None) -> str:
        """
        Build a complete system prompt for a specific tenant.

        Args:
            tenant: Tenant model instance or dict with keys:
                - name (str): Restaurant/business name
                - platform (str): "sopzy" | "standalone" | "custom"
                - upi_id (str | None): UPI payment ID
                - voice_config (dict | None): {greeting, language, tone, extra_instructions}
            sub_tenant: Optional SubTenant for a multi-tenant account. When given,
                its name is spoken by the agent and its greeting/UPI override the
                parent's — see resolve_display_config for the inheritance rule.

        Returns:
            Formatted system prompt string ready for DeepSeek.
        """
        # A sub-tenant's identity and overrides replace the parent's for anything
        # the caller hears. Without one this resolves to the tenant's own values,
        # so single-tenant behaviour is unchanged.
        if sub_tenant is not None:
            from app.services.tenant_service import resolve_display_config

            tenant = resolve_display_config(tenant, sub_tenant)

        # Extract tenant fields (support both model objects and dicts)
        name = self._get_field(tenant, "name", "the restaurant")
        platform = self._get_field(tenant, "platform", "standalone")
        upi_id = self._get_field(tenant, "upi_id", None)
        voice_config = self._get_field(tenant, "voice_config", {}) or {}

        # Extract voice configuration
        language = voice_config.get("language", "English")
        tone = voice_config.get("tone", "friendly and professional")
        extra_instructions = voice_config.get("extra_instructions", "")

        # Resolve the tenant's per-language custom greeting + instructions
        # (falls back to the legacy flat greeting for older tenants).
        lang_cfg = resolve_language_config(voice_config, language)
        greeting = lang_cfg["greeting"]
        custom_instructions = lang_cfg["instructions"]

        # A per-language custom prompt controls how the agent talks; the
        # mandatory ordering/TTS/tool rules below are always kept on top of it.
        if custom_instructions:
            extra_instructions = (
                f"{custom_instructions}\n\n{extra_instructions}".strip()
                if extra_instructions
                else custom_instructions
            )

        # Build custom greeting section
        custom_greeting = ""
        if greeting:
            custom_greeting = f"Your default greeting when a customer starts a conversation is:\n\"{greeting}\""
        else:
            custom_greeting = (
                f"When a customer starts a conversation, greet them warmly and "
                f"introduce yourself as {name}'s ordering assistant."
            )

        # Build payment section
        payment_section = (
            PAYMENT_SECTION_TEMPLATE.format(upi_id=upi_id)
            if upi_id
            else NO_PAYMENT_SECTION
        )

        # Build extra instructions section
        extra_section = ""
        if extra_instructions:
            extra_section = f"\n## Additional Instructions\n{extra_instructions}"

        # Build language-specific rules section
        language_rules_section = self._build_language_section(language)

        # Format the final prompt
        prompt = BASE_SYSTEM_PROMPT.format(
            restaurant_name=name,
            platform=platform,
            custom_greeting=custom_greeting,
            payment_section=payment_section,
            language=language,
            tone=tone,
            extra_instructions=extra_section,
            language_rules=language_rules_section,
        )

        logger.debug(
            "system_prompt_built",
            tenant_name=name,
            platform=platform,
            has_greeting=bool(greeting),
            has_upi=bool(upi_id),
            language=language,
            prompt_length=len(prompt),
        )

        return prompt

    def build_messages(
        self,
        tenant,
        user_message: str,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        """
        Build the complete messages array for a DeepSeek API call.

        Args:
            tenant: Tenant model instance or dict.
            user_message: The latest message from the customer.
            conversation_history: Previous messages in the session
                                  [{role: "user"/"assistant", content: "..."}].

        Returns:
            List of message dicts ready for DeepSeek API:
            [
                {"role": "system", "content": "..."},
                {"role": "user", "content": "..."},
                {"role": "assistant", "content": "..."},
                ...
                {"role": "user", "content": "<latest message>"},
            ]
        """
        messages = []

        # 1. System prompt (always first)
        system_prompt = self.build_system_prompt(tenant)
        messages.append({"role": "system", "content": system_prompt})

        # 2. Conversation history (if any)
        if conversation_history:
            # Only include the last N messages to stay within token limits
            # DeepSeek has 8192 token context — keep history manageable
            max_history = 20  # ~10 turns of conversation
            recent_history = conversation_history[-max_history:]

            for msg in recent_history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})

        # 3. Current user message (always last)
        messages.append({"role": "user", "content": user_message})

        logger.debug(
            "messages_built",
            total_messages=len(messages),
            history_count=len(conversation_history) if conversation_history else 0,
        )

        return messages

    @staticmethod
    def _get_field(obj, field: str, default=None):
        """Extract a field from either a model object or a dict."""
        if isinstance(obj, dict):
            return obj.get(field, default)
        return getattr(obj, field, default)

    @staticmethod
    def _build_language_section(language: str) -> str:
        """
        Return language-specific prompt rules based on the selected language.

        For English (default), no extra rules are needed.
        For Indian languages (Tamil, Telugu, Kannada, Malayalam, Hindi), injects
        rules that keep menu item names in English while responding in that language.

        Args:
            language: Language name from voice_config (e.g. "Tamil", "English").

        Returns:
            A formatted string with language rules to embed in the prompt,
            or an empty string for English.
        """
        key = language.lower().strip()

        if key == "english" or key == "en":
            return ""  # No extra rules needed for English

        rules = LANGUAGE_RULES.get(key)
        if rules:
            return f"\n## Language Rules\n{rules}"

        # Fallback for any unrecognized non-English language
        return (
            f"\n## Language Rules\n"
            f"You are speaking in {language}. Respond entirely in {language}. "
            f"Keep all menu item names and category names in English as-is — "
            f"do NOT translate or transliterate dish names."
        )
