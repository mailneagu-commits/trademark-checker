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


async def _auto_compare(source: str, date_str: str):
    """Pornește automat comparația buletin↔monitorizare (ca tabelul să fie deja
    gata în UI, fără click pe „Compară"). Apelat doar când buletinul pentru
    `date_str` tocmai a fost rezolvat cu succes pentru prima dată."""
    from routes.monitor import start_bulletin_compare
    try:
        await start_bulletin_compare(source, date_str)
        print(f"[SCHEDULER] Comparație auto pornită: {source} {date_str}")
    except Exception as e:
        print(f"[SCHEDULER] Eroare comparație auto ({source} {date_str}): {e}")


async def _auto_run_all_watches():
    """Pornește automat „Monitorizează acum toate mărcile" (căutare completă per
    marcă + alertă email, dacă SMTP e configurat). O singură rulare per tick, chiar
    dacă atât OSIM cât și EUIPO au fost proaspăt rezolvate în același tick — nu are
    rost s-o repete de două ori la rând."""
    from routes.monitor import run_all_watches
    try:
        result = await run_all_watches()
        print(f"[SCHEDULER] Monitorizare auto (run-all): {result}")
    except Exception as e:
        print(f"[SCHEDULER] Eroare monitorizare auto: {e}")


async def _prefetch_bulletins():
    """Pre-descarcă și pre-parsează buletinele OSIM/EUIPO de îndată ce apar (nu o
    singură dată/zi — rulează periodic, pentru că ora exactă de publicare variază),
    apoi — DOAR dacă buletinul tocmai a fost rezolvat cu succes pentru prima dată —
    pornește automat și comparația cu lista de monitorizare + „Monitorizează acum
    toate mărcile". La un tick ulterior din aceeași zi (deja rezolvat), nu repetă
    nimic — fetch_*_for_date e aproape instant din cache, iar monitorizarea completă
    (căutări TMview/EUIPO per marcă) nu merită repetată la fiecare oră fără rost."""
    from scrapers.osim_bulletin import fetch_latest_osim, _load_processed as _osim_processed
    from scrapers.euipo_bulletin import (
        fetch_latest_euipo, is_fetch_in_progress, _in_progress,
        _prev_working_day, _date_slug, _load_processed as _euipo_processed,
    )
    from datetime import date

    loop  = asyncio.get_event_loop()
    today = date.today()

    def _ok_osim_slugs(processed):
        return {s for s, e in processed.items() if e.get("status") == "ok"}

    def _ok_euipo_slugs(processed):
        return {s for s, e in processed.items() if e.get("status") in ("ok_bulletin", "ok_api")}

    # ── OSIM ── fetch_latest_osim reîncearcă mai multe zile lucrătoare în urmă —
    # dacă buletinul de azi nu a apărut încă, prinde automat cel de ieri (ultimul
    # deja disponibil), fără să fie nevoie de vreo logică specială aici.
    before_osim = _ok_osim_slugs(_osim_processed())
    try:
        await loop.run_in_executor(None, fetch_latest_osim)
    except Exception as e:
        print(f"[SCHEDULER] OSIM prefetch error: {e}")
    new_osim = sorted(_ok_osim_slugs(_osim_processed()) - before_osim)

    # ── EUIPO ── (evită coliziunea cu un fetch pornit manual din UI — ambele
    # ar scrie același fișier PDF — sărind peste tick-ul ăsta dacă e deja în curs)
    before_euipo = _ok_euipo_slugs(_euipo_processed())
    new_euipo: list = []
    if is_fetch_in_progress(today):
        print("[SCHEDULER] EUIPO fetch deja în curs — sar peste acest tick de prefetch")
    else:
        guard_slug = _date_slug(_prev_working_day(today))
        _in_progress.add(guard_slug)
        try:
            await loop.run_in_executor(None, fetch_latest_euipo)
        except Exception as e:
            print(f"[SCHEDULER] EUIPO prefetch error: {e}")
        finally:
            _in_progress.discard(guard_slug)
        new_euipo = sorted(_ok_euipo_slugs(_euipo_processed()) - before_euipo)

    for slug in new_osim:
        await _auto_compare("osim", slug.removeprefix("osim-"))
    for slug in new_euipo:
        await _auto_compare("euipo", slug.removeprefix("euipo-"))
    if new_osim or new_euipo:
        await _auto_run_all_watches()


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
