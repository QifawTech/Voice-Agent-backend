"""
Voice Agent Platform — Scheduler Process

The clock. Run it as its own process:

    python -m app.scheduler

Why its own process and not inside the API: the API runs under uvicorn with
--workers=2, and a scheduler started in the FastAPI lifespan would therefore be
two schedulers. The claim lock means that would still not double-send — which
is exactly what makes it dangerous, because the symptom would be invisible.
The nightly top-up would run twice, every tick would do double the work, and
nothing would look wrong. One process, one clock.

Three jobs, matching the three loops in the engine:

    tick        every minute   what is due now
    escalate    every minute   what was sent and not answered
    top up      nightly        the next 60 days of occurrences

The heartbeat is the fourth thing this process does, and the plan calls it the
single most important item in hardening: a dead scheduler fails silently. No
reminders go out, nobody is told, and the first report comes days later from a
person who missed their medication. So every tick writes a timestamp to Redis
with a short expiry, and /health reports it. If the key is missing, the clock
has stopped — whatever the API says about itself.

Alerting on that heartbeat is phase 8. Writing it is here, because retrofitting
a heartbeat into a system that is already trusted is how it gets skipped.
"""

import asyncio
import json
import signal
from datetime import timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.database import close_database, verify_database_connection
from app.logger import get_logger, setup_logging
from app.redis_client import close_redis, get_redis_sync, init_redis
from app.services.reminder_engine import ReminderEngine

logger = get_logger(__name__)

# Redis keys. The heartbeat's TTL is deliberately a few multiples of the tick
# interval: long enough that one slow tick is not an alert, short enough that a
# stopped scheduler is obvious within minutes rather than hours.
HEARTBEAT_KEY = "scheduler:heartbeat"
HEARTBEAT_TTL_SECONDS = 300
LAST_RUN_KEY = "scheduler:last_run"


class SchedulerService:
    """Owns the APScheduler instance and the engine it drives."""

    def __init__(self) -> None:
        self.engine = ReminderEngine()
        self.scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._stopping = asyncio.Event()

    # ---- jobs ----

    async def run_tick(self) -> None:
        try:
            stats = await self.engine.tick()
            await self._heartbeat("tick", str(stats))
        except Exception as exc:
            # A job that raises is logged by APScheduler and then forgotten.
            # Catching it here means the heartbeat still records that the clock
            # is alive but failing, which is a different and more useful signal
            # than silence.
            logger.exception("tick_failed", error=str(exc))
            await self._heartbeat("tick", f"error: {type(exc).__name__}")

    async def run_escalation(self) -> None:
        try:
            stats = await self.engine.escalate()
            if stats.claimed:
                logger.info("escalation_pass", stats=str(stats))
        except Exception as exc:
            logger.exception("escalation_failed", error=str(exc))

    async def run_top_up(self) -> None:
        try:
            written = await self.engine.top_up()
            logger.info("nightly_top_up", occurrences_written=written)
        except Exception as exc:
            logger.exception("top_up_failed", error=str(exc))

    # ---- heartbeat ----

    async def _heartbeat(self, job: str, detail: str) -> None:
        """
        Record that the clock ran. Never allowed to break the clock.

        Redis being down must not stop reminders going out, so this swallows
        its own failures: the loop that matters has already done its work by
        the time this is called.
        """
        from app.services.timekeeping import utcnow

        try:
            redis = get_redis_sync()
            now = utcnow().isoformat()
            await redis.set(HEARTBEAT_KEY, now, ex=HEARTBEAT_TTL_SECONDS)
            await redis.set(
                LAST_RUN_KEY,
                json.dumps({"job": job, "at": now, "detail": detail}),
                ex=HEARTBEAT_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning("heartbeat_write_failed", error=str(exc))

    # ---- lifecycle ----

    def _register_jobs(self) -> None:
        settings = get_settings()

        common = {
            # One instance at a time. A tick that overruns a minute must not
            # start a second tick alongside itself — the claim lock would keep
            # it correct, but the connection pool would not enjoy it.
            "max_instances": 1,
            # If several runs were missed (a pause, a slow host), run once and
            # move on rather than replaying each missed minute.
            "coalesce": True,
            # How late a run may start and still be worth doing. Past this
            # APScheduler drops it, and the next minute's run picks the work up
            # anyway, because the work lives in the database and not in the job.
            "misfire_grace_time": 50,
        }

        self.scheduler.add_job(
            self.run_tick,
            "interval",
            minutes=1,
            id="reminder_tick",
            name="Reminder tick — send what is due",
            **common,
        )
        self.scheduler.add_job(
            self.run_escalation,
            "interval",
            minutes=1,
            id="reminder_escalation",
            name="Escalation — climb the ladder on no answer",
            **common,
        )
        self.scheduler.add_job(
            self.run_top_up,
            "cron",
            hour=settings.REMINDER_TOPUP_HOUR_UTC,
            minute=0,
            id="reminder_top_up",
            name="Nightly top-up — generate the next 60 days",
            max_instances=1,
            coalesce=True,
            # A missed nightly run is worth doing hours late: the occurrences
            # it writes are for the next 60 days, so there is nothing stale
            # about them.
            misfire_grace_time=3600,
        )

    async def start(self) -> None:
        settings = get_settings()
        setup_logging(debug=settings.DEBUG)

        logger.info(
            "scheduler_starting",
            environment=settings.ENVIRONMENT,
            horizon_days=settings.REMINDER_HORIZON_DAYS,
            tick_batch=settings.REMINDER_TICK_BATCH,
            smtp_configured=settings.smtp_configured,
        )

        if not settings.smtp_configured:
            # Worth saying loudly at startup rather than leaving it to be
            # discovered in the delivery log: with no channel configured, every
            # reminder this process handles will be recorded as missed.
            logger.warning(
                "no_channel_configured",
                detail=(
                    "SMTP is not configured, and calls and WhatsApp are not "
                    "built yet. Reminders will be marked missed, not sent."
                ),
            )

        if not await verify_database_connection():
            # Without a database there is no queue to read and nothing this
            # process can usefully do. Exiting is better than looping on
            # errors, because a container that exits is a container something
            # notices.
            raise SystemExit("Scheduler cannot start: database is unreachable.")

        await init_redis()

        self._register_jobs()
        self.scheduler.start()

        # Top up once at startup. A scheduler that has been down for a day
        # comes back to a thin queue, and waiting until the next nightly run
        # to refill it would leave a gap nobody asked for.
        await self.run_top_up()

        logger.info(
            "scheduler_started",
            jobs=[job.id for job in self.scheduler.get_jobs()],
        )

        await self._stopping.wait()

    async def stop(self) -> None:
        logger.info("scheduler_stopping")
        self.scheduler.shutdown(wait=True)
        await close_database()
        await close_redis()
        self._stopping.set()
        logger.info("scheduler_stopped")


async def main() -> None:
    service = SchedulerService()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        # Shut the clock down cleanly so an in-flight tick finishes: an
        # occurrence killed between its claim and its send is recoverable, but
        # only after the stale-claim timeout, which is a delay nobody needs.
        loop.create_task(service.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            # Windows does not support add_signal_handler for these. KeyboardInterrupt
            # below covers Ctrl+C, which is how this runs during development there.
            pass

    try:
        await service.start()
    except KeyboardInterrupt:
        await service.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("scheduler_interrupted")
