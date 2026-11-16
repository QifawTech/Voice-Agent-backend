"""
Voice Agent Platform — Message Composition

What a reminder actually says. One module, so the wording of a product that
phones people at 21:00 is reviewable in one place rather than spread across
adapters.

Phase 1 writes English only. Tamil, Telugu, Kannada and Malayalam wording
arrives with the voice calls in phase 3, and WhatsApp's wording is not free
text at all — Meta requires pre-approved templates with variable slots, so
phase 4 fills slots rather than composing sentences. Keeping composition
separate from sending is what makes both of those additions local changes.

The tone rule, which holds for every channel: this is a service message, never
marketing. A reminder that reads like an advertisement is what gets a sender
reported, and a reported sender takes down every tenant on the platform.
"""

from dataclasses import dataclass

from app.models.occurrence import Occurrence
from app.models.person import Person
from app.models.reminder import Reminder
from app.services.answer_tokens import answer_url


@dataclass(frozen=True)
class Message:
    """A composed message, ready for whichever adapter is sending it."""

    subject: str
    body: str


# How each category refers to itself in a sentence. Small and explicit rather
# than clever: the whole value of a reminder is that it says the right thing.
_CATEGORY_LEAD = {
    "medicine": "It is time for your {title}.",
    "bill": "Your {title} is due.",
    "renewal": "Your {title} renews today.",
    "document": "Your {title} needs attention.",
    "business": "{title} is due today.",
    "personal": "A reminder: {title}.",
}


def expiry_phrase(offset_days: int | None) -> str | None:
    """
    How far off an expiry is, in words a person would use.

    "expires in 270 days" is technically correct and useless. Nine months is
    the unit someone thinks in when deciding whether to start a passport
    renewal.
    """
    if offset_days is None:
        return None
    if offset_days == 0:
        return "expires today"
    if offset_days == 1:
        return "expires tomorrow"
    if offset_days < 45:
        return f"expires in {offset_days} days"

    months = round(offset_days / 30)
    if months < 12:
        return f"expires in about {months} months"

    years = offset_days / 365
    if 0.9 <= years <= 1.1:
        return "expires in about a year"
    return f"expires in about {round(years, 1)} years"


def build(
    *,
    reminder: Reminder,
    person: Person,
    occurrence: Occurrence,
    channel: str,
) -> Message:
    """
    Compose the message for one occurrence on one channel.

    `channel` is accepted now and barely used: it is what phase 3 and 4 branch
    on, and threading it through from the start means those phases do not have
    to change every call site.
    """
    title = reminder.title
    when = occurrence.local_due_at.strftime("%H:%M")

    phrase = expiry_phrase(occurrence.offset_days)
    if phrase:
        lead = f"Your {title} {phrase}."
    else:
        lead = _CATEGORY_LEAD.get(
            reminder.category, _CATEGORY_LEAD["personal"]
        ).format(title=title)

    lines = [f"Hello {person.name},", "", lead]

    if reminder.notes:
        lines += ["", reminder.notes]

    if channel == "email":
        lines += [
            "",
            "When you have done it, use this link and the reminder will close:",
            answer_url(occurrence.id, "done"),
            "",
            "Not now? This link puts it off for an hour:",
            answer_url(occurrence.id, "snooze"),
        ]

    lines += [
        "",
        "---",
        f"Scheduled for {when}. You are receiving this because this reminder "
        "was set up for you.",
    ]

    subject = f"{title} — {phrase}" if phrase else f"Reminder: {title}"

    return Message(subject=subject, body="\n".join(lines))


def build_missed_notice(
    *, reminder: Reminder, person: Person, occurrence: Occurrence
) -> Message:
    """
    The message to whoever set the reminder up, when nobody answered.

    This is the caregiver feature, and the thing being paid for. A son in
    Chennai does not need to be told his mother took her tablet; he needs to
    be told when she did not.
    """
    when = occurrence.local_due_at.strftime("%d %b at %H:%M")
    attempts = occurrence.attempt_no + 1

    body = "\n".join(
        [
            f"{person.name} did not answer the reminder for {reminder.title}.",
            "",
            f"Due {when}. Tried {attempts} "
            f"{'time' if attempts == 1 else 'times'} and got no answer.",
            "",
            "Nothing further will be sent for this one.",
        ]
    )

    return Message(
        subject=f"No answer: {reminder.title} for {person.name}",
        body=body,
    )
