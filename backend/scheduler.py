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

    sched.start()
    print("[SCHEDULER] Started — daily 07:00, weekly Mon 07:30, monthly 1st 08:00 UTC")


def stop_scheduler():
    sched = _get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
