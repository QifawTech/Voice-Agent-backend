"""
Voice Agent Platform — SMS Channel (deliberately empty)

SMS is out of scope on purpose, and this file is the plan's instruction kept
literally: keep sms.py as an empty adapter that reports "not configured", so
adding SMS later is half a day rather than a redesign.

Why it is not built: sending SMS to Indian numbers requires DLT registration
with TRAI — weeks of paperwork and a per-template approval process — for a
channel WhatsApp already covers here. Dropping it removes an entire regulatory
track from the project.

What this file buys by existing: 'sms' stays a valid rung in a channel ladder.
A reminder configured today as ['call', 'sms', 'email'] does the right thing —
skips SMS with a recorded reason, arrives by email — and would start sending
SMS the day this class gets a body, with no change to any reminder.
"""

from app.services.channels.base import SendResult


class SmsChannel:
    """Not configured, by decision rather than by omission."""

    name = "sms"
    provider = "none"

    def is_configured(self) -> bool:
        return False

    async def send(self, **_kwargs) -> SendResult:
        return SendResult.skipped(
            self.provider,
            "SMS is not configured: DLT registration with TRAI is required and "
            "is out of scope. Use WhatsApp or a call.",
        )
