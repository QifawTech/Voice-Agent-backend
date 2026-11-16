"""
Milestone 1 check — does a real reminder arrive, on time, twice, across a restart?

The plan's finish line for phase 1 is specific, and it is not something a unit
test can assert:

    a reminder set for 21:00 arrives by email at 21:00, twice in a row,
    including across a service restart

This script sets that up against a real mailbox and shows you what happened.
It does not send anything itself — the scheduler does, exactly as it will in
production. That is the point: a check that bypasses the scheduler proves
nothing about the scheduler.

Usage
-----

    # 1. Put your SMTP details in .env (SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM)

    # 2. Set up a reminder that fires every 3 minutes, to your own address
    python verify_reminder_engine.py setup --email you@gmail.com --every 3

    # 3. In another terminal, start the clock and leave it running
    python -m app.scheduler

    # 4. Watch what happens
    python verify_reminder_engine.py status

    # 5. Kill the scheduler with Ctrl+C, start it again, and check again.
    #    Nothing should be sent twice, and nothing should be lost.

    # 6. When you are done
    python verify_reminder_engine.py cleanup

For the literal overnight version, use --at instead of --every:

    python verify_reminder_engine.py setup --email you@gmail.com --at 21:00

That creates a daily 21:00 reminder, which is the real test. Two evenings have
to pass, because that is what "twice in a row" means — the plan is blunt that
days have to pass for days to be tested.

Everything is created under a throwaway tenant called "Milestone 1 check", so
`cleanup` removes all of it and nothing else.
"""

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, time, timedelta, timezone

os.environ.setdefault("SECRET_KEY", "dev-secret")
os.environ.setdefault("ENCRYPTION_KEY", "dev-encryption-key-32bytes-long!!")
os.environ["DEBUG"] = "false"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import delete, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import async_session_factory, close_database  # noqa: E402
from app.models.agent import Agent  # noqa: E402
from app.models.delivery import Delivery  # noqa: E402
from app.models.occurrence import Occurrence  # noqa: E402
from app.models.person import Person  # noqa: E402
from app.models.reminder import Reminder  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.services import occurrence_generator  # noqa: E402
from app.services.reminder_engine import ReminderEngine  # noqa: E402
from app.services.timekeeping import to_local, utcnow  # noqa: E402

TENANT_SLUG = "milestone-1-check"
TENANT_NAME = "Milestone 1 check"


def rule(line: str = "") -> None:
    print(f"\n{line}" if line else "")
    print("-" * 72)


# ----------------------------------------------------------------
# setup
# ----------------------------------------------------------------


async def cmd_setup(args) -> int:
    settings = get_settings()

    print("=" * 72)
    print("Milestone 1 check - setup")
    print("=" * 72)

    if not settings.smtp_configured:
        print(
            "\nSMTP is not configured, so nothing can be delivered.\n"
            "Set these in .env and run this again:\n"
            "    SMTP_HOST=smtp.gmail.com\n"
            "    SMTP_USERNAME=<your gmail address>\n"
            "    SMTP_PASSWORD=<16-character app password, no spaces>\n"
            "    SMTP_FROM=<your gmail address>\n"
        )
        return 2

    print(f"\n  SMTP host     {settings.SMTP_HOST}:{settings.SMTP_PORT}")
    print(f"  Sending from  {settings.SMTP_FROM}")
    print(f"  Sending to    {args.email}")
    print(f"  Timezone      {args.timezone}")

    await cmd_cleanup(args, quiet=True)

    if args.at:
        try:
            hour, minute = (int(p) for p in args.at.split(":"))
            fire_at = time(hour, minute)
        except Exception:
            print(f"\n  '{args.at}' is not a time. Use HH:MM, for example 21:00.")
            return 2
        schedule_rule = "FREQ=DAILY"
        shape = f"every day at {fire_at:%H:%M} {args.timezone}"
    else:
        # A minutely rule, so the three properties being tested — on time,
        # twice running, survives a restart — can be seen in ten minutes
        # instead of two days. The engine cannot tell the difference: it is the
        # same rule expansion, the same claim lock and the same ladder.
        fire_at = to_local(utcnow() + timedelta(minutes=1), args.timezone).time()
        schedule_rule = f"FREQ=MINUTELY;INTERVAL={args.every}"
        shape = f"every {args.every} minutes, starting in about a minute"

    async with async_session_factory() as session:
        tenant = Tenant(name=TENANT_NAME, slug=TENANT_SLUG, platform="standalone")
        session.add(tenant)
        await session.flush()

        agent = Agent(
            account_id=tenant.id,
            type="nudge",
            name="Milestone 1 reminders",
            slug="milestone-1",
            is_active=True,
        )
        session.add(agent)
        await session.flush()

        person = Person(
            agent_id=agent.id,
            name=args.name,
            email=args.email,
            is_account_holder=True,
            language="en",
            timezone=args.timezone,
            # Quiet hours are disabled for this check unless asked for.
            # Otherwise a perfectly working engine looks broken when the test
            # is run at 23:00: the reminder would be held until 07:00, which is
            # correct behaviour and a confusing first experience.
            quiet_hours_start=time(22, 0) if args.quiet_hours else time(0, 0),
            quiet_hours_end=time(7, 0) if args.quiet_hours else time(0, 0),
            consent_confirmed_at=utcnow(),
            consent_confirmed_by="verify_reminder_engine.py",
        )
        session.add(person)
        await session.flush()

        reminder = Reminder(
            agent_id=agent.id,
            person_id=person.id,
            title=args.title,
            category="medicine",
            notes="This is the phase 1 milestone check.",
            schedule_kind="repeating",
            rrule=schedule_rule,
            time_of_day=fire_at,
            timezone=args.timezone,
            # Email only. Calls and WhatsApp would be skipped with a recorded
            # reason, which is correct but noisy for a first look.
            channel_ladder=["email"],
            max_attempts=3,
            grace_minutes=10,
            notify_email=args.email,
            warn_before_days=[],
        )
        session.add(reminder)
        await session.flush()
        reminder_id = reminder.id
        await session.commit()

    engine = ReminderEngine()
    async with async_session_factory() as session:
        reminder = (
            await session.execute(select(Reminder).where(Reminder.id == reminder_id))
        ).scalar_one()

    written = await engine.generate_for(reminder, horizon_days=2)

    rule("What was created")
    print(f"  Reminder      {args.title}")
    print(f"  Schedule      {shape}")
    print(f"  In words      {occurrence_generator.describe(reminder)}")
    print(f"  Occurrences   {written} written ahead of time")
    print(f"  Grace window  {reminder.grace_minutes} minutes")
    if args.quiet_hours:
        print("  Quiet hours   22:00-07:00 - a reminder inside them waits for 07:00")
    else:
        print("  Quiet hours   off for this check (--quiet-hours turns them on)")

    await _print_upcoming(reminder_id, limit=5)

    rule("Next")
    print("  1. Start the clock, and leave it running:")
    print("         python -m app.scheduler")
    print("  2. Watch what happens:")
    print("         python verify_reminder_engine.py status")
    print("  3. Kill the scheduler with Ctrl+C and start it again.")
    print("     Nothing should be sent twice, and nothing should be lost.")
    print("  4. Clean up:")
    print("         python verify_reminder_engine.py cleanup")
    print()
    return 0


# ----------------------------------------------------------------
# status
# ----------------------------------------------------------------


async def cmd_status(args) -> int:
    print("=" * 72)
    print("Milestone 1 check - status")
    print("=" * 72)

    async with async_session_factory() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == TENANT_SLUG))
        ).scalar_one_or_none()
        if tenant is None:
            print("\n  Nothing set up. Run `setup` first.\n")
            return 1

        agent = (
            await session.execute(select(Agent).where(Agent.account_id == tenant.id))
        ).scalar_one_or_none()
        reminder = (
            await session.execute(select(Reminder).where(Reminder.agent_id == agent.id))
        ).scalar_one_or_none()

    # Is the clock actually running? This is the question the plan calls the
    # single most important one in hardening: a dead scheduler sends nothing
    # and reports nothing.
    rule("The clock")
    try:
        from app.redis_client import init_redis
        from app.scheduler import HEARTBEAT_KEY, LAST_RUN_KEY

        redis = await init_redis()
        beat = await redis.get(HEARTBEAT_KEY)
        last = await redis.get(LAST_RUN_KEY)
        if beat:
            age = (utcnow() - datetime.fromisoformat(beat)).total_seconds()
            print(f"  Running. Last tick {age:.0f}s ago ({beat}).")
            if last:
                print(f"  Last run: {last}")
        else:
            print(
                "  STOPPED - no tick in the last 5 minutes.\n"
                "  Nothing is being sent. Start it with: python -m app.scheduler"
            )
        await redis.close()
    except Exception as exc:
        print(f"  Could not read the heartbeat: {exc}")

    rule("The reminder")
    print(f"  {reminder.title}  -  {occurrence_generator.describe(reminder)}")
    print(f"  Ladder: {' -> '.join(reminder.channel_ladder)}")

    await _print_occurrences(reminder.id)
    await _print_deliveries(reminder.id)

    rule("How to read this")
    print("  sent       delivered, waiting for an answer or escalation")
    print("  done       the person used the link in the email")
    print("  missed     nobody could be reached, or it was too late to be useful")
    print("  scheduled  still ahead")
    print()
    print("  For milestone 1 you are looking for two 'sent' rows at the right")
    print("  times, each with exactly one delivery, including across a restart.")
    print()
    return 0


async def _print_upcoming(reminder_id, limit: int = 5) -> None:
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Occurrence)
                    .where(Occurrence.reminder_id == reminder_id)
                    .order_by(Occurrence.due_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    rule("The next few moments")
    for occurrence in rows:
        print(
            f"  {occurrence.local_due_at:%Y-%m-%d %H:%M} local"
            f"   ({occurrence.due_at:%H:%M} UTC)   {occurrence.status}"
        )


async def _print_occurrences(reminder_id) -> None:
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Occurrence)
                    .where(Occurrence.reminder_id == reminder_id)
                    .order_by(Occurrence.due_at)
                )
            )
            .scalars()
            .all()
        )

    now = utcnow()
    past = [o for o in rows if o.due_at <= now]
    ahead = [o for o in rows if o.due_at > now]

    rule(f"What has happened  ({len(past)} due so far)")
    if not past:
        print("  Nothing due yet.")
    for occurrence in past:
        sent = f"{occurrence.sent_at:%H:%M:%S}" if occurrence.sent_at else "--:--:--"
        # How far from its due time it actually went out. This is the number
        # milestone 1 is really about: "arrives at 21:00" means seconds of
        # lateness, not minutes.
        if occurrence.sent_at:
            drift = (occurrence.sent_at - occurrence.due_at).total_seconds()
            drift_text = f"{drift:+.0f}s"
        else:
            drift_text = "   -"
        print(
            f"  due {occurrence.local_due_at:%H:%M:%S} local"
            f"  sent {sent}  drift {drift_text:>7}"
            f"  {occurrence.status:<10} attempts={occurrence.attempts_made}"
        )
        if occurrence.outcome_reason:
            print(f"      {occurrence.outcome_reason}")

    if ahead:
        print(f"\n  Still ahead ({len(ahead)}):")
        for occurrence in ahead[:3]:
            print(f"      {occurrence.local_due_at:%H:%M:%S} local  {occurrence.status}")


async def _print_deliveries(reminder_id) -> None:
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Delivery, Occurrence)
                    .join(Occurrence, Occurrence.id == Delivery.occurrence_id)
                    .where(Occurrence.reminder_id == reminder_id)
                    .order_by(Delivery.created_at)
                )
            )
            .all()
        )

    rule(f"Every delivery attempt  ({len(rows)})")
    if not rows:
        print("  None yet.")
    for delivery, occurrence in rows:
        local = to_local(delivery.created_at, "Asia/Kolkata")
        print(
            f"  {local:%H:%M:%S}  {delivery.channel:<9} {delivery.status:<8}"
            f" {delivery.recipient or '-'}"
        )
        if delivery.provider_message_id:
            print(f"      message id: {delivery.provider_message_id}")
        if delivery.error:
            print(f"      {delivery.error}")

    # Two deliveries for one occurrence on the same channel would be the
    # double-send bug, which is the thing the claim lock exists to prevent.
    by_occurrence: dict = {}
    for delivery, occurrence in rows:
        if delivery.status == "sent":
            by_occurrence.setdefault(occurrence.id, []).append(delivery.channel)

    duplicates = {
        oid: channels
        for oid, channels in by_occurrence.items()
        if len(channels) != len(set(channels))
    }
    if duplicates:
        print("\n  PROBLEM: the same occurrence was sent twice on one channel:")
        for oid, channels in duplicates.items():
            print(f"      {oid}  {channels}")
    elif by_occurrence:
        print(f"\n  No double sends: {len(by_occurrence)} occurrence(s), one send each.")


# ----------------------------------------------------------------
# cleanup
# ----------------------------------------------------------------


async def cmd_cleanup(args, *, quiet: bool = False) -> int:
    async with async_session_factory() as session:
        result = await session.execute(
            delete(Tenant).where(Tenant.slug == TENANT_SLUG)
        )
        await session.commit()

    if not quiet:
        count = result.rowcount or 0
        if count:
            print("\n  Removed the 'Milestone 1 check' tenant and everything under it.\n")
        else:
            print("\n  Nothing to remove.\n")
    return 0


# ----------------------------------------------------------------
# main
# ----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Milestone 1 check for the reminder engine."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="create a reminder against a real mailbox")
    setup.add_argument("--email", required=True, help="where the reminder is sent")
    setup.add_argument("--name", default="Arsath", help="the recipient's name")
    setup.add_argument("--title", default="BP tablet", help="what the reminder is for")
    setup.add_argument("--timezone", default="Asia/Kolkata")
    setup.add_argument(
        "--every",
        type=int,
        default=3,
        help="fire every N minutes (the fast proof). Default 3.",
    )
    setup.add_argument(
        "--at",
        default=None,
        help="instead, fire daily at HH:MM local (the literal overnight proof)",
    )
    setup.add_argument(
        "--quiet-hours",
        action="store_true",
        help="turn on 22:00-07:00 quiet hours, to test that path",
    )

    sub.add_parser("status", help="show what the engine has done")
    sub.add_parser("cleanup", help="remove everything this script created")

    args = parser.parse_args()

    handlers = {"setup": cmd_setup, "status": cmd_status, "cleanup": cmd_cleanup}

    async def run() -> int:
        try:
            return await handlers[args.command](args)
        finally:
            await close_database()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
