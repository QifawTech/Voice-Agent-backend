"""
Voice Agent Platform — The Reminder Engine

Three loops and one doorway. This is the whole of what makes a reminder happen,
and it is identical for Nudge and for Vasool: the tick, the escalation ladder,
quiet hours, the claim lock and the delivery log do not know which product they
are serving. Nudge plugs in a generator that reads repeat rules; Vasool, in
phase 5, plugs in one that reads invoices and a chase policy. That is the
entire difference in code.

    loop 1   tick        every minute: what is due now, and send it
    loop 2   escalate    every minute: what was sent and not answered
    loop 3   top up      nightly: generate the next 60 days of occurrences
    doorway  answers     done / snooze, from a call, a button or a link

The three traps the plan names, and where each is handled here:

    timezones         _process_one uses the person's own zone for quiet hours
                      and the reminder's for due times; all conversion lives in
                      services/timekeeping.py
    double sending    _claim_due, below — a row is locked and marked in one
                      transaction, so a restart at the wrong second cannot
                      send twice
    catching up       the grace window in _process_one — a three-hour-late
                      reminder is marked missed, not fired at midnight

One deliberate design choice worth knowing before reading: a failure to *send*
does not wait. The ladder's waits exist for "no answer", so when SMTP rejects a
message the engine moves straight to the next rung inside the same pass rather
than sleeping five minutes first.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.config import get_settings
from app.database import async_session_factory
from app.logger import get_logger
from app.models.agent import Agent
from app.models.delivery import Delivery
from app.models.occurrence import TERMINAL_STATUSES, Occurrence
from app.models.person import Person
from app.models.reminder import Reminder
from app.services import message_builder, occurrence_generator
from app.services.channels import build_channels
from app.services.channels.base import Channel
from app.services.timekeeping import (
    is_within_quiet_hours,
    next_quiet_hours_end,
    to_local,
    utcnow,
)

logger = get_logger(__name__)

# How long to wait for an answer after a send, per channel, before climbing to
# the next rung. From the plan's ladder: a call that is not picked up is known
# within a minute, so five minutes is generous; a WhatsApp message deserves
# half an hour before being treated as unread.
WAIT_AFTER_SEND = {
    "call": timedelta(minutes=5),
    "whatsapp": timedelta(minutes=30),
    "email": timedelta(minutes=30),
    "sms": timedelta(minutes=15),
}
DEFAULT_WAIT = timedelta(minutes=30)

# Default snooze when a person says "remind me later" without saying when.
DEFAULT_SNOOZE = timedelta(hours=1)


@dataclass
class LoopStats:
    """What one pass of a loop did. Logged, and used by the tests."""

    claimed: int = 0
    sent: int = 0
    deferred: int = 0   # moved out of quiet hours
    missed: int = 0
    cancelled: int = 0
    recovered: int = 0  # stale claims handed back
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"claimed={self.claimed} sent={self.sent} deferred={self.deferred} "
            f"missed={self.missed} cancelled={self.cancelled} "
            f"recovered={self.recovered} errors={len(self.errors)}"
        )


class ReminderEngine:
    """The clock, the ladder and the log."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        channels: dict[str, Channel] | None = None,
        now_fn=utcnow,
    ) -> None:
        # Injected so the tests can hand in a fake channel map and a frozen
        # clock. Everything below reads the clock through self._now(), never
        # datetime.now, which is what makes a DST test possible at all.
        self._session_factory = session_factory or async_session_factory
        self._channels = channels if channels is not None else build_channels()
        self._now = now_fn

    # ------------------------------------------------------------------
    # Loop 1 — the tick
    # ------------------------------------------------------------------

    async def tick(self, *, limit: int | None = None) -> LoopStats:
        """
        Find everything due, claim it, send it.

        Runs every minute. Claiming happens in one short transaction for the
        whole batch; sending happens afterwards, outside it, because holding a
        database transaction open across an SMTP round trip is how a connection
        pool runs dry.
        """
        settings = get_settings()
        limit = limit or settings.REMINDER_TICK_BATCH
        now = self._now()

        stats = LoopStats()
        stats.recovered = await self.recover_stale_claims(now=now)

        ids = await self._claim_due(now=now, limit=limit)
        stats.claimed = len(ids)
        if not ids:
            return stats

        await self._process_all(ids, now=now, stats=stats)

        logger.info("tick_complete", **_loggable(stats))
        return stats

    async def _claim_due(self, *, now: datetime, limit: int) -> list:
        """
        Take ownership of up to `limit` due occurrences.

        This is the single most important function in the engine, so it is
        worth reading slowly. It defends against double sending with two
        independent mechanisms, and either one alone would be too weak:

        1. SELECT ... FOR UPDATE SKIP LOCKED
           The database locks each selected row for the life of this
           transaction. SKIP LOCKED means a second worker running the same
           query at the same moment does not block and does not wait — it
           simply does not see these rows, and goes off with a different
           hundred. Without SKIP LOCKED, two workers would queue behind each
           other and the second would wake up to find every row already taken.

        2. status = 'claimed', committed before anything is sent
           The lock dies with the transaction. The status change outlives it,
           which is what protects against the harder case: this process
           crashing after the commit, or a *restart* bringing up a worker that
           queries the same window a second later. The WHERE clause on the next
           tick's query no longer matches these rows, so they cannot be picked
           up twice.

        The pair is what the plan calls the conditional claim. The important
        property is that the decision "who owns this row" is made by the
        database, not by the application: no amount of restarting, scaling to
        more workers, or two schedulers running by mistake can produce two
        sends, because both would have to win the same row lock.

        Paused reminders are filtered here rather than skipped later, so a
        paused reminder's occurrences are never claimed at all and need no
        unwinding.
        """
        candidates = (
            select(Occurrence.id)
            .join(Reminder, Reminder.id == Occurrence.reminder_id)
            .join(Agent, Agent.id == Reminder.agent_id)
            .where(
                Occurrence.status == "scheduled",
                Occurrence.due_at <= now,
                Reminder.is_paused.is_(False),
                Reminder.deleted_at.is_(None),
                Agent.is_active.is_(True),
            )
            # Oldest first: after an outage the backlog drains in the order
            # people were promised, not in whatever order the planner likes.
            .order_by(Occurrence.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True, of=Occurrence)
        )

        async with self._session_factory() as session:
            async with session.begin():
                rows = await session.execute(candidates)
                ids = [row[0] for row in rows.all()]
                if ids:
                    await session.execute(
                        update(Occurrence)
                        .where(Occurrence.id.in_(ids))
                        .values(status="claimed", claimed_at=now, updated_at=now)
                    )
        # The transaction has committed here: the claim is durable and the row
        # locks are released. Only now is it safe to start sending.
        return ids

    async def recover_stale_claims(self, *, now: datetime | None = None) -> int:
        """
        Hand back rows claimed by a process that died before sending.

        Without this, a crash between claim and send strands an occurrence in
        'claimed' forever: no loop looks at that status, so the reminder simply
        never happens and nothing reports it. The plan's own failure table has
        this shape — "scheduler dies silently, nobody notices for days" — and
        the claim state is the quietest version of it.

        A recovered row goes back to 'scheduled' with its original due_at, so
        the grace window gets to decide whether it is still worth sending. That
        is the right judge: if the process was down for four hours, a medicine
        reminder should be marked missed, not delivered at 01:00.
        """
        now = now or self._now()
        settings = get_settings()
        cutoff = now - timedelta(minutes=settings.REMINDER_CLAIM_TIMEOUT_MINUTES)

        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(Occurrence)
                    .where(
                        Occurrence.status == "claimed",
                        Occurrence.claimed_at <= cutoff,
                    )
                    .values(status="scheduled", claimed_at=None, updated_at=now)
                )
        count = result.rowcount or 0
        if count:
            logger.warning(
                "stale_claims_recovered",
                count=count,
                claim_timeout_minutes=settings.REMINDER_CLAIM_TIMEOUT_MINUTES,
            )
        return count

    async def _process_all(
        self, ids: list, *, now: datetime, stats: LoopStats
    ) -> None:
        """
        Send the claimed batch, a few at a time.

        Bounded concurrency rather than one-at-a-time: the plan's own load test
        is 500 reminders due in the same minute, and at 200ms per email a
        sequential loop needs 100 seconds to clear what has to be cleared
        inside 60. Bounded rather than unbounded because 500 simultaneous SMTP
        connections is how a sending reputation gets destroyed.
        """
        settings = get_settings()
        semaphore = asyncio.Semaphore(settings.REMINDER_SEND_CONCURRENCY)

        async def run(occurrence_id):
            async with semaphore:
                try:
                    return await self._process_one(occurrence_id, now=now)
                except Exception as exc:
                    # One unexpected failure must not abandon the other 99
                    # claimed rows — they would sit in 'claimed' until the
                    # stale-claim recovery above picks them up.
                    logger.exception(
                        "occurrence_processing_failed",
                        occurrence_id=str(occurrence_id),
                        error=str(exc),
                    )
                    return f"error:{type(exc).__name__}"

        outcomes = await asyncio.gather(*(run(i) for i in ids))

        for outcome in outcomes:
            if outcome == "sent":
                stats.sent += 1
            elif outcome == "deferred":
                stats.deferred += 1
            elif outcome == "missed":
                stats.missed += 1
            elif outcome == "cancelled":
                stats.cancelled += 1
            elif isinstance(outcome, str) and outcome.startswith("error:"):
                stats.errors.append(outcome)

    async def _process_one(self, occurrence_id, *, now: datetime) -> str:
        """
        Decide what happens to one claimed occurrence, and do it.

        The order of the checks below is load-bearing, and each one is a
        decision the plan makes explicitly:

        1. opted out      absolute, and checked first. Someone who said STOP
                          is not contacted, whatever else is true.
        2. grace window   before quiet hours, because a reminder that is
                          already hours late is not made useful by being
                          delayed further.
        3. quiet hours    moves due_at rather than merely deferring the send,
                          so that when it goes out at 07:00 it is on time for
                          07:00 instead of twelve hours late for 19:00.
        4. the ladder     first rung that exists and has an address.
        """
        async with self._session_factory() as session:
            occurrence = await self._load(session, occurrence_id)
            if occurrence is None:
                return "gone"

            # Defensive: only this worker should hold a claimed row, but a
            # manual database edit or a future code path could change that, and
            # re-sending is the one mistake worth paying a query to avoid.
            if occurrence.status != "claimed":
                return "skipped"

            reminder = occurrence.reminder
            person = reminder.person

            # ---- 1. Opted out ----
            if not person.is_contactable:
                occurrence.status = "cancelled"
                occurrence.outcome_reason = (
                    f"{person.name} opted out on "
                    f"{person.opted_out_at:%Y-%m-%d} and is never contacted again."
                )
                occurrence.updated_at = now
                await session.commit()
                logger.info(
                    "occurrence_cancelled_opted_out",
                    occurrence_id=str(occurrence.id),
                )
                return "cancelled"

            # ---- 2. Grace window ----
            lateness = now - occurrence.due_at
            grace = timedelta(minutes=reminder.grace_minutes)
            if lateness > grace:
                occurrence.status = "missed"
                occurrence.outcome_reason = (
                    f"Too late to be useful: due "
                    f"{_minutes(lateness)} ago, and this reminder's grace "
                    f"window is {reminder.grace_minutes} minutes."
                )
                occurrence.updated_at = now
                await session.commit()
                logger.warning(
                    "occurrence_missed_past_grace",
                    occurrence_id=str(occurrence.id),
                    late_minutes=int(lateness.total_seconds() // 60),
                    grace_minutes=reminder.grace_minutes,
                )
                await self._notify_setter(reminder, person, occurrence)
                return "missed"

            # ---- 3. Quiet hours ----
            # The person's own timezone, not the reminder's: quiet hours are a
            # fact about when someone sleeps, so they follow the person if they
            # move, while an existing reminder keeps the clock time it was set
            # with.
            local_now = to_local(now, person.timezone)
            if is_within_quiet_hours(
                local_now, person.quiet_hours_start, person.quiet_hours_end
            ):
                new_due_at, new_local = next_quiet_hours_end(
                    local_now,
                    person.quiet_hours_start,
                    person.quiet_hours_end,
                    person.timezone,
                )
                occurrence.status = "scheduled"
                occurrence.claimed_at = None
                occurrence.due_at = new_due_at
                occurrence.local_due_at = new_local
                occurrence.outcome_reason = (
                    f"Held until {new_local:%H:%M} — quiet hours are "
                    f"{person.quiet_hours_start:%H:%M} to "
                    f"{person.quiet_hours_end:%H:%M}."
                )
                occurrence.updated_at = now
                await session.commit()
                logger.info(
                    "occurrence_deferred_quiet_hours",
                    occurrence_id=str(occurrence.id),
                    moved_to=new_due_at.isoformat(),
                )
                return "deferred"

            # ---- 4. Climb the ladder ----
            return await self._attempt_from(
                session,
                occurrence=occurrence,
                reminder=reminder,
                person=person,
                start_index=occurrence.attempt_no,
                now=now,
            )

    # ------------------------------------------------------------------
    # Loop 2 — escalation
    # ------------------------------------------------------------------

    async def escalate(self, *, limit: int | None = None) -> LoopStats:
        """
        Climb the ladder for anything sent and not answered.

        The plan's shape, exactly: another rung left means send again; no rungs
        left means mark it missed and tell whoever set it up. That last part is
        the feature a caregiver is actually paying for — not knowing that his
        mother took her tablet, but knowing that she did not.
        """
        settings = get_settings()
        limit = limit or settings.REMINDER_TICK_BATCH
        now = self._now()
        stats = LoopStats()

        candidates = (
            select(Occurrence.id)
            .join(Reminder, Reminder.id == Occurrence.reminder_id)
            .where(
                Occurrence.status == "sent",
                Occurrence.next_attempt_at.is_not(None),
                Occurrence.next_attempt_at <= now,
                Reminder.is_paused.is_(False),
                Reminder.deleted_at.is_(None),
            )
            .order_by(Occurrence.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True, of=Occurrence)
        )

        async with self._session_factory() as session:
            async with session.begin():
                rows = await session.execute(candidates)
                ids = [row[0] for row in rows.all()]
                if ids:
                    await session.execute(
                        update(Occurrence)
                        .where(Occurrence.id.in_(ids))
                        .values(status="claimed", claimed_at=now, updated_at=now)
                    )

        stats.claimed = len(ids)
        if not ids:
            return stats

        await self._escalate_all(ids, now=now, stats=stats)
        logger.info("escalation_complete", **_loggable(stats))
        return stats

    async def _escalate_all(
        self, ids: list, *, now: datetime, stats: LoopStats
    ) -> None:
        settings = get_settings()
        semaphore = asyncio.Semaphore(settings.REMINDER_SEND_CONCURRENCY)

        async def run(occurrence_id):
            async with semaphore:
                try:
                    return await self._escalate_one(occurrence_id, now=now)
                except Exception as exc:
                    logger.exception(
                        "escalation_failed",
                        occurrence_id=str(occurrence_id),
                        error=str(exc),
                    )
                    return f"error:{type(exc).__name__}"

        for outcome in await asyncio.gather(*(run(i) for i in ids)):
            if outcome == "sent":
                stats.sent += 1
            elif outcome == "missed":
                stats.missed += 1
            elif outcome == "cancelled":
                stats.cancelled += 1
            elif outcome == "deferred":
                stats.deferred += 1
            elif isinstance(outcome, str) and outcome.startswith("error:"):
                stats.errors.append(outcome)

    async def _escalate_one(self, occurrence_id, *, now: datetime) -> str:
        async with self._session_factory() as session:
            occurrence = await self._load(session, occurrence_id)
            if occurrence is None or occurrence.status != "claimed":
                return "skipped"

            reminder = occurrence.reminder
            person = reminder.person

            if not person.is_contactable:
                occurrence.status = "cancelled"
                occurrence.outcome_reason = f"{person.name} opted out."
                occurrence.updated_at = now
                await session.commit()
                return "cancelled"

            # The hard cap. The plan requires a maximum number of contact
            # attempts and then a full stop: "It never chases forever."
            if occurrence.attempts_made >= reminder.max_attempts:
                return await self._give_up(
                    session,
                    occurrence=occurrence,
                    reminder=reminder,
                    person=person,
                    now=now,
                    reason=(
                        f"No answer after {occurrence.attempts_made} "
                        f"attempts, which is this reminder's limit."
                    ),
                )

            # Quiet hours apply to escalation too. A 21:55 call that goes
            # unanswered must not become a 22:05 WhatsApp message.
            local_now = to_local(now, person.timezone)
            if is_within_quiet_hours(
                local_now, person.quiet_hours_start, person.quiet_hours_end
            ):
                resume_at, new_local = next_quiet_hours_end(
                    local_now,
                    person.quiet_hours_start,
                    person.quiet_hours_end,
                    person.timezone,
                )
                # Stays 'sent' rather than going back to 'scheduled': this
                # occurrence has already been delivered once, so the escalation
                # loop is the right one to pick it up again. next_attempt_at is
                # what that loop reads, so moving that is the whole deferral.
                occurrence.status = "sent"
                occurrence.claimed_at = None
                occurrence.next_attempt_at = resume_at
                occurrence.outcome_reason = (
                    f"Escalation held until {new_local:%H:%M} — quiet hours."
                )
                occurrence.updated_at = now
                await session.commit()
                return "deferred"

            return await self._attempt_from(
                session,
                occurrence=occurrence,
                reminder=reminder,
                person=person,
                start_index=occurrence.attempt_no + 1,
                now=now,
            )

    # ------------------------------------------------------------------
    # Sending — shared by the tick and the escalation loop
    # ------------------------------------------------------------------

    async def _attempt_from(
        self,
        session: AsyncSession,
        *,
        occurrence: Occurrence,
        reminder: Reminder,
        person: Person,
        start_index: int,
        now: datetime,
    ) -> str:
        """
        Try rungs from `start_index` until one send succeeds.

        Two kinds of rung are walked past rather than waited on:

            not configured   'call' before phase 3, 'whatsapp' before phase 4.
                             Logged as a skipped delivery with the reason, and
                             it does not consume one of the person's contact
                             attempts, because nobody was contacted.

            no address       the person has no email, or no phone. Also
                             skipped, also recorded.

        A send that *fails* is different: it counts as an attempt, and the next
        rung is tried immediately. The ladder's waits are for silence, not for
        a broken mail server.
        """
        ladder = list(reminder.channel_ladder or [])

        for index in range(start_index, len(ladder)):
            if occurrence.attempts_made >= reminder.max_attempts:
                break

            channel_name = ladder[index]
            channel = self._channels.get(channel_name)

            if channel is None:
                await self._log_skip(
                    session,
                    occurrence=occurrence,
                    channel_name=channel_name,
                    provider="unknown",
                    reason=f"'{channel_name}' is not a channel this build knows.",
                    index=index,
                    now=now,
                )
                continue

            if not channel.is_configured():
                result = await channel.send()
                await self._log_skip(
                    session,
                    occurrence=occurrence,
                    channel_name=channel_name,
                    provider=channel.provider,
                    reason=result.error or "Channel not configured.",
                    index=index,
                    now=now,
                )
                continue

            if not person.address_for(channel_name):
                await self._log_skip(
                    session,
                    occurrence=occurrence,
                    channel_name=channel_name,
                    provider=channel.provider,
                    reason=f"{person.name} has no {channel_name} address on file.",
                    index=index,
                    now=now,
                )
                continue

            message = message_builder.build(
                reminder=reminder,
                person=person,
                occurrence=occurrence,
                channel=channel_name,
            )
            result = await channel.send(
                person=person,
                reminder=reminder,
                occurrence=occurrence,
                message=message,
            )

            session.add(
                Delivery(
                    occurrence_id=occurrence.id,
                    channel=channel_name,
                    provider=result.provider,
                    recipient=result.recipient,
                    status=result.status,
                    provider_message_id=result.provider_message_id,
                    error=result.error,
                    cost=result.cost,
                    attempt_no=index,
                    created_at=now,
                )
            )

            if result.status == "skipped":
                # The adapter decided it could not try after all — a missing
                # address it knows about and the engine does not.
                continue

            occurrence.attempts_made += 1
            occurrence.attempt_no = index

            if result.ok:
                occurrence.status = "sent"
                occurrence.sent_at = now
                occurrence.claimed_at = None
                occurrence.outcome_reason = None
                occurrence.next_attempt_at = now + WAIT_AFTER_SEND.get(
                    channel_name, DEFAULT_WAIT
                )
                occurrence.updated_at = now
                await session.commit()
                logger.info(
                    "occurrence_sent",
                    occurrence_id=str(occurrence.id),
                    channel=channel_name,
                    rung=index,
                    attempts_made=occurrence.attempts_made,
                    next_attempt_at=occurrence.next_attempt_at.isoformat(),
                )
                return "sent"

            # Failed. Keep climbing in this same pass.
            logger.warning(
                "send_failed_trying_next_rung",
                occurrence_id=str(occurrence.id),
                channel=channel_name,
                error=result.error,
            )

        # Nothing on the ladder could reach this person.
        return await self._give_up(
            session,
            occurrence=occurrence,
            reminder=reminder,
            person=person,
            now=now,
            reason=_give_up_reason(ladder, start_index, occurrence, reminder),
        )

    async def _give_up(
        self,
        session: AsyncSession,
        *,
        occurrence: Occurrence,
        reminder: Reminder,
        person: Person,
        now: datetime,
        reason: str,
    ) -> str:
        """
        Mark an occurrence missed, and tell whoever set it up.

        Missed, never "sent". The plan is specific about this: a reminder that
        did not reach anybody must show as missed, in red, and must not be
        quietly recorded as delivered. A dashboard that rounds failures up to
        successes is worse than no dashboard.
        """
        occurrence.status = "missed"
        occurrence.claimed_at = None
        occurrence.next_attempt_at = None
        occurrence.outcome_reason = reason
        occurrence.updated_at = now
        await session.commit()

        logger.warning(
            "occurrence_missed",
            occurrence_id=str(occurrence.id),
            reminder=reminder.title,
            person=person.name,
            reason=reason,
        )

        await self._notify_setter(reminder, person, occurrence)
        return "missed"

    async def _notify_setter(
        self, reminder: Reminder, person: Person, occurrence: Occurrence
    ) -> None:
        """
        Tell the person who set the reminder up that it was not answered.

        Best-effort and deliberately silent on failure: the occurrence is
        already correctly marked missed, and a bounced notice must not turn
        into an exception that rolls that back.
        """
        if not reminder.notify_email:
            return

        email = self._channels.get("email")
        if email is None or not email.is_configured():
            return
        if not hasattr(email, "send_plain"):
            return

        try:
            notice = message_builder.build_missed_notice(
                reminder=reminder, person=person, occurrence=occurrence
            )
            await email.send_plain(
                to_address=reminder.notify_email, message=notice
            )
            logger.info(
                "missed_notice_sent",
                occurrence_id=str(occurrence.id),
                to=reminder.notify_email,
            )
        except Exception as exc:
            logger.error(
                "missed_notice_failed",
                occurrence_id=str(occurrence.id),
                error=str(exc),
            )

    async def _log_skip(
        self,
        session: AsyncSession,
        *,
        occurrence: Occurrence,
        channel_name: str,
        provider: str,
        reason: str,
        index: int,
        now: datetime,
    ) -> None:
        """
        Record a rung that was never attempted, and why.

        created_at comes from the engine's clock, not from the model's default.
        A skipped row stamped with the wall clock while the sent row beside it
        carries the engine's time puts the delivery log out of order — which is
        invisible in production, where the two are milliseconds apart, and
        wrong every time in a test with a frozen clock. The log is evidence in
        a billing dispute, so its ordering has to come from one source.
        """
        session.add(
            Delivery(
                occurrence_id=occurrence.id,
                channel=channel_name,
                provider=provider,
                status="skipped",
                error=reason,
                attempt_no=index,
                created_at=now,
            )
        )
        logger.info(
            "channel_skipped",
            occurrence_id=str(occurrence.id),
            channel=channel_name,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Loop 3 — nightly top-up
    # ------------------------------------------------------------------

    async def top_up(
        self, *, horizon_days: int | None = None, now: datetime | None = None
    ) -> int:
        """
        Generate the next stretch of occurrences for every active rule.

        Safe to re-run at any time, including twice in a row, because of the
        unique index on (reminder_id, due_at): a row that already exists is
        skipped by the database rather than detected by the application. That
        is what makes this loop need no cursor, no bookkeeping and no care
        about having been interrupted halfway.

        Generating ahead rather than computing on demand is also what makes the
        agenda screen a plain SELECT over a date range, and what means an
        outage cannot break the chain — the rows for tomorrow were written last
        night.
        """
        settings = get_settings()
        horizon = horizon_days or settings.REMINDER_HORIZON_DAYS
        now = now or self._now()
        written = 0

        async with self._session_factory() as session:
            result = await session.execute(
                select(Reminder)
                .join(Agent, Agent.id == Reminder.agent_id)
                .options(joinedload(Reminder.person))
                .where(
                    Reminder.is_paused.is_(False),
                    Reminder.deleted_at.is_(None),
                    Agent.is_active.is_(True),
                )
            )
            reminders = result.unique().scalars().all()

        for reminder in reminders:
            try:
                written += await self.generate_for(
                    reminder, horizon_days=horizon, now=now
                )
            except Exception as exc:
                # One malformed rule must not stop the other reminders from
                # being topped up — that would turn a single bad RRULE into a
                # platform-wide outage tomorrow.
                logger.error(
                    "top_up_failed_for_reminder",
                    reminder_id=str(reminder.id),
                    error=f"{type(exc).__name__}: {exc}",
                )

        logger.info(
            "top_up_complete",
            reminders=len(reminders),
            occurrences_written=written,
            horizon_days=horizon,
        )
        return written

    async def generate_for(
        self,
        reminder: Reminder,
        *,
        horizon_days: int | None = None,
        now: datetime | None = None,
    ) -> int:
        """
        Write the occurrences for one rule. Returns how many were new.

        Called by the nightly loop and again right after a reminder is created,
        so a reminder set up at 20:55 for 21:00 fires tonight rather than
        waiting for a nightly job that has already run.
        """
        settings = get_settings()
        horizon = horizon_days or settings.REMINDER_HORIZON_DAYS
        now = now or self._now()

        specs = occurrence_generator.generate(
            reminder, horizon_days=horizon, now=now
        )
        if not specs:
            return 0

        rows = [
            {
                "reminder_id": reminder.id,
                "due_at": spec.due_at,
                "local_due_at": spec.local_due_at,
                "offset_days": spec.offset_days,
                "status": "scheduled",
                "attempt_no": 0,
                "attempts_made": 0,
                "created_at": now,
                "updated_at": now,
            }
            for spec in specs
        ]

        async with self._session_factory() as session:
            async with session.begin():
                statement = (
                    pg_insert(Occurrence)
                    .values(rows)
                    # The idempotency that makes this loop simple. Not an
                    # application-level "does it exist?" check, which two
                    # workers can both pass at the same moment.
                    .on_conflict_do_nothing(
                        constraint="uq_occurrences_reminder_due"
                    )
                    .returning(Occurrence.id)
                )
                result = await session.execute(statement)
                inserted = len(result.all())

                await session.execute(
                    update(Reminder)
                    .where(Reminder.id == reminder.id)
                    .values(
                        generated_through=(now + timedelta(days=horizon)).date(),
                        updated_at=now,
                    )
                )

        if inserted:
            logger.info(
                "occurrences_generated",
                reminder_id=str(reminder.id),
                requested=len(rows),
                inserted=inserted,
            )
        return inserted

    # ------------------------------------------------------------------
    # The doorway — answers coming back
    # ------------------------------------------------------------------

    async def mark_done(
        self, occurrence_id, *, channel: str = "dashboard"
    ) -> bool:
        """
        Close an occurrence. Returns False if it was already closed.

        The conditional WHERE is what makes this idempotent, and it matters:
        the link in an email can be clicked twice, a mail client can prefetch
        it, and a person can say "done" on the call and then tap the WhatsApp
        button. All of those must mean the same thing as doing it once.

        Terminal statuses are excluded rather than overwritten, so a missed
        occurrence answered an hour later does not rewrite history into a
        success.
        """
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(Occurrence)
                    .where(
                        Occurrence.id == occurrence_id,
                        Occurrence.status.not_in(TERMINAL_STATUSES),
                    )
                    .values(
                        status="done",
                        answered_at=now,
                        answer_channel=channel,
                        next_attempt_at=None,
                        claimed_at=None,
                        updated_at=now,
                    )
                )
        changed = (result.rowcount or 0) > 0
        logger.info(
            "occurrence_answered",
            occurrence_id=str(occurrence_id),
            channel=channel,
            changed=changed,
        )
        return changed

    async def snooze(
        self,
        occurrence_id,
        *,
        until: datetime | None = None,
        channel: str = "dashboard",
    ) -> datetime | None:
        """
        Push this occurrence back, without touching next month's.

        Two separate things happen, and keeping them separate is the point of
        having two tables at all:

            this occurrence   becomes 'snoozed' — terminal for this moment
            a new occurrence  is written at the new time

        The rule is never touched, so "remind me in an hour" tonight does not
        move the 5th of next month. If the engine edited due_at in place
        instead, a snooze would quietly rewrite the schedule, which is the
        classic bug this design exists to prevent.
        """
        now = self._now()
        until = until or (now + DEFAULT_SNOOZE)
        if until <= now:
            raise ValueError("A snooze has to be in the future.")

        async with self._session_factory() as session:
            occurrence = await self._load(session, occurrence_id)
            if occurrence is None:
                return None
            if occurrence.status in TERMINAL_STATUSES:
                return None

            reminder = occurrence.reminder
            local_until = to_local(until, reminder.timezone)

            occurrence.status = "snoozed"
            occurrence.snoozed_until = until
            occurrence.answered_at = now
            occurrence.answer_channel = channel
            occurrence.next_attempt_at = None
            occurrence.claimed_at = None
            occurrence.outcome_reason = (
                f"Snoozed to {local_until:%d %b %H:%M} at the person's request."
            )
            occurrence.updated_at = now

            await session.execute(
                pg_insert(Occurrence)
                .values(
                    reminder_id=reminder.id,
                    due_at=until,
                    local_due_at=local_until,
                    # Kept, so an expiry warning still says "expires in three
                    # months" after being snoozed rather than losing the phrase.
                    offset_days=occurrence.offset_days,
                    status="scheduled",
                    attempt_no=0,
                    attempts_made=0,
                    created_at=now,
                    updated_at=now,
                )
                # A snooze landing on a moment this rule already covers is not
                # an error; the existing row does the job.
                .on_conflict_do_nothing(constraint="uq_occurrences_reminder_due")
            )
            await session.commit()

        logger.info(
            "occurrence_snoozed",
            occurrence_id=str(occurrence_id),
            until=until.isoformat(),
            channel=channel,
        )
        return until

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load(
        self, session: AsyncSession, occurrence_id
    ) -> Occurrence | None:
        """
        Load an occurrence with its reminder and person in one query.

        joinedload rather than letting the relationships lazy-load: a lazy load
        inside an async session raises, and a per-row extra round trip would
        triple the queries in a 500-reminder minute.
        """
        result = await session.execute(
            select(Occurrence)
            .options(joinedload(Occurrence.reminder).joinedload(Reminder.person))
            .where(Occurrence.id == occurrence_id)
        )
        return result.unique().scalar_one_or_none()


def _give_up_reason(
    ladder: list, start_index: int, occurrence: Occurrence, reminder: Reminder
) -> str:
    """A sentence a person can act on, not a status code."""
    if occurrence.attempts_made >= reminder.max_attempts:
        return (
            f"No answer after {occurrence.attempts_made} attempts, which is "
            f"this reminder's limit of {reminder.max_attempts}."
        )
    remaining = ladder[start_index:]
    if not remaining:
        return "Every channel on this reminder's ladder has been tried."
    return (
        "Could not reach them on any remaining channel: "
        + ", ".join(remaining)
        + ". Check the delivery log for the reason on each."
    )


def _minutes(delta: timedelta) -> str:
    total = int(delta.total_seconds() // 60)
    if total < 60:
        return f"{total} minutes"
    hours = total / 60
    return f"{hours:.1f} hours"


def _loggable(stats: LoopStats) -> dict:
    return {
        "claimed": stats.claimed,
        "sent": stats.sent,
        "deferred": stats.deferred,
        "missed": stats.missed,
        "cancelled": stats.cancelled,
        "recovered": stats.recovered,
        "errors": len(stats.errors),
    }


