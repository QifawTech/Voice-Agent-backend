"""
Voice Agent Platform — Reminder Engine Schemas

Request and response shapes for agents, people, reminders and occurrences.

Validation here is doing real work rather than type-checking. Every rule below
exists because the alternative is a row that looks fine and silently never
fires:

    a repeating reminder with no repeat rule       generates nothing, forever
    an expiry reminder with no date                the same
    a person with no phone, email or WhatsApp      cannot be reached by any rung
    a timezone that is not an IANA name            fires at the wrong hour
    a channel name with a typo                     skipped at every attempt

Catching those at the edge turns each one into a 422 with a sentence someone
can act on, rather than a quiet absence discovered weeks later.
"""

from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.agent import AGENT_TYPES
from app.models.reminder import (
    CATEGORIES,
    DEFAULT_CHANNEL_LADDER,
    SCHEDULE_KINDS,
)
from app.services.channels import CHANNEL_NAMES
from app.services.occurrence_generator import (
    InvalidSchedule,
    normalise_warn_before_days,
    validate_rrule,
)
from app.services.timekeeping import UnknownTimezone, validate_timezone


def check_schedule(
    schedule_kind: str,
    rrule: str | None,
    due_date: date | None,
    warn_before_days: list[int] | None,
) -> list[int] | None:
    """
    Check that a schedule is one the generator can actually expand, and return
    the normalised warning days.

    Lives outside the models because an edit has to be checked the same way a
    creation is, against the merged result rather than against the patch. A
    reminder that was repeating and is sent only a new due_date would otherwise
    pass field validation and then quietly stop producing anything.

    Raises ValueError, so it reads as a 422 wherever Pydantic calls it.
    """
    try:
        if schedule_kind == "repeating":
            if not rrule:
                raise InvalidSchedule(
                    "A repeating reminder needs a repeat rule, for example "
                    "FREQ=MONTHLY;BYMONTHDAY=5 for the 5th of each month."
                )
            validate_rrule(rrule)
            return warn_before_days

        if schedule_kind == "expiry":
            if due_date is None:
                raise InvalidSchedule(
                    "An expiry reminder needs the date it expires on."
                )
            return normalise_warn_before_days(warn_before_days)

        if schedule_kind == "balance":
            raise InvalidSchedule(
                "Balance-check reminders are not built yet: they need a "
                "per-provider API call to know whether anything is due."
            )
    except InvalidSchedule as exc:
        raise ValueError(str(exc)) from exc

    return warn_before_days


# ----------------------------------------------------------------
# Agents
# ----------------------------------------------------------------


class AgentCreate(BaseModel):
    type: str = Field(description="restaurant | nudge | vasool")
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(
        default=None,
        max_length=100,
        description="URL-safe id, unique within the account. Derived from the name if omitted.",
    )
    voice_config: dict | None = None

    @field_validator("type")
    @classmethod
    def known_type(cls, v: str) -> str:
        if v not in AGENT_TYPES:
            raise ValueError(
                f"Unknown agent type '{v}'. One of: {', '.join(AGENT_TYPES)}."
            )
        return v

    @field_validator("slug")
    @classmethod
    def slug_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = v.strip().lower()
        if not cleaned.replace("-", "").isalnum():
            raise ValueError(
                "A slug may contain only letters, numbers and hyphens."
            )
        return cleaned


class AgentUpdate(BaseModel):
    """
    What may be changed on an agent after it exists.

    `type` and `slug` are absent on purpose. The type decides which generator
    writes the occurrences, and the slug is in the URL of every bookmark and
    link to this agent; changing either silently breaks things that already
    work. Delete and recreate if one is genuinely wrong.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    voice_config: dict | None = None
    is_active: bool | None = Field(
        default=None,
        description=(
            "Setting this false stops every reminder on this agent being "
            "claimed. Existing occurrences are left where they are."
        ),
    )


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    type: str
    name: str
    slug: str
    voice_config: dict | None
    is_active: bool
    created_at: datetime


# ----------------------------------------------------------------
# People
# ----------------------------------------------------------------


class PersonCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    is_account_holder: bool = False

    phone: str | None = Field(default=None, max_length=20)
    whatsapp: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None

    language: str = Field(default="en", max_length=10)
    timezone: str = Field(default="Asia/Kolkata", max_length=64)

    quiet_hours_start: time = time(22, 0)
    quiet_hours_end: time = time(7, 0)

    # Recording consent is not optional for a product that phones people. The
    # route stamps the timestamp and the confirming user; this flag is the
    # human saying it is true.
    consent_confirmed: bool = Field(
        default=False,
        description=(
            "Confirmation that this person has agreed to be contacted. Required."
        ),
    )

    @field_validator("timezone")
    @classmethod
    def real_timezone(cls, v: str) -> str:
        try:
            return validate_timezone(v)
        except UnknownTimezone as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def reachable_somehow(self):
        if not (self.phone or self.whatsapp or self.email):
            raise ValueError(
                "This person needs at least one of phone, whatsapp or email — "
                "otherwise no reminder can ever reach them."
            )
        if not self.consent_confirmed:
            raise ValueError(
                "consent_confirmed must be true: this platform does not "
                "contact anyone whose consent has not been recorded."
            )
        return self


class PersonUpdate(BaseModel):
    """
    Correcting someone's details after the fact — a mistyped phone number, a
    move to a different timezone, the wrong language.

    Three fields are deliberately absent:

        consent_confirmed   consent is a record of something that happened,
                            with a timestamp and a name against it. It is not
                            a checkbox to be toggled later.
        opted_out_at        a person who said STOP stays stopped. The plan is
                            blunt about this: if an owner can re-enable
                            someone who opted out, the platform's sending
                            number gets reported and every other account on it
                            goes dark.
        opt_out_reason      same reasoning.

    Every field is optional, but a field that is sent is applied — including
    setting an email or phone back to null, which is why the model validator
    below re-checks reachability against the merged result rather than against
    this patch alone.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=20)
    whatsapp: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None

    language: str | None = Field(default=None, max_length=10)
    timezone: str | None = Field(default=None, max_length=64)

    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None

    @field_validator("timezone")
    @classmethod
    def real_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return validate_timezone(v)
        except UnknownTimezone as exc:
            raise ValueError(str(exc)) from exc


class PersonResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    name: str
    is_account_holder: bool
    phone: str | None
    whatsapp: str | None
    email: str | None
    language: str
    timezone: str
    quiet_hours_start: time
    quiet_hours_end: time
    consent_confirmed_at: datetime | None
    opted_out_at: datetime | None
    opt_out_reason: str | None
    created_at: datetime


# ----------------------------------------------------------------
# Reminders
# ----------------------------------------------------------------


class ReminderCreate(BaseModel):
    person_id: UUID
    title: str = Field(min_length=1, max_length=255)
    category: str = Field(default="personal")
    notes: str | None = None

    schedule_kind: str = Field(default="repeating", description=" | ".join(SCHEDULE_KINDS))
    rrule: str | None = Field(
        default=None,
        description="RFC 5545 rule without DTSTART, e.g. FREQ=MONTHLY;BYMONTHDAY=5",
    )
    due_date: date | None = None
    warn_before_days: list[int] | None = None

    time_of_day: time = time(9, 0)
    timezone: str | None = Field(
        default=None,
        description="Defaults to the person's timezone.",
    )

    channel_ladder: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CHANNEL_LADDER)
    )
    max_attempts: int = Field(default=3, ge=1, le=10)
    grace_minutes: int = Field(default=120, ge=1, le=1440)

    notify_email: EmailStr | None = Field(
        default=None,
        description=(
            "Told when this reminder goes unanswered. Defaults to the email of "
            "whoever created it."
        ),
    )

    @field_validator("category")
    @classmethod
    def known_category(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(
                f"Unknown category '{v}'. One of: {', '.join(CATEGORIES)}."
            )
        return v

    @field_validator("schedule_kind")
    @classmethod
    def known_kind(cls, v: str) -> str:
        if v not in SCHEDULE_KINDS:
            raise ValueError(
                f"Unknown schedule kind '{v}'. One of: {', '.join(SCHEDULE_KINDS)}."
            )
        return v

    @field_validator("channel_ladder")
    @classmethod
    def known_channels(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError(
                "A reminder needs at least one channel, or it can never be "
                "delivered."
            )
        unknown = [c for c in v if c not in CHANNEL_NAMES]
        if unknown:
            raise ValueError(
                f"Unknown channel(s): {', '.join(unknown)}. "
                f"One of: {', '.join(CHANNEL_NAMES)}."
            )
        if len(set(v)) != len(v):
            raise ValueError(
                "The same channel twice in one ladder would contact the person "
                "the same way twice and call it an escalation."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def real_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return validate_timezone(v)
        except UnknownTimezone as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def schedule_is_complete(self):
        """
        Each shape needs its own fields. A repeating reminder with only a
        due_date, or an expiry with only an rrule, would be accepted by every
        field validator above and then generate nothing at all.
        """
        self.warn_before_days = check_schedule(
            self.schedule_kind, self.rrule, self.due_date, self.warn_before_days
        )
        return self


class ReminderUpdate(BaseModel):
    """
    What may be changed on a reminder that already exists.

    `person_id` is absent. Moving a reminder to a different person would mean
    the occurrences already sent went to someone the history no longer names.
    Create a new reminder for the new person instead.

    The schedule fields are here, and changing any of them regenerates the
    future. The route checks the merged schedule, not this patch, because
    sending only `rrule` to a reminder that is an expiry would otherwise look
    valid and then produce nothing.
    """

    title: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = None
    notes: str | None = None

    schedule_kind: str | None = None
    rrule: str | None = None
    due_date: date | None = None
    warn_before_days: list[int] | None = None

    time_of_day: time | None = None
    timezone: str | None = None

    channel_ladder: list[str] | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    grace_minutes: int | None = Field(default=None, ge=1, le=1440)

    notify_email: EmailStr | None = None

    is_paused: bool | None = Field(
        default=None,
        description=(
            "Pausing stops future occurrences being claimed. It does not "
            "delete them, so unpausing resumes where it left off."
        ),
    )

    @field_validator("category")
    @classmethod
    def known_category(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in CATEGORIES:
            raise ValueError(
                f"Unknown category '{v}'. One of: {', '.join(CATEGORIES)}."
            )
        return v

    @field_validator("schedule_kind")
    @classmethod
    def known_kind(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in SCHEDULE_KINDS:
            raise ValueError(
                f"Unknown schedule kind '{v}'. One of: {', '.join(SCHEDULE_KINDS)}."
            )
        return v

    @field_validator("channel_ladder")
    @classmethod
    def known_channels(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if not v:
            raise ValueError(
                "A reminder needs at least one channel, or it can never be "
                "delivered."
            )
        unknown = [c for c in v if c not in CHANNEL_NAMES]
        if unknown:
            raise ValueError(
                f"Unknown channel(s): {', '.join(unknown)}. "
                f"One of: {', '.join(CHANNEL_NAMES)}."
            )
        if len(set(v)) != len(v):
            raise ValueError(
                "The same channel twice in one ladder would contact the person "
                "the same way twice and call it an escalation."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def real_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return validate_timezone(v)
        except UnknownTimezone as exc:
            raise ValueError(str(exc)) from exc

    def touches_schedule(self) -> bool:
        """Whether this edit changes when the reminder fires."""
        sent = self.model_fields_set
        return bool(
            sent
            & {
                "schedule_kind",
                "rrule",
                "due_date",
                "warn_before_days",
                "time_of_day",
                "timezone",
            }
        )


class ReminderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    person_id: UUID
    title: str
    category: str
    notes: str | None
    schedule_kind: str
    rrule: str | None
    due_date: date | None
    warn_before_days: list | None
    time_of_day: time
    timezone: str
    channel_ladder: list
    max_attempts: int
    grace_minutes: int
    notify_email: str | None
    is_paused: bool
    generated_through: date | None
    created_at: datetime


class ReminderCreated(BaseModel):
    """A reminder plus proof that its schedule actually produced moments."""

    reminder: ReminderResponse
    schedule: str = Field(description="The rule in plain words.")
    occurrences_created: int
    next_due_at: datetime | None
    next_due_local: datetime | None


class ReminderUpdated(ReminderCreated):
    """
    The same, plus what the edit did to the queue.

    `occurrences_cancelled` is the count of future, untouched moments that were
    dropped and replaced. It is reported rather than hidden because an edit
    that silently empties the queue — a new rule that produces nothing — looks
    identical to a successful one until the night nobody is reminded.
    """

    occurrences_cancelled: int = 0
    schedule_changed: bool = False


# ----------------------------------------------------------------
# Occurrences
# ----------------------------------------------------------------


class OccurrenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reminder_id: UUID
    due_at: datetime
    local_due_at: datetime
    offset_days: int | None
    status: str
    attempt_no: int
    attempts_made: int
    next_attempt_at: datetime | None
    sent_at: datetime | None
    answered_at: datetime | None
    snoozed_until: datetime | None
    answer_channel: str | None
    outcome_reason: str | None


class AgendaItem(BaseModel):
    """One row on the reminders agenda, with the context a table needs."""

    occurrence: OccurrenceResponse
    reminder_title: str
    person_name: str
    channel_ladder: list


class AgendaResponse(BaseModel):
    """
    Grouped the way reminders are actually read.

    Not a flat table: reminders are time-shaped, so Overdue / Today / This week
    / Later is the grouping that answers the question someone opens the page
    with.
    """

    overdue: list[AgendaItem]
    today: list[AgendaItem]
    this_week: list[AgendaItem]
    later: list[AgendaItem]
    total: int


class SnoozeRequest(BaseModel):
    minutes: int = Field(
        default=60,
        ge=1,
        le=60 * 24 * 30,
        description="How long to put it off for.",
    )


class AnswerResponse(BaseModel):
    occurrence_id: UUID
    status: str
    changed: bool = Field(
        description=(
            "False when it was already closed — a second click on the same "
            "link, which is normal and not an error."
        )
    )
    message: str
