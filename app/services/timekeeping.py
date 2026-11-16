"""
Voice Agent Platform — Timekeeping

Every "21:00 medicine reminder fired at 02:30" bug lives in the conversion
between a wall-clock time a person chose and the instant a machine fires at.
All of that conversion is here, so there is one place to get it right and one
place to test.

The rule, which the whole engine follows:

    compute in the person's timezone, persist UTC, display local

Two cases make this harder than a call to astimezone, and both are real:

    nonexistent    on a spring-forward day the wall clock skips an hour. In
                   Europe/London on 30 March 2025 there is no 01:30. A daily
                   01:30 reminder has to fire at *some* point that day.

    ambiguous      on a fall-back day a wall time happens twice. In
                   America/New_York on 2 November 2025, 01:30 occurs twice.
                   One of them has to be chosen.

India has no DST, so none of this bites today — which is exactly why it has to
be written now rather than discovered by a customer in London later.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.logger import get_logger

logger = get_logger(__name__)

# Fallback when a person's stored timezone is not a name this system knows —
# a typo, or a zone removed from the tz database. Guessing is better than
# crashing the tick for every other reminder, but it is logged loudly.
DEFAULT_TIMEZONE = "Asia/Kolkata"

# How far forward to search for a real instant when a wall time does not exist.
# DST gaps are an hour nearly everywhere and 30 minutes on Lord Howe Island, so
# 15-minute steps over three hours covers every zone in the database.
_GAP_STEP = timedelta(minutes=15)
_GAP_MAX_STEPS = 12


class UnknownTimezone(ValueError):
    """Raised by validate_timezone for a name the tz database does not have."""


def validate_timezone(name: str) -> str:
    """
    Check a timezone name at the edge of the system, where a human can be told.

    Called by the create routes so a bad zone is a 422 at setup time rather
    than a surprise in the tick loop at 21:00.
    """
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezone(
            f"'{name}' is not a known timezone name. Use an IANA name such as "
            "'Asia/Kolkata'."
        ) from exc
    return name


def get_zone(name: str) -> ZoneInfo:
    """A ZoneInfo for a stored name, falling back loudly rather than raising."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.error(
            "unknown_timezone_falling_back",
            requested=name,
            using=DEFAULT_TIMEZONE,
        )
        return ZoneInfo(DEFAULT_TIMEZONE)


def to_utc(local_naive: datetime, tz_name: str) -> datetime:
    """
    Turn a wall-clock time in some timezone into the UTC instant it happens at.

    `local_naive` must have no tzinfo: it is what a person wrote on a form.

    Ambiguous wall times (the hour that happens twice on a fall-back day)
    resolve to the *first* of the two, via fold=0. That choice is deliberate:
    the earlier instant means a reminder is never late, and "an hour early
    once a year" is a smaller failure than "an hour late once a year" for a
    medicine reminder.

    Nonexistent wall times (the hour the clock skips on a spring-forward day)
    move forward to the first instant that does exist, so a 01:30 reminder on
    that one day fires at 02:00 rather than not at all.
    """
    if local_naive.tzinfo is not None:
        raise ValueError("to_utc expects a naive datetime (no tzinfo)")

    zone = get_zone(tz_name)
    candidate = local_naive

    for _ in range(_GAP_MAX_STEPS):
        # fold=0 picks the earlier instant when this wall time happens twice.
        aware = candidate.replace(tzinfo=zone, fold=0)
        as_utc = aware.astimezone(timezone.utc)

        # The round-trip test for a nonexistent time: convert to UTC and back.
        # If the wall clock never showed this time, the value that comes back
        # is not the value that went in.
        if as_utc.astimezone(zone).replace(tzinfo=None) == candidate:
            return as_utc

        candidate += _GAP_STEP

    # No real instant within three hours. This should be unreachable; falling
    # back to a plain conversion is still better than dropping the reminder.
    logger.error(
        "no_valid_local_instant",
        local=local_naive.isoformat(),
        timezone=tz_name,
    )
    return local_naive.replace(tzinfo=zone).astimezone(timezone.utc)


def to_local(instant: datetime, tz_name: str) -> datetime:
    """The naive wall-clock time a UTC instant shows as in some timezone."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(get_zone(tz_name)).replace(tzinfo=None)


def combine_local(day: date, at: time, tz_name: str) -> tuple[datetime, datetime]:
    """
    A date plus a wall-clock time, as (utc_instant, local_naive).

    Both halves are returned because occurrences store both: UTC to fire on,
    local to show. The local value returned is the *corrected* one — if the
    wall time did not exist that day, this is the time it actually fires at,
    not the one that was asked for. Storing the asked-for time would make the
    dashboard disagree with reality on exactly the day that is hardest to
    debug.
    """
    asked = datetime.combine(day, at)
    instant = to_utc(asked, tz_name)
    return instant, to_local(instant, tz_name)


def is_within_quiet_hours(local: datetime | time, start: time, end: time) -> bool:
    """
    Whether a local wall-clock time falls inside quiet hours.

    Quiet hours normally wrap midnight (22:00 to 07:00), so this is not a
    simple `start <= t < end`. Getting that wrong inverts the window and calls
    people only at night, which is the single worst bug this product could
    ship.

    start == end is treated as no quiet hours at all, rather than as a
    24-hour-wide window that would block every reminder forever.
    """
    t = local.time() if isinstance(local, datetime) else local

    if start == end:
        return False
    if start < end:
        # A window inside one day: 01:00 to 06:00.
        return start <= t < end
    # A window across midnight: 22:00 to 07:00.
    return t >= start or t < end


def next_quiet_hours_end(
    local: datetime, start: time, end: time, tz_name: str
) -> tuple[datetime, datetime]:
    """
    The first moment after `local` that is outside quiet hours.

    Returns (utc_instant, local_naive), ready to write straight onto an
    occurrence's due_at / local_due_at. Moving due_at rather than merely
    deferring the send is what keeps the grace window honest: when the
    reminder does go out at 07:00 it is on time for 07:00, not twelve hours
    late for 19:00.
    """
    if not is_within_quiet_hours(local, start, end):
        return to_utc(local, tz_name), local

    # Quiet hours end at `end` — today if that is still ahead, otherwise
    # tomorrow. For a window across midnight, a 23:30 reminder waits for the
    # following morning; a 03:00 one waits only a few hours.
    candidate = datetime.combine(local.date(), end)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), end)

    instant = to_utc(candidate, tz_name)
    return instant, to_local(instant, tz_name)


def utcnow() -> datetime:
    """
    Timezone-aware current UTC time.

    One function rather than datetime.now(timezone.utc) scattered through the
    engine, so tests can freeze the clock in one place.
    """
    return datetime.now(timezone.utc)
