"""
Voice Agent Platform — Channel Interface

Every way of reaching a person looks the same to the engine: it is handed a
person, a composed message, and asked to send. The engine's tick loop, claim
lock, quiet hours, escalation ladder and delivery log do not know or care
which channel they are driving.

That is the whole reason Nudge and Vasool are one engine rather than two
products. Phase 3 drops a voice adapter into this interface and the ladder
starts making phone calls; phase 4 does the same for WhatsApp. Neither touches
the engine.

The contract has one unusual rule: a channel reports whether it is configured,
and an unconfigured channel is never an error. A platform where WhatsApp
approval has not landed yet must keep sending reminders by email, and the
honest record of that is a `skipped` delivery with a reason — not a crash, and
emphatically not a row that says "sent".
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.models.occurrence import Occurrence
from app.models.person import Person
from app.models.reminder import Reminder
from app.services.message_builder import Message


@dataclass(frozen=True)
class SendResult:
    """What came back from one attempt. Becomes one `deliveries` row."""

    status: str  # "sent" | "failed" | "skipped"
    provider: str
    recipient: str | None = None
    provider_message_id: str | None = None
    error: str | None = None
    cost: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "sent"

    @classmethod
    def skipped(cls, provider: str, reason: str) -> "SendResult":
        """Not attempted, and why. Never treated as a success."""
        return cls(status="skipped", provider=provider, error=reason)

    @classmethod
    def failed(cls, provider: str, reason: str, recipient: str | None = None) -> "SendResult":
        return cls(
            status="failed", provider=provider, error=reason, recipient=recipient
        )


@runtime_checkable
class Channel(Protocol):
    """One way of reaching a person."""

    name: str
    provider: str

    def is_configured(self) -> bool:
        """
        Whether this channel could send right now.

        Checked before composing a message, so an unconfigured channel costs
        nothing. False is a normal state, not a failure: it is what a platform
        waiting on WhatsApp approval or telephony KYC looks like.
        """
        ...

    async def send(
        self,
        *,
        person: Person,
        reminder: Reminder,
        occurrence: Occurrence,
        message: Message,
    ) -> SendResult:
        """
        Attempt one send. Must not raise.

        A channel that raises takes the tick loop down with it and stops every
        other reminder due in the same minute, so every adapter converts its
        own failures into SendResult.failed instead.
        """
        ...


class UnconfiguredChannel:
    """
    A channel that exists in the ladder but cannot send yet.

    Phase 1 ships four of these so that a reminder whose ladder is
    ['call', 'whatsapp', 'email'] behaves correctly today — it skips the two
    rungs that do not exist and arrives by email — and behaves correctly in
    phase 3 with no change to the reminder, the ladder or the engine.

    The plan asks specifically for `sms.py` to stay as an empty adapter that
    reports "not configured", so that adding SMS later is half a day rather
    than a redesign. This is that, generalised.
    """

    def __init__(self, name: str, provider: str, reason: str) -> None:
        self.name = name
        self.provider = provider
        self._reason = reason

    def is_configured(self) -> bool:
        return False

    async def send(self, **_kwargs) -> SendResult:
        return SendResult.skipped(self.provider, self._reason)
