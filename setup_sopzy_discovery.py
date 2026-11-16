"""
Create (or update) the "Sopzy Discovery" voice account.

The outside-the-store voice agent: a customer opens the Sopzy app, names a
dish, and hears which nearby shops sell it. It is a SEPARATE account from the
in-store Sopzy account so that:

  * it is single-tenant, so a call needs no restaurant slug (no 401), and
  * its agent is offered ONLY the discovery tool, never the ordering tools.

Both accounts call the same Sopzy API; they differ only in which endpoints
their tools point at. Design: VOICE_DISCOVERY_PLAN.md in the Sopzy repo.

Safe to re-run: an existing account is updated in place, never duplicated, and
its tools are reset to exactly the discovery set.

Usage (from this folder, with .env pointing at the target database):

    python setup_sopzy_discovery.py --source-slug shopy --key-file <path>

  --source-slug  The in-store Sopzy account to copy from: its API base URL, its
                 API token, and its voice settings (language, TTS), so both
                 agents call the same Sopzy API and sound the same.
  --key-file     Where to write the account's widget API key (the app needs
                 it). The key is never printed. Keep this file out of git.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.database import async_session_factory
from app.models.admin_user import AdminUser
from app.models.tenant import Tenant
from app.redis_client import init_redis
from app.schemas.tenant import TenantCreate
from app.schemas.tool import ToolCreate
from app.services.security import decrypt_token, encrypt_token
from app.services.tenant_service import TenantService
from app.services.tool_presets import GREETING_TOOL_NAME
from app.services.tool_registry import ToolRegistry

DISCOVERY_NAME = "Sopzy Discovery"
DISCOVERY_SLUG = "sopzy-discovery"

# Voice settings copied from the source account so both agents sound alike.
VOICE_KEYS = (
    "language",
    "tone",
    "tts_provider",
    "elevenlabs_model",
    "elevenlabs_voice_id",
    "voice_gender",
    "voice",
    "pace",
)

# One persona for every language: the platform appends its own per-language
# rules (script, transliteration) after this, so the wording here stays English.
PERSONA = (
    "You are Sopzy's food-finder voice assistant. The customer has just opened "
    "the Sopzy app and is not inside any restaurant yet. Your only job is to "
    "help them find which nearby shops sell the dish they want. "
    "When the customer names a dish or a craving, call find_dish_nearby with "
    "that dish straight away. "
    # "nearest first" was a lie whenever a coupon was in play: the ranking puts
    # the coupon shop FIRST even when it is farther, so an agent told to sort by
    # distance demoted it, then cut it for length -- the customer never heard
    # about Rs 200 they already held (seen in a real call, 2026-11-06).
    "Read out the first three shops from find_dish_nearby IN THE ORDER IT "
    "RETURNED THEM. The list is already ranked for this customer - never "
    "re-sort it, and never skip a shop so that a nearer one can come first. "
    "For each shop: its name, how far it is in kilometres (only when a "
    "distance is given - never guess one), and its cheapest price for that "
    "dish. "
    "If a shop comes with an offer or coupon, you MUST say the offer out loud "
    "and you MUST NOT leave that shop out. It is money the customer already "
    "holds, so it is read out even when the shop is farther away than the "
    "others. "
    "If no shop sells it, say so briefly and ask what else they would like. "
    "Never invent shops, dishes, prices or distances - only say what "
    "find_dish_nearby returned. "
    "You cannot place orders here. When the customer picks a shop, tell them "
    "to tap that shop's card on the screen to open it and order there. "
    # One or two sentences cannot hold three shops with three facts each, so
    # the model silently dropped the third shop -- which the ranking had made
    # the coupon one. Budget it honestly instead, and say what brevity may not
    # cost.
    "Keep it tight: one short line per shop, four sentences at most. Being "
    "brief NEVER justifies dropping a shop or leaving out an offer. "
    # Shop menus are stored in Latin letters ("Chiken Biriyani", "Masala Dosa"),
    # and the dish search matches on that text. A Tamil/Telugu/etc. caller says
    # the dish in their own script, so without this rule the model passes e.g.
    # "பிரியாணி", nothing matches, and the agent wrongly says it is unavailable.
    "CRITICAL RULE for the find_dish_nearby tool: the `dish` argument MUST "
    "always be written in plain English letters (Latin script) - for example "
    "'biriyani', 'dosa', 'chicken', 'parotta' - spelled the way the word "
    "SOUNDS. NEVER pass Tamil, Telugu, Kannada, Malayalam, Hindi or any other "
    "script in the `dish` argument, because every shop menu is written in "
    "English letters and a non-English argument finds nothing. This applies "
    "ONLY to the tool argument: keep SPEAKING to the customer in their own "
    "language and script as usual. "
    "If a search returns no shops, consider whether you spelled the dish in "
    "English letters; if not, call find_dish_nearby once more with the English "
    "spelling before telling the customer it is unavailable."
)
PERSONA_LANGUAGES = ("english", "tamil", "telugu", "kannada", "malayalam", "hindi")

# Spoken only if the greeting API is unreachable (greeting_setup mode "api").
FALLBACK_GREETING = "Hello! What would you like to eat today?"

# Goes into voice_config["extra_instructions"], which build_instruction_text
# appends AFTER its own per-language rules. That placement is the whole point:
# the platform's Tamil/Telugu/... rule says to transliterate ALL dish names into
# the local script, and being later in the prompt it beat the same wording when
# it sat in the persona. This restates the exception last, so it wins.
TOOL_ARGUMENT_OVERRIDE = (
    "FINAL OVERRIDE, and it beats every earlier rule about transliterating dish "
    "names: the transliteration rule applies ONLY to words you SPEAK OUT LOUD. "
    "It does NOT apply to function/tool arguments. When you call "
    "find_dish_nearby, the `dish` argument MUST be plain English letters (Latin "
    "script), spelled how the word sounds - 'biriyani', 'dosa', 'chicken', "
    "'parotta', 'idli'. Passing Tamil, Telugu, Kannada, Malayalam or Hindi "
    "script in that argument always returns zero shops, so never do it. "
    "Keep speaking to the customer in their own language and script as before."
)

FIND_DISH_TOOL = {
    "name": "find_dish_nearby",
    "display_name": "Find Dish Nearby",
    "description": (
        "Find shops near the customer that sell a dish, nearest and most "
        "popular first. Call this whenever the customer names a dish or food "
        "they want (e.g. 'biriyani', 'dosa'). Returns each shop's name, its "
        "distance in km (null when unknown), how often the dish was ordered "
        "there, its cheapest matching price, and the matching item names."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dish": {
                "type": "string",
                "description": (
                    "The dish or food the customer asked for, in plain ENGLISH "
                    "LETTERS only (Latin script), spelled how it sounds - e.g. "
                    "'biriyani', 'dosa', 'chicken', 'parotta'. Shop menus are "
                    "written in English letters, so a Tamil/Telugu/Kannada/"
                    "Malayalam/Hindi spelling here matches nothing. If the "
                    "customer said it in another language, transliterate it to "
                    "English letters before passing it."
                ),
            },
        },
        "required": ["dish"],
    },
    "http_method": "GET",
    "path_template": "/voice-agent/discover/dish",
    # lat/lng come from the app's per-call context bag, not from the model.
    "query_template": {
        "q": "{dish}",
        "lat": "{context_lat}",
        "lng": "{context_lng}",
    },
    "body_template": None,
    "headers_template": None,
    # MUST be set. With no template the platform falls back to its generic
    # summary, which reports only a list's LENGTH ("2 shops") and drops the
    # names, distances and prices — the agent then has nothing to read out and
    # says it cannot tell the customer which shops (verified 2026-11-04). The
    # endpoint returns a ready sentence in `summary` for exactly this.
    "success_template": "{summary}",
    "sort_order": 10,
}

# The internal greeting tool, repointed from the in-store greeting at the
# discovery one. It must keep the name get_greeting: that is the name the agent
# looks up at connect, and the name that keeps it out of the model's tool list.
GREETING_TOOL_UPDATE = {
    "display_name": "Opening Greeting Data",
    "description": (
        "Internal. Fetched once when a call connects to build the opening line "
        "(greets the customer by name). The assistant never calls this during "
        "the conversation."
    ),
    "path_template": "/voice-agent/discover/greeting",
    "query_template": {"name": "{context_customer_name}", "lang": "{lang}"},
    "body_template": None,
    "success_template": None,
}

KEEP_TOOLS = {GREETING_TOOL_NAME, FIND_DISH_TOOL["name"]}


def build_voice_config(source: Tenant) -> dict:
    src = source.voice_config or {}
    config = {k: src[k] for k in VOICE_KEYS if src.get(k) not in (None, "")}
    config["greeting"] = FALLBACK_GREETING
    config["greeting_setup"] = {"mode": "api", "configured": True}
    config["prompts"] = {
        lang: {"instructions": PERSONA} for lang in PERSONA_LANGUAGES
    }
    # MUST stay in extra_instructions, not in the persona: build_instruction_text
    # appends this LAST, after the platform's own per-language rule that orders
    # the model to transliterate ALL dish names into the local script. That rule
    # would otherwise win by position and the tool would be called with e.g.
    # "பிரியாணி", which matches no menu item (verified 2026-11-04).
    config["extra_instructions"] = TOOL_ARGUMENT_OVERRIDE
    return config


async def main(source_slug: str, key_file: str) -> int:
    redis = await init_redis()

    async with async_session_factory() as db:
        source = (
            await db.execute(select(Tenant).where(Tenant.slug == source_slug))
        ).scalar_one_or_none()
        if source is None:
            print(f"ERROR: no account with slug '{source_slug}'.")
            return 1
        if not source.api_base_url or not source.api_auth_token:
            print(f"ERROR: '{source_slug}' has no API URL or API token to copy.")
            return 1
        api_token = decrypt_token(source.api_auth_token)
        voice_config = build_voice_config(source)

        tenants = TenantService(db=db, redis=redis)
        tenant = (
            await db.execute(select(Tenant).where(Tenant.slug == DISCOVERY_SLUG))
        ).scalar_one_or_none()

        if tenant is None:
            admin = (
                await db.execute(
                    select(AdminUser).where(AdminUser.role == "super_admin").limit(1)
                )
            ).scalar_one_or_none()
            if admin is None:
                print("ERROR: no super_admin user exists to create the account.")
                return 1
            tenant = await tenants.create_tenant(
                TenantCreate(
                    name=DISCOVERY_NAME,
                    slug=DISCOVERY_SLUG,
                    platform="sopzy",
                    tenancy_type="single",
                    api_base_url=source.api_base_url,
                    api_auth_token=api_token,
                    voice_config=voice_config,
                ),
                admin,
            )
            await db.commit()
            action = "created"
        else:
            tenant.name = DISCOVERY_NAME
            tenant.platform = "sopzy"
            tenant.tenancy_type = "single"
            tenant.api_base_url = source.api_base_url
            tenant.api_auth_token = encrypt_token(api_token)
            tenant.voice_config = voice_config
            tenant.is_active = True
            await db.commit()
            await tenants._invalidate_cache(tenant.id)
            action = "updated"

        await db.refresh(tenant)

        # Tools: exactly the discovery set. The ordering presets a new account
        # is seeded with would only confuse a call that has no restaurant.
        registry = ToolRegistry(db=db, redis=redis)
        removed = []
        for tool in await registry.list_tools(tenant.id):
            if tool.name not in KEEP_TOOLS:
                await registry.delete_tool(tenant.id, tool.id)
                removed.append(tool.name)

        greeting = await registry.get_tool_by_name(tenant.id, GREETING_TOOL_NAME)
        if greeting is None:
            await registry.seed_presets(tenant.id, [GREETING_TOOL_NAME])
            greeting = await registry.get_tool_by_name(tenant.id, GREETING_TOOL_NAME)
        await registry.update_tool(tenant.id, greeting.id, GREETING_TOOL_UPDATE)

        # Validate exactly as the dashboard would before saving.
        ToolCreate(**FIND_DISH_TOOL)
        find_dish = await registry.get_tool_by_name(tenant.id, FIND_DISH_TOOL["name"])
        if find_dish is None:
            await registry.create_tool(tenant.id, FIND_DISH_TOOL)
        else:
            await registry.update_tool(tenant.id, find_dish.id, FIND_DISH_TOOL)

        with open(key_file, "w", encoding="utf-8") as f:
            f.write(tenant.widget_api_key)

        final_tools = sorted(t.name for t in await registry.list_tools(tenant.id))
        print(f"account {action}: {tenant.name} ({tenant.slug})")
        print(f"  tenant_id     : {tenant.id}")
        print(f"  tenancy_type  : {tenant.tenancy_type}")
        print(f"  api_base_url  : {tenant.api_base_url}  (copied from '{source_slug}')")
        print(f"  language      : {voice_config.get('language')}")
        print(f"  greeting_setup: {voice_config['greeting_setup']}")
        print(f"  tools         : {final_tools}")
        if removed:
            print(f"  removed       : {sorted(removed)}")
        print(f"  widget key    : written to {key_file} (not printed)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-slug", required=True)
    parser.add_argument("--key-file", required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.source_slug, args.key_file)))
