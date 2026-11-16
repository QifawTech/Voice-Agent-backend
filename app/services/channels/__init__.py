"""
Voice Agent Platform — Channel Registry

The set of channels the engine can drive, and the one place that decides which
of them can actually send today.

Phase 1 has one working channel. The other three are placeholders that report
themselves unconfigured, which is what lets a reminder be set up now with the
ladder it should really have — call, then WhatsApp, then email — and start
using the better rungs the moment they exist, without anybody editing their
reminders.

Replacing a placeholder is the whole of what phases 3 and 4 do here:

    phase 3   VoiceChannel  — LiveKit SIP outbound, in place of the stub
    phase 4   WhatsAppChannel — Meta Cloud API templates, in place of the stub
"""

from app.services.channels.base import (
    Channel,
    SendResult,
    UnconfiguredChannel,
)
from app.services.channels.email import EmailChannel
from app.services.channels.sms import SmsChannel

# Order matters only as documentation; a reminder's own channel_ladder decides
# what is tried and in what sequence.
CHANNEL_NAMES = ("call", "whatsapp", "email", "sms")


def build_channels() -> dict[str, Channel]:
    """
    The channel map the engine is constructed with.

    A function rather than a module-level dict so tests can build their own
    map — a fake email channel that records what it was asked to send proves
    the ladder without a mail server.
    """
    return {
        "call": UnconfiguredChannel(
            name="call",
            provider="livekit-sip",
            reason=(
                "Outbound calling is not configured yet: it needs a telephony "
                "provider and a LiveKit SIP trunk (phase 3)."
            ),
        ),
        "whatsapp": UnconfiguredChannel(
            name="whatsapp",
            provider="meta-cloud",
            reason=(
                "WhatsApp is not configured yet: it needs Meta business "
                "verification and approved templates (phase 4)."
            ),
        ),
        "email": EmailChannel(),
        "sms": SmsChannel(),
    }


__all__ = [
    "Channel",
    "SendResult",
    "EmailChannel",
    "SmsChannel",
    "UnconfiguredChannel",
    "CHANNEL_NAMES",
    "build_channels",
]
