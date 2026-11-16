"""
Voice Agent Platform — Email Channel

The first real channel, and the one the whole engine is proven with: a
reminder set for 21:00 arriving by email at 21:00, twice in a row, across a
restart.

Email is also the honest last rung. The plan is explicit that without SMS, a
person who misses the call and does not read WhatsApp has effectively not been
reminded — so an email that was sent must never be the reason a dashboard says
everything is fine. That judgement belongs to the engine; this module's only
job is to report truthfully what happened.

Async SMTP, not stdlib smtplib: a blocking send stalls the event loop, which
means it stalls the tick that is trying to send the next 99 reminders.
"""

import ssl
import uuid

import aiosmtplib
from email.message import EmailMessage

from app.config import get_settings
from app.logger import get_logger
from app.models.occurrence import Occurrence
from app.models.person import Person
from app.models.reminder import Reminder
from app.services.channels.base import SendResult
from app.services.message_builder import Message

logger = get_logger(__name__)


class EmailChannel:
    """Sends reminders over SMTP."""

    name = "email"
    provider = "smtp"

    def is_configured(self) -> bool:
        return get_settings().smtp_configured

    async def send(
        self,
        *,
        person: Person,
        reminder: Reminder,
        occurrence: Occurrence,
        message: Message,
    ) -> SendResult:
        settings = get_settings()

        if not settings.smtp_configured:
            return SendResult.skipped(
                self.provider, "SMTP is not configured (SMTP_HOST / SMTP_FROM)"
            )

        to_address = person.email
        if not to_address:
            return SendResult.skipped(
                self.provider, f"{person.name} has no email address"
            )

        mail = EmailMessage()
        mail["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM}>"
        mail["To"] = to_address
        mail["Subject"] = message.subject

        # A Message-ID we generate ourselves, so the delivery row can be tied
        # to the message in a mail server's logs during a dispute. Most servers
        # keep the one they are given.
        domain = settings.SMTP_FROM.split("@")[-1] or "localhost"
        message_id = f"<{uuid.uuid4()}@{domain}>"
        mail["Message-ID"] = message_id

        # Marks this as an automated service message. Well-behaved mail clients
        # use it to suppress out-of-office replies, which would otherwise
        # bounce back at a mailbox nobody reads.
        mail["Auto-Submitted"] = "auto-generated"

        mail.set_content(message.body)

        try:
            await aiosmtplib.send(
                mail,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME or None,
                password=settings.SMTP_PASSWORD or None,
                start_tls=settings.SMTP_USE_TLS,
                tls_context=ssl.create_default_context()
                if settings.SMTP_USE_TLS
                else None,
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # Every failure becomes a result, never an exception: one bad
            # mailbox must not stop the other reminders due this minute.
            # The exception type is included because "Connection refused" and
            # "535 auth failed" need completely different fixes.
            reason = f"{type(exc).__name__}: {exc}"
            logger.error(
                "email_send_failed",
                occurrence_id=str(occurrence.id),
                recipient=to_address,
                error=reason,
            )
            return SendResult.failed(self.provider, reason, recipient=to_address)

        logger.info(
            "email_sent",
            occurrence_id=str(occurrence.id),
            reminder=reminder.title,
            recipient=to_address,
        )
        return SendResult(
            status="sent",
            provider=self.provider,
            recipient=to_address,
            provider_message_id=message_id,
            # SMTP through SES or Gmail is cheap enough per message that a
            # per-send figure would be invented. Phase 8 prices channels
            # properly for the spend caps; until then the column stays honest
            # by staying empty.
            cost=None,
        )

    async def send_plain(self, *, to_address: str, message: Message) -> SendResult:
        """
        Send to an arbitrary address, outside the reminder ladder.

        Used for the "nobody answered" notice to whoever set the reminder up.
        That recipient is an account holder, not a `people` row, so it cannot
        go through the normal path.
        """
        settings = get_settings()
        if not settings.smtp_configured:
            return SendResult.skipped(self.provider, "SMTP is not configured")

        mail = EmailMessage()
        mail["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM}>"
        mail["To"] = to_address
        mail["Subject"] = message.subject
        mail["Auto-Submitted"] = "auto-generated"
        mail.set_content(message.body)

        try:
            await aiosmtplib.send(
                mail,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME or None,
                password=settings.SMTP_PASSWORD or None,
                start_tls=settings.SMTP_USE_TLS,
                tls_context=ssl.create_default_context()
                if settings.SMTP_USE_TLS
                else None,
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.error("notice_send_failed", recipient=to_address, error=reason)
            return SendResult.failed(self.provider, reason, recipient=to_address)

        return SendResult(
            status="sent", provider=self.provider, recipient=to_address
        )
