"""
Voice Agent Platform — Occurrence Generation

Turns a rule into dated rows. This is the half of the engine that decides
*when*; the engine decides *whether* and *how*.

Two shapes generate today:

    repeating   an RRULE expanded in the reminder's own timezone
    expiry      one due date fanned out into several warnings before it

The third shape in the plan, `balance` ("FASTag running low"), needs an API
call per provider and generates nothing yet.

Expansion happens in LOCAL wall-clock time and only then converts to UTC. The
reverse — expanding in UTC and converting for display — looks equivalent and
is not: "the 5th of every month at 09:00" has to stay 09:00 across a DST
boundary, and only a local expansion does that.

Nothing in the past is ever generated. A row due before now would either fire
immediately or be marked missed on arrival, and neither is what someone means
when they set up a reminder.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from dateutil.rrule import rrulestr

from app.logger import get_logger
from app.models.reminder import DEFAULT_WARN_BEFORE_DAYS, Reminder
from app.services.timekeeping import combine_local, to_local, utcnow

logger = get_logger(__name__)

# An RRULE is expanded by a library that will happily accept a rule producing
# millions of dates. This caps one expansion, so a malformed FREQ=SECONDLY
# cannot exhaust memory for every other account.
MAX_OCCURRENCES_PER_RUN = 500


class InvalidSchedule(ValueError):
    """A rule that cannot produce occurrences. Raised at the API edge."""


@dataclass(frozen=True)
class OccurrenceSpec:
    """One moment to be written, before it is a database row."""

    due_at: datetime        # UTC, aware
    local_due_at: datetime  # wall clock, naive
    offset_days: int | None # expiry shape only: days before the real due date


def validate_rrule(rule: str) -> str:
    """
    Check an RRULE string before it is stored.

    DTSTART is rejected rather than honoured: the engine supplies it from the
    reminder's timezone and time_of_day, and a DTSTART inside the string would
    silently override the time the person chose.
    """
    text = (rule or "").strip()
    if not text:
        raise InvalidSchedule("A repeating reminder needs a repeat rule.")
    if "DTSTART" in text.upper():
        raise InvalidSchedule(
            "Leave DTSTART out of the rule — the reminder's own date and time "
            "are used instead."
        )
    try:
        rrulestr(text, dtstart=datetime(2026, 1, 1, 9, 0))
    except Exception as exc:
        raise InvalidSchedule(f"Could not read that repeat rule: {exc}") from exc
    return text


def normalise_warn_before_days(values: list | None) -> list[int]:
    """
    Clean a warning ladder: whole days, no duplicates, furthest-out first.

    An empty list becomes the default ladder rather than staying empty. An
    expiry reminder with no warnings is a reminder that only tells you on the
    day it is already too late — the exact failure the plan calls out for
    passports.
    """
    if not values:
        return list(DEFAULT_WARN_BEFORE_DAYS)

    cleaned: set[int] = set()
    for v in values:
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise InvalidSchedule(f"'{v}' is not a number of days.")
        if n < 0:
            raise InvalidSchedule(
                "A warning cannot be a negative number of days before the date."
            )
        if n > 3650:
            raise InvalidSchedule("Warnings more than ten years ahead are not useful.")
        cleaned.add(n)

    return sorted(cleaned, reverse=True)


def generate(
    reminder: Reminder,
    *,
    horizon_days: int = 60,
    now: datetime | None = None,
) -> list[OccurrenceSpec]:
    """
    Every moment this rule is due between now and `horizon_days` ahead.

    Safe to call repeatedly: it returns specs, and the unique index on
    (reminder_id, due_at) is what makes writing them idempotent. That is why
    the nightly top-up can re-run without a cursor and without duplicating.
    """
    now = now or utcnow()
    window_end = now + timedelta(days=horizon_days)

    if reminder.schedule_kind == "repeating":
        return _generate_repeating(reminder, now, window_end)
    if reminder.schedule_kind == "expiry":
        return _generate_expiry(reminder, now, window_end)
    if reminder.schedule_kind == "balance":
        # Needs a per-provider API call to know whether anything is due.
        return []

    raise InvalidSchedule(f"Unknown schedule kind '{reminder.schedule_kind}'.")


def _generate_repeating(
    reminder: Reminder, now: datetime, window_end: datetime
) -> list[OccurrenceSpec]:
    rule_text = validate_rrule(reminder.rrule)
    tz = reminder.timezone

    # The window, as wall-clock time in the reminder's own timezone. The rule
    # is expanded against these, so "the 5th" means the 5th where the person
    # lives, not the 5th in UTC.
    local_now = to_local(now, tz)
    local_end = to_local(window_end, tz)

    # DTSTART anchors the clock time. It is placed a little before the window
    # so a rule whose first date is today is not missed, while staying close
    # enough that dateutil is not walking years of history to reach the window.
    dtstart = datetime.combine(
        (local_now - timedelta(days=1)).date(), reminder.time_of_day
    )

    try:
        rule = rrulestr(rule_text, dtstart=dtstart)
    except Exception as exc:
        raise InvalidSchedule(f"Could not read that repeat rule: {exc}") from exc

    specs: list[OccurrenceSpec] = []
    for local_naive in rule.between(local_now, local_end, inc=True):
        # The rule can carry its own hour (FREQ=HOURLY, or a BYHOUR clause).
        # When it does not, dtstart's time is what comes back — which is
        # time_of_day, as intended.
        due_at, corrected_local = combine_local(
            local_naive.date(), local_naive.time(), tz
        )

        # between() works in local wall time, so a rule date can still land
        # before `now` once converted — most visibly on a DST boundary.
        if due_at <= now:
            continue

        specs.append(
            OccurrenceSpec(
                due_at=due_at, local_due_at=corrected_local, offset_days=None
            )
        )

        if len(specs) >= MAX_OCCURRENCES_PER_RUN:
            logger.warning(
                "occurrence_generation_capped",
                reminder_id=str(reminder.id),
                cap=MAX_OCCURRENCES_PER_RUN,
                rrule=rule_text,
            )
            break

    return specs


def _generate_expiry(
    reminder: Reminder, now: datetime, window_end: datetime
) -> list[OccurrenceSpec]:
    """
    One expiry date, several warnings before it.

        Passport expires 12 March 2027
          12 Jun 2026   nine months left, time to apply
          12 Dec 2026   three months left
          12 Feb 2027   one month left
          12 Mar 2027   expires today

    Each occurrence keeps its offset_days, which is what lets those four rows
    say four different things instead of repeating the same sentence.
    """
    if reminder.due_date is None:
        raise InvalidSchedule("An expiry reminder needs a date it expires on.")

    due_on: date = reminder.due_date
    tz = reminder.timezone
    specs: list[OccurrenceSpec] = []

    for offset in normalise_warn_before_days(reminder.warn_before_days):
        warn_day = due_on - timedelta(days=offset)
        due_at, local_due_at = combine_local(warn_day, reminder.time_of_day, tz)

        # Warnings already behind us are skipped, not fired late. Setting up a
        # passport reminder in 2026 should not immediately send the
        # nine-months-out warning for a date that is four months away.
        if due_at <= now or due_at > window_end:
            continue

        specs.append(
            OccurrenceSpec(
                due_at=due_at, local_due_at=local_due_at, offset_days=offset
            )
        )

    return specs


def describe(reminder: Reminder) -> str:
    """
    The rule in plain words, for logs and for the agenda screen.

    Deliberately small: it covers the rules the product actually offers and
    falls back to the raw rule rather than guessing at an RRULE nobody built a
    control for.
    """
    at = reminder.time_of_day.strftime("%H:%M")

    if reminder.schedule_kind == "expiry":
        if reminder.due_date is None:
            return "No date set"
        warnings = normalise_warn_before_days(reminder.warn_before_days)
        # Built by hand rather than with strftime("%-d"): the no-pad directive
        # is platform-specific and is not supported on Windows, where this also
        # has to run.
        d = reminder.due_date
        when = f"{d.day} {d.strftime('%b %Y')}"
        return f"Expires {when} · {len(warnings)} warnings"

    if reminder.schedule_kind == "balance":
        return f"Balance check at {at}"

    rule = (reminder.rrule or "").upper()
    if "FREQ=DAILY" in rule:
        return f"Every day at {at}"
    if "FREQ=WEEKLY" in rule:
        return f"Every week at {at}"
    if "FREQ=MONTHLY" in rule and "BYMONTHDAY=" in rule:
        day = rule.split("BYMONTHDAY=")[1].split(";")[0]
        return f"Every month on the {_ordinal(day)} at {at}"
    if "FREQ=MONTHLY" in rule:
        return f"Every month at {at}"
    if "FREQ=YEARLY" in rule:
        return f"Every year at {at}"
    return reminder.rrule or "No schedule"


def _ordinal(day: str) -> str:
    try:
        n = int(day)
    except ValueError:
        return day
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
