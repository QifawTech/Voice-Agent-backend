"""
Voice Agent Platform — Reminder Engine Routes

The smallest surface that makes the engine usable and provable:

    POST   /api/v1/agents                                   create an agent
    GET    /api/v1/agents                                    list mine
    POST   /api/v1/agents/{id}/people                        add a recipient
    GET    /api/v1/agents/{id}/people
    POST   /api/v1/agents/{id}/reminders                     create a reminder
    GET    /api/v1/agents/{id}/reminders
    GET    /api/v1/agents/{id}/agenda                        Overdue / Today / ...
    POST   /api/v1/agents/{id}/reminders/{rid}/send-now      the test button
    POST   /api/v1/occurrences/{id}/done                     from the dashboard
    POST   /api/v1/occurrences/{id}/snooze
    GET    /api/v1/r/{id}/done?t=...                         from an email, no login
    GET    /api/v1/r/{id}/snooze?t=...

The last two are the only public routes in this file, and they are public by
necessity: the person answering a reminder may be an 82-year-old with a basic
phone who will never have an account here. Their authority comes from an HMAC
in the link rather than from a session — see services/answer_tokens.py.

The test button exists because of a line in the plan worth taking literally:
nobody trusts a reminder system until they have heard it fire once.
"""

import re
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.logger import get_logger
from app.middleware.auth import get_current_user
from app.models.admin_user import AdminUser
from app.models.agent import Agent
from app.models.delivery import Delivery
from app.models.occurrence import Occurrence
from app.models.person import Person
from app.models.reminder import Reminder
from app.schemas.reminder import (
    AgendaItem,
    AgendaResponse,
    AgentCreate,
    AgentResponse,
    AnswerResponse,
    OccurrenceResponse,
    PersonCreate,
    PersonResponse,
    ReminderCreate,
    ReminderCreated,
    ReminderResponse,
    SnoozeRequest,
)
from app.services import occurrence_generator
from app.services.answer_tokens import verify_token
from app.services.reminder_engine import ReminderEngine
from app.services.timekeeping import to_local, utcnow

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Reminders"])


def _get_engine() -> ReminderEngine:
    return ReminderEngine()


# ----------------------------------------------------------------
# Scoping
# ----------------------------------------------------------------


async def _require_agent(
    agent_id: UUID, current_user: AdminUser, db: AsyncSession
) -> Agent:
    """
    Load an agent, enforcing that the caller may see it.

    A super admin (account_id is NULL) may reach any account's agents; everyone
    else is confined to their own. Returning 404 rather than 403 for someone
    else's agent is deliberate: a 403 confirms the id exists.
    """
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()

    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if current_user.role != "super_admin" and agent.account_id != current_user.account_id:
        raise HTTPException(status_code=404, detail="Agent not found")

    return agent


async def _require_occurrence(
    occurrence_id: UUID, current_user: AdminUser, db: AsyncSession
) -> Occurrence:
    result = await db.execute(
        select(Occurrence)
        .options(joinedload(Occurrence.reminder).joinedload(Reminder.agent))
        .where(Occurrence.id == occurrence_id)
    )
    occurrence = result.unique().scalar_one_or_none()
    if occurrence is None:
        raise HTTPException(status_code=404, detail="Reminder occurrence not found")

    agent = occurrence.reminder.agent
    if current_user.role != "super_admin" and agent.account_id != current_user.account_id:
        raise HTTPException(status_code=404, detail="Reminder occurrence not found")

    return occurrence


def _slugify(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return cleaned[:100] or "agent"


# ----------------------------------------------------------------
# Agents
# ----------------------------------------------------------------


@router.post(
    "/agents",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Agent",
    description=(
        "One job the voice engine does for this account: answer the phone for a "
        "restaurant, ring you about your bills, or chase your customers."
    ),
)
async def create_agent(
    payload: AgentCreate,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.account_id is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This login is not attached to an account, so it has nowhere to "
                "put an agent. Log in as an account's admin."
            ),
        )

    agent = Agent(
        account_id=current_user.account_id,
        type=payload.type,
        name=payload.name,
        slug=payload.slug or _slugify(payload.name),
        voice_config=payload.voice_config or {},
    )
    db.add(agent)

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"This account already has an agent with the slug "
                f"'{agent.slug}'. Give this one a different name or slug."
            ),
        )

    await db.refresh(agent)
    logger.info(
        "agent_created",
        agent_id=str(agent.id),
        type=agent.type,
        account_id=str(agent.account_id),
    )
    return agent


@router.get(
    "/agents",
    response_model=list[AgentResponse],
    summary="List Agents",
)
async def list_agents(
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(Agent).order_by(Agent.created_at)
    if current_user.role != "super_admin":
        query = query.where(Agent.account_id == current_user.account_id)
    result = await db.execute(query)
    return list(result.scalars().all())


# ----------------------------------------------------------------
# People
# ----------------------------------------------------------------


@router.post(
    "/agents/{agent_id}/people",
    response_model=PersonResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add A Person",
    description=(
        "Someone who receives reminders — the account holder, or a parent who "
        "will never touch an app. Consent is recorded with the identity of "
        "whoever confirmed it."
    ),
)
async def create_person(
    agent_id: UUID,
    payload: PersonCreate,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_agent(agent_id, current_user, db)

    person = Person(
        agent_id=agent_id,
        name=payload.name,
        is_account_holder=payload.is_account_holder,
        phone=payload.phone,
        whatsapp=payload.whatsapp,
        email=payload.email,
        language=payload.language,
        timezone=payload.timezone,
        quiet_hours_start=payload.quiet_hours_start,
        quiet_hours_end=payload.quiet_hours_end,
        # Stamped server-side from the authenticated caller. A consent record
        # the client could fill in itself would not be worth storing.
        consent_confirmed_at=utcnow(),
        consent_confirmed_by=f"{current_user.email} ({current_user.id})",
    )
    db.add(person)
    await db.flush()
    await db.refresh(person)

    logger.info(
        "person_created",
        person_id=str(person.id),
        agent_id=str(agent_id),
        confirmed_by=str(current_user.id),
    )
    return person


@router.get(
    "/agents/{agent_id}/people",
    response_model=list[PersonResponse],
    summary="List People",
)
async def list_people(
    agent_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_agent(agent_id, current_user, db)
    result = await db.execute(
        select(Person).where(Person.agent_id == agent_id).order_by(Person.name)
    )
    return list(result.scalars().all())


# ----------------------------------------------------------------
# Reminders
# ----------------------------------------------------------------


@router.post(
    "/agents/{agent_id}/reminders",
    response_model=ReminderCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create Reminder",
    description=(
        "Creates the rule and immediately generates its occurrences, so a "
        "reminder set up at 20:55 for 21:00 fires tonight instead of waiting "
        "for the nightly job. The response reports how many moments the "
        "schedule produced and when the first one is — a schedule that "
        "produced nothing is a setup mistake worth seeing at once."
    ),
)
async def create_reminder(
    agent_id: UUID,
    payload: ReminderCreate,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    engine: ReminderEngine = Depends(_get_engine),
):
    await _require_agent(agent_id, current_user, db)

    result = await db.execute(
        select(Person).where(
            Person.id == payload.person_id, Person.agent_id == agent_id
        )
    )
    person = result.scalar_one_or_none()
    if person is None:
        raise HTTPException(
            status_code=404, detail="That person is not on this agent."
        )
    if not person.is_contactable:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{person.name} opted out on "
                f"{person.opted_out_at:%d %b %Y} and cannot be sent reminders."
            ),
        )

    reminder = Reminder(
        agent_id=agent_id,
        person_id=person.id,
        title=payload.title,
        category=payload.category,
        notes=payload.notes,
        schedule_kind=payload.schedule_kind,
        rrule=payload.rrule,
        due_date=payload.due_date,
        warn_before_days=payload.warn_before_days or [],
        time_of_day=payload.time_of_day,
        # The person's timezone unless told otherwise: "21:00" means 21:00
        # where the person being reminded is, not where the account holder is.
        timezone=payload.timezone or person.timezone,
        channel_ladder=payload.channel_ladder,
        max_attempts=payload.max_attempts,
        grace_minutes=payload.grace_minutes,
        notify_email=payload.notify_email or current_user.email,
    )
    db.add(reminder)
    await db.flush()
    await db.refresh(reminder)

    # Commit before generating: generate_for opens its own session, and an
    # uncommitted reminder would not be visible to it.
    await db.commit()

    created = await engine.generate_for(reminder)

    next_row = await db.execute(
        select(Occurrence)
        .where(
            Occurrence.reminder_id == reminder.id,
            Occurrence.status == "scheduled",
        )
        .order_by(Occurrence.due_at)
        .limit(1)
    )
    nxt = next_row.scalar_one_or_none()

    logger.info(
        "reminder_created",
        reminder_id=str(reminder.id),
        occurrences=created,
        schedule=occurrence_generator.describe(reminder),
    )

    return ReminderCreated(
        reminder=ReminderResponse.model_validate(reminder),
        schedule=occurrence_generator.describe(reminder),
        occurrences_created=created,
        next_due_at=nxt.due_at if nxt else None,
        next_due_local=nxt.local_due_at if nxt else None,
    )


@router.get(
    "/agents/{agent_id}/reminders",
    response_model=list[ReminderResponse],
    summary="List Reminders",
)
async def list_reminders(
    agent_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_agent(agent_id, current_user, db)
    result = await db.execute(
        select(Reminder)
        .where(Reminder.agent_id == agent_id)
        .order_by(Reminder.created_at.desc())
    )
    return list(result.scalars().all())


@router.post(
    "/agents/{agent_id}/reminders/{reminder_id}/send-now",
    response_model=OccurrenceResponse,
    summary="Send This Reminder Now",
    description=(
        "The test button. Writes a one-off occurrence due immediately and runs "
        "one tick, so the message arrives in seconds rather than at the top of "
        "the next minute.\n\n"
        "This sends a real message to the real person. Quiet hours still apply."
    ),
)
async def send_now(
    agent_id: UUID,
    reminder_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    engine: ReminderEngine = Depends(_get_engine),
):
    await _require_agent(agent_id, current_user, db)

    result = await db.execute(
        select(Reminder).where(
            Reminder.id == reminder_id, Reminder.agent_id == agent_id
        )
    )
    reminder = result.scalar_one_or_none()
    if reminder is None:
        raise HTTPException(status_code=404, detail="Reminder not found")

    now = utcnow()
    occurrence = Occurrence(
        reminder_id=reminder.id,
        due_at=now,
        local_due_at=to_local(now, reminder.timezone),
        status="scheduled",
        # A test send is its own moment, so it carries no expiry offset and no
        # history. It is deliberately a real occurrence rather than a special
        # path: a test that does not use the engine proves nothing about it.
        offset_days=None,
    )
    db.add(occurrence)
    await db.flush()
    occurrence_id = occurrence.id
    await db.commit()

    await engine.tick(limit=50)

    refreshed = await db.execute(
        select(Occurrence).where(Occurrence.id == occurrence_id)
    )
    return refreshed.scalar_one()


# ----------------------------------------------------------------
# Agenda
# ----------------------------------------------------------------


@router.get(
    "/agents/{agent_id}/agenda",
    response_model=AgendaResponse,
    summary="Reminders Agenda",
    description=(
        "Occurrences grouped Overdue / Today / This week / Later. A plain "
        "query, because occurrences are generated ahead of time rather than "
        "computed on demand."
    ),
)
async def agenda(
    agent_id: UUID,
    days: int = Query(default=30, ge=1, le=365),
    include_closed: bool = Query(
        default=False,
        description="Include done, missed and cancelled occurrences.",
    ),
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_agent(agent_id, current_user, db)

    now = utcnow()
    horizon = now + timedelta(days=days)

    query = (
        select(Occurrence)
        .join(Reminder, Reminder.id == Occurrence.reminder_id)
        .options(joinedload(Occurrence.reminder).joinedload(Reminder.person))
        .where(Reminder.agent_id == agent_id, Occurrence.due_at <= horizon)
        .order_by(Occurrence.due_at)
    )
    if not include_closed:
        query = query.where(
            Occurrence.status.in_(["scheduled", "claimed", "sent"])
        )

    rows = (await db.execute(query)).unique().scalars().all()

    buckets: dict[str, list[AgendaItem]] = {
        "overdue": [],
        "today": [],
        "this_week": [],
        "later": [],
    }

    for occurrence in rows:
        reminder = occurrence.reminder
        item = AgendaItem(
            occurrence=OccurrenceResponse.model_validate(occurrence),
            reminder_title=reminder.title,
            person_name=reminder.person.name,
            channel_ladder=list(reminder.channel_ladder or []),
        )

        # "Today" is decided in the reminder's own timezone, not the server's.
        # A reminder due 23:30 in Chennai is today for the person it is for,
        # whatever date it is in UTC.
        local_now = to_local(now, reminder.timezone)
        local_due = occurrence.local_due_at

        if occurrence.due_at < now and occurrence.status in ("scheduled", "claimed"):
            buckets["overdue"].append(item)
        elif local_due.date() == local_now.date():
            buckets["today"].append(item)
        elif local_due.date() <= (local_now + timedelta(days=7)).date():
            buckets["this_week"].append(item)
        else:
            buckets["later"].append(item)

    return AgendaResponse(**buckets, total=len(rows))


# ----------------------------------------------------------------
# Answers — from the dashboard
# ----------------------------------------------------------------


@router.post(
    "/occurrences/{occurrence_id}/done",
    response_model=AnswerResponse,
    summary="Mark Done",
)
async def mark_done(
    occurrence_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    engine: ReminderEngine = Depends(_get_engine),
):
    await _require_occurrence(occurrence_id, current_user, db)
    changed = await engine.mark_done(occurrence_id, channel="dashboard")
    return AnswerResponse(
        occurrence_id=occurrence_id,
        status="done",
        changed=changed,
        message="Closed." if changed else "It was already closed.",
    )


@router.post(
    "/occurrences/{occurrence_id}/snooze",
    response_model=AnswerResponse,
    summary="Snooze",
    description=(
        "Pushes this one occurrence back and writes a new one at the new time. "
        "The repeat rule is untouched, so snoozing tonight does not move next "
        "month."
    ),
)
async def snooze(
    occurrence_id: UUID,
    payload: SnoozeRequest,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    engine: ReminderEngine = Depends(_get_engine),
):
    await _require_occurrence(occurrence_id, current_user, db)

    until = utcnow() + timedelta(minutes=payload.minutes)
    result = await engine.snooze(occurrence_id, until=until, channel="dashboard")

    if result is None:
        raise HTTPException(
            status_code=409,
            detail="That reminder is already closed, so it cannot be snoozed.",
        )

    return AnswerResponse(
        occurrence_id=occurrence_id,
        status="snoozed",
        changed=True,
        message=f"Put off until {result:%d %b %H:%M} UTC.",
    )


@router.get(
    "/occurrences/{occurrence_id}/deliveries",
    summary="Delivery Log",
    description=(
        "Every attempt made for this occurrence, including the channels that "
        "were skipped and why. This is what makes a 'missed' honest."
    ),
)
async def occurrence_deliveries(
    occurrence_id: UUID,
    current_user: AdminUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_occurrence(occurrence_id, current_user, db)
    rows = (
        (
            await db.execute(
                select(Delivery)
                .where(Delivery.occurrence_id == occurrence_id)
                .order_by(Delivery.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "channel": d.channel,
            "provider": d.provider,
            "status": d.status,
            "recipient": d.recipient,
            "provider_message_id": d.provider_message_id,
            "error": d.error,
            "cost": float(d.cost) if d.cost is not None else None,
            "attempt_no": int(d.attempt_no) if d.attempt_no is not None else None,
            "created_at": d.created_at,
        }
        for d in rows
    ]


# ----------------------------------------------------------------
# Answers — from a link in a message, with no login
# ----------------------------------------------------------------


def _page(title: str, line: str, ok: bool = True) -> HTMLResponse:
    """
    The page a person sees after tapping a link in a reminder.

    Deliberately one sentence and no navigation. The reader may be elderly, on
    a slow connection, on a basic phone browser, and has no account here. The
    only thing they need is confirmation that their tap registered.
    """
    accent = "#0f766e" if ok else "#b91c1c"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
         background:#fafaf9; color:#1c1917; padding:24px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#18181b; color:#f4f4f5; }}
  }}
  .card {{ max-width:26rem; text-align:center; }}
  .mark {{ font-size:2.5rem; color:{accent}; line-height:1; }}
  h1 {{ font-size:1.25rem; margin:.75rem 0 .25rem; }}
  p {{ margin:0; opacity:.75; }}
</style>
</head><body>
  <div class="card">
    <div class="mark">{"&#10003;" if ok else "&#33;"}</div>
    <h1>{title}</h1>
    <p>{line}</p>
  </div>
</body></html>""",
        status_code=200 if ok else 400,
    )


@router.get(
    "/r/{occurrence_id}/done",
    include_in_schema=False,
    response_class=HTMLResponse,
)
async def answer_done(
    occurrence_id: UUID,
    t: str = Query(default=""),
    engine: ReminderEngine = Depends(_get_engine),
):
    """Close a reminder from a link. No login — the token is the authority."""
    if not verify_token(occurrence_id, "done", t):
        logger.warning("answer_token_rejected", occurrence_id=str(occurrence_id))
        return _page(
            "That link did not work",
            "It may have been altered in transit. Please use the link in the "
            "original message.",
            ok=False,
        )

    changed = await engine.mark_done(occurrence_id, channel="email")
    return _page(
        "Thank you",
        "This reminder is closed."
        if changed
        else "This reminder was already closed.",
    )


@router.get(
    "/r/{occurrence_id}/snooze",
    include_in_schema=False,
    response_class=HTMLResponse,
)
async def answer_snooze(
    occurrence_id: UUID,
    t: str = Query(default=""),
    minutes: int = Query(default=60, ge=1, le=1440),
    engine: ReminderEngine = Depends(_get_engine),
):
    if not verify_token(occurrence_id, "snooze", t):
        logger.warning("answer_token_rejected", occurrence_id=str(occurrence_id))
        return _page(
            "That link did not work",
            "It may have been altered in transit. Please use the link in the "
            "original message.",
            ok=False,
        )

    until = utcnow() + timedelta(minutes=minutes)
    result = await engine.snooze(occurrence_id, until=until, channel="email")

    if result is None:
        return _page("Already closed", "There is nothing left to put off here.")

    return _page("Put off for now", f"We will remind you again in {minutes} minutes.")
