# Widget API Key Exposure — The Problem and The Fix

> **What this is:** why the voice widget's API key is visible to the public once a
> customer embeds the widget on their own website, what an attacker can actually do
> with it, and the concrete fix (domain lock + usage caps) planned for this backend.
>
> **Recorded:** 2026-10-26 · **Scope:** `Voice-Agent-backend` (the token endpoint),
> with notes on how `Sopzy` embeds the widget.
>
> **TL;DR:** You cannot hide a key the browser must use. The fix is not to hide it —
> it's to make it **worthless when stolen**: lock it to the customer's domain, and
> cap how much any one tenant can spend. Ship the caps first; they remove the scary
> part (an unbounded bill).

---

## 1. The setup — two different tokens, two directions

People confuse these constantly. They are **not** the same thing and they protect
**different doors**.

```
Browser ──[ widget key: sk_live_... ]──► Voice backend ──[ VOICE_AGENT_API_TOKEN ]──► Sopzy
         (in public JS, EXPOSED)          (our server)    (server-side .env, HIDDEN)
```

| | **Widget key** (`sk_live_…`) | **VOICE_AGENT_API_TOKEN** |
|---|---|---|
| Direction | Browser → Voice backend | Voice backend → Sopzy |
| Who reads it | The **browser** | Our **server** only |
| Purpose | "This café is allowed to **start a call**" | "I am the real agent, let me **place the order**" |
| Where it lives | Frontend `.env` (`VITE_…`) → **baked into public JS** | Backend `.env` → never sent to browser |
| Exposed to public? | **YES** ❌ | **No** ✅ |
| Code | `X-API-Key` checked in [`app/middleware/tenant_resolver.py`](../app/middleware/tenant_resolver.py) `get_current_tenant` | `Authorization: Bearer` built in [`app/services/action_router.py`](../app/services/action_router.py) `_build_headers`; checked on Sopzy in `app/core/voice_auth.py` |

**Key point:** `VOICE_AGENT_API_TOKEN` is handled correctly (server-to-server,
constant-time compare, fails closed, rate-limited, never logged). **This document is
only about the widget key**, which the browser must hold and therefore the public can
read.

---

## 2. Why `.env` does NOT hide the widget key

> **Rule:** a `.env` value is hidden **only if SERVER code reads it.**
> If the **frontend build** reads it, `.env` does not hide it.

The widget key is a `VITE_…` variable. Vite **copies every `VITE_` value into the
public JavaScript bundle at build time.** So even though it sits in a `.env` file, the
finished website ships the key in plain text — and it has to, because the browser
needs it to start a call.

A customer who pastes the embed snippet onto **their own website** puts the key into
plain public HTML. There is no `.env` on their side to hide it in.

---

## 3. What an attacker CAN and CANNOT do with the widget key

The attacker has only the widget key + tenant id (both in the public snippet).

### CAN ✅ (damage is real but **scoped to one café**)

1. **Run unlimited calls on our bill** ("denial of wallet") — every call costs us
   LLM + speech-to-text + text-to-speech + LiveKit minutes. The key alone is enough:
   see the flow below — starting a call never touches `VOICE_AGENT_API_TOKEN`.
2. **Place junk orders in the real kitchen** — the attacker talks to the agent and
   says "50 biryanis, table 5". The **agent** (which holds `VOICE_AGENT_API_TOKEN`)
   places it for them. They never need that token; the agent uses it on their behalf.
3. **Spam menu/price lookups**, and **impersonate** that café's voice line.

### CANNOT ❌

- Get `VOICE_AGENT_API_TOKEN` or the master LiveKit signing secret.
- Touch **any other tenant** — the key is per-tenant.
- Reach Sopzy's admin, database, customer list, or payments.
- Call Sopzy's `/voice-agent/*` routes directly (that door needs the inner token).

### Why the key alone is enough to run calls

A common misconception: "they need `VOICE_AGENT_API_TOKEN` to connect to the AI."
**No.** That token is used only when the agent calls *Sopzy*, which is a later step.

```
STEP 1  Attacker → Voice backend:  "here's sk_live_, give me a call ticket"
        Backend validates the key → mints a LiveKit JWT.      ← no VOICE_AGENT_API_TOKEN
STEP 2  Attacker → joins the LiveKit room with that ticket.
        The agent (already running on our server) greets them.
        🎙️ Live call. 💸 Already billing us.                   ← still no VOICE_AGENT_API_TOKEN
STEP 3  (only if they ask to order) Agent uses its OWN
        VOICE_AGENT_API_TOKEN ──► Sopzy places the order.     ← agent provides it, not the caller
```

Steps 1–2 — dial, connect, talk — cost us money using **only the widget key**.

---

## 4. The fix — make the key safe to be *seen*, not hidden

This is how every embeddable widget works (Stripe `pk_`, Google Maps keys): the key is
public by design, and made harmless with two layers.

### 🔒 Lock 1 — Domain check (Origin allowlist)

When a **real browser** calls our API from a web page, the browser automatically
stamps the request with that page's address in the `Origin` header, **and the page
cannot forge it.** So we check: is this Origin on the tenant's allowed list?

```
Asha registers "asha-cafe.com" → tenant.allowed_origins = ["https://asha-cafe.com"]

✅ Customer on asha-cafe.com   → Origin: https://asha-cafe.com → match   → ticket issued
❌ Key pasted on evil.com      → Origin: https://evil.com      → no match → rejected
```

**Stops:** the casual "copy the key, use it on another website" attack — the common case.

**Does NOT stop:** a script (curl) that fakes the `Origin` header, because curl is not
a browser and nothing forces it to tell the truth. That's what Lock 2 is for.

### 🔒 Lock 2 — Per-tenant usage caps (the real backstop)

Even a faked request can't exceed what a real café would ever need. We cap it. The
Redis rate limiter already exists in [`app/middleware/rate_limiter.py`](../app/middleware/rate_limiter.py)
(`RateLimitType`) — it is simply **not yet applied to the token endpoint**, so minting
call tickets is currently unlimited.

| Cap | Example | Stops |
|---|---|---|
| Rate | 10 tickets / minute / tenant | rapid-fire scripting |
| Daily quota | 500 calls / day / tenant | overnight bleed |
| Concurrency | 5 simultaneous calls / tenant | mass parallel abuse |

```
Script fakes Origin, requests 2,000 tickets/hour:
  11th in a minute → 429 Too Many Requests
  501st today      → blocked till tomorrow
  Worst-case bill  = 500 calls, not infinite — and an alert fires.
```

### How they combine (token endpoint — [`app/routes/livekit.py`](../app/routes/livekit.py))

```
valid key?  ──no──► ❌            (already implemented: get_current_tenant)
   │yes
Origin allowed?  ──no──► ❌       Lock 1: kills paste-on-other-site
   │yes
under rate + daily + concurrency caps?  ──no──► ❌   Lock 2: bounds scripted flood
   │yes
   ✅ mint the LiveKit ticket
```

---

## 5. "What if the attacker runs from local?"

| Attack from local | Result |
|---|---|
| Real **browser** on `localhost` / `file://` | `Origin` is `http://localhost…` or `null` → not in the café's list → **❌ blocked by Lock 1** |
| **Script** (curl) with a **faked** `Origin` | Lock 1 can't tell it from a real browser → **bounded by Lock 2 caps** |

> ⚠️ **Trap:** do **not** leave `http://localhost` in a *production* tenant's
> `allowed_origins`. It's fine for your own dev tenant; if it's on a real café's list,
> an attacker's localhost page will match and get in.

**The hard truth:** *any* key a browser can use, a script can also use. You can never
100% block a script holding a valid public key — nobody can; this is true for Stripe
and Google too. The defense is: **domain lock** (kills the easy theft) + **caps**
(bound the scripted damage) + **billing alerts** (notice and disable fast).

---

## 6. Build plan (in `Voice-Agent-backend`)

| # | Change | Files | Effort |
|---|---|---|---|
| 1 | Add `allowed_origins` column to `Tenant` + Alembic migration | [`app/models/tenant.py`](../app/models/tenant.py), `alembic/versions/` | small |
| 2 | Check request `Origin` against it in the token endpoint | [`app/routes/livekit.py`](../app/routes/livekit.py) or `get_current_tenant` | small |
| 3 | Add a `TOKEN` rate-limit type and apply the existing limiter to the token route | [`app/middleware/rate_limiter.py`](../app/middleware/rate_limiter.py), [`app/routes/livekit.py`](../app/routes/livekit.py) | small |
| 4 | Add daily quota + concurrency cap (same Redis) | `app/middleware/rate_limiter.py` | medium |
| 5 | Let the restaurant set its domains in Sopzy's settings UI | Sopzy frontend + backend | medium |

**Recommended order:** ship **1–3 first** — a few hours of work that removes the
unbounded-bill risk and the paste-elsewhere attack. Then harden with **4–5**.

Doing this also unblocks the public copy-paste embed feature honestly: the key in the
snippet becomes safe to be public, because it only works from the café's own
registered site and can't be used to flood us.

---

## 7. Related cleanup (separate issue, noted here for context)

- `sk_live_…` is currently committed in Sopzy's tracked `.env.production`. Once the
  above is in place, still **rotate that key** and stop tracking the file — a key
  that has been in git history should be treated as already leaked.
- Sopzy's `app/core/config.py` has a **real `fernet_key` default** baked into source;
  it should default to `""` and fail loudly if unset in production.

> These do not change the architecture above; they're hygiene fixes to do alongside it.
