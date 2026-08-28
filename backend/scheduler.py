"""
APScheduler setup — runs monitoring jobs based on each WatchItem's frequency.
"""
from __future__ import annotations

import asyncio
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler: Optional[AsyncIOScheduler] = None


def _get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


async def _prefetch_bulletins():
    """Pre-descarcă și pre-parsează buletinele OSIM/EUIPO de îndată ce apar, ca
    utilizatorul să nu mai aștepte 1-4 minute la primul click pe „Descarcă" sau
    „Compară cu monitorizarea" — până atunci deja sunt în cache. Rulează periodic
    (nu o singură dată/zi) pentru că ora exactă de publicare variază; fetch_latest_*
    e ieftin (aproape instant) odată ce ziua curentă e deja rezolvată cu succes, și
    reîncearcă din nou zilele nepublicate încă la verificarea precedentă."""
    from scrapers.osim_bulletin import fetch_latest_osim
    from scrapers.euipo_bulletin import (
        fetch_latest_euipo, is_fetch_in_progress, _in_progress,
        _prev_working_day, _date_slug,
    )
    from datetime import date

    loop = asyncio.get_event_loop()

    try:
        await loop.run_in_executor(None, fetch_latest_osim)
    except Exception as e:
        print(f"[SCHEDULER] OSIM prefetch error: {e}")

    # Evită să pornească o descărcare EUIPO în paralel cu una declanșată manual de
    # utilizator din UI (ambele ar scrie același fișier PDF) — dacă una e deja în
    # curs (pornită din /bulletin-fetch), sărim peste acest tick.
    today = date.today()
    if is_fetch_in_progress(today):
        print("[SCHEDULER] EUIPO fetch deja în curs — sar peste acest tick de prefetch")
        return
    slug = _date_slug(_prev_working_day(today))
    _in_progress.add(slug)
    try:
        await loop.run_in_executor(None, fetch_latest_euipo)
    except Exception as e:
        print(f"[SCHEDULER] EUIPO prefetch error: {e}")
    finally:
        _in_progress.discard(slug)


async def _run_all_due(frequency: str):
    from db import SessionLocal
    from monitor_models import WatchItem
    from monitor_service import run_watch_item

    db = SessionLocal()
    try:
        items = db.query(WatchItem).filter(
            WatchItem.active == True,
            WatchItem.frequency == frequency,
        ).all()
        print(f"[SCHEDULER] {frequency} run — {len(items)} watch item(s)")
        for item in items:
            try:
                await run_watch_item(item, db)
            except Exception as e:
                print(f"[SCHEDULER] Error for item {item.id}: {e}")
    finally:
        db.close()


def start_scheduler():
    sched = _get_scheduler()
    if sched.running:
        return

    # Daily at 07:00 UTC
    sched.add_job(
        lambda: asyncio.ensure_future(_run_all_due("daily")),
        CronTrigger(hour=7, minute=0),
        id="daily_monitor",
        replace_existing=True,
    )
    # Weekly on Monday at 07:30 UTC
    sched.add_job(
        lambda: asyncio.ensure_future(_run_all_due("weekly")),
        CronTrigger(day_of_week="mon", hour=7, minute=30),
        id="weekly_monitor",
        replace_existing=True,
    )
    # Monthly on the 1st at 08:00 UTC
    sched.add_job(
        lambda: asyncio.ensure_future(_run_all_due("monthly")),
        CronTrigger(day=1, hour=8, minute=0),
        id="monthly_monitor",
        replace_existing=True,
    )
    # Pre-descărcare buletine: la fiecare oră, 06:00-19:00 UTC, zile lucrătoare —
    # ora exactă de publicare a buletinelor OSIM/EUIPO variază de la o zi la alta.
    sched.add_job(
        lambda: asyncio.ensure_future(_prefetch_bulletins()),
        CronTrigger(day_of_week="mon-fri", hour="6-19", minute=0),
        id="bulletin_prefetch",
        replace_existing=True,
    )

    sched.start()
    print("[SCHEDULER] Started — daily 07:00, weekly Mon 07:30, monthly 1st 08:00 UTC, "
          "bulletin prefetch hourly 06-19 UTC (mon-fri)")

    # Rulează prefetch-ul și imediat la pornire, nu doar la următorul tick orar —
    # altfel, chiar după un deploy (care șterge cache-ul de fișiere), primul
    # utilizator tot ar aștepta descărcarea/parsarea completă.
    asyncio.ensure_future(_prefetch_bulletins())


def stop_scheduler():
    sched = _get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
