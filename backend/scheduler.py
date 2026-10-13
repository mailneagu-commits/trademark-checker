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


async def _auto_enrich(source: str, date_str: str):
    """Aduce din TMview detaliile complete ale mărcilor din buletin (ca în aplicația de
    verificare) înainte de comparație, ca tabelul să le afișeze de la prima deschidere."""
    from routes.monitor import run_bulletin_enrich
    try:
        res = await run_bulletin_enrich(source, date_str)
        print(f"[SCHEDULER] Detalii buletin {source} {date_str}: {res}")
    except Exception as e:
        print(f"[SCHEDULER] Eroare detalii buletin ({source} {date_str}): {e}")


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


async def _probe_tmview() -> Optional[str]:
    """O singură căutare de test pe TMview. None = merge, altfel motivul blocajului.
    Imperva blochează în mai multe feluri: 403 „Request Rejected”, 200 cu pagină HTML
    în loc de JSON, sau conexiuni resetate / care atârnă."""
    from agents.search_agent import AsyncSession, TMVIEW_URL, TMVIEW_HOME, _build_headers, HAS_CURL_CFFI
    if not HAS_CURL_CFFI:
        return None
    payload = {"page": "1", "pageSize": "3", "criteria": "F", "basicSearch": "TEST",
               "newPage": True, "fields": ["ST13", "tmName"], "territories": ["RO"]}
    try:
        async with AsyncSession(impersonate="chrome120", proxies=None, verify=True) as session:
            await session.get(TMVIEW_HOME, timeout=15)
            r = await session.post(TMVIEW_URL, json=payload, headers=_build_headers(), timeout=20)
    except Exception as e:
        return f"eroare de conexiune: {type(e).__name__}: {str(e)[:120]}"
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    if "json" not in r.headers.get("content-type", "").lower():
        return "200 cu HTML în loc de JSON (Imperva)"
    return None


_last_health: dict = {}  # ultima verificare periodică — afișată în /api/region-status


async def _check_tmview_health():
    """Rulat la 10 min: dacă TMview blochează IP-ul containerului, rotim regiunea Railway.
    Confirmăm cu o a doua probă, ca o flotare izolată să nu declanșeze un redeploy."""
    from datetime import datetime as _dt
    reason = await _probe_tmview()
    _last_health.update({"at": _dt.utcnow().isoformat(), "blocked": reason})
    if reason is None:
        return
    await asyncio.sleep(20)
    reason2 = await _probe_tmview()
    if reason2 is None:
        print(f"[TMVIEW-HEALTH] Prima probă a eșuat ({reason}), a doua a mers — nu rotesc.")
        return
    print(f"[TMVIEW-HEALTH] TMview blocat ({reason2}) — cer rotirea regiunii")
    from region_rotate import rotate_region
    res = await asyncio.get_event_loop().run_in_executor(
        None, rotate_region, f"verificare periodică: TMview blocat ({reason2})")
    print(f"[TMVIEW-HEALTH] Rezultat rotire: {res}")


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

    # Comparația OSIM pornește ACUM, imediat — nu mai jos, după EUIPO. EUIPO poate
    # dura 1-4+ minute (uneori mai mult, pe conexiune instabilă); OSIM nu are de ce
    # să aștepte după el doar pentru că sunt în aceeași funcție.
    for slug in new_osim:
        await _auto_enrich("osim", slug.removeprefix("osim-"))
        await _auto_compare("osim", slug.removeprefix("osim-"))

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
            # max_days=1: spre deosebire de OSIM, fetch_euipo_for_date rezolvă deja
            # "cel mai recent buletin publicat la sau înainte de data cerută" — o a
            # doua verificare pentru "ieri" ar re-descărca practic ACELAȘI buletin,
            # doar sub altă cheie de cache (bazată pe data cerută, nu pe data reală a
            # buletinului), irosind încă un ciclu întreg de reîncercări PDF (minute).
            await loop.run_in_executor(None, fetch_latest_euipo, 1)
        except Exception as e:
            print(f"[SCHEDULER] EUIPO prefetch error: {e}")
        finally:
            _in_progress.discard(guard_slug)
        new_euipo = sorted(_ok_euipo_slugs(_euipo_processed()) - before_euipo)

    for slug in new_euipo:
        await _auto_enrich("euipo", slug.removeprefix("euipo-"))
        await _auto_compare("euipo", slug.removeprefix("euipo-"))
    if new_osim or new_euipo:
        await _auto_run_all_watches()

    # Reia aducerea detaliilor pentru buletinele recente rămase incomplete (TMview limitează
    # uneori cererile). Pentru cele complete e un no-op instant — nu se face nicio cerere.
    for src, processed_fn, ok_fn in (("osim", _osim_processed, _ok_osim_slugs),
                                     ("euipo", _euipo_processed, _ok_euipo_slugs)):
        for slug in sorted(ok_fn(processed_fn()), reverse=True)[:2]:
            await _auto_enrich(src, slug.removeprefix(f"{src}-"))


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
        _run_all_due,
        CronTrigger(hour=7, minute=0),
        args=["daily"],
        id="daily_monitor",
        replace_existing=True,
    )
    # Weekly on Monday at 07:30 UTC
    sched.add_job(
        _run_all_due,
        CronTrigger(day_of_week="mon", hour=7, minute=30),
        args=["weekly"],
        id="weekly_monitor",
        replace_existing=True,
    )
    # Monthly on the 1st at 08:00 UTC
    sched.add_job(
        _run_all_due,
        CronTrigger(day=1, hour=8, minute=0),
        args=["monthly"],
        id="monthly_monitor",
        replace_existing=True,
    )
    # Pre-descărcare buletine: la fiecare oră, 06:00-19:00 UTC, zile lucrătoare —
    # ora exactă de publicare a buletinelor OSIM/EUIPO variază de la o zi la alta.
    sched.add_job(
        _prefetch_bulletins,
        CronTrigger(day_of_week="mon-fri", hour="6-19", minute=0),
        id="bulletin_prefetch",
        replace_existing=True,
    )
    # Verificare TMview la fiecare 10 minute — rotește regiunea automat dacă e blocat,
    # chiar dacă nimeni nu a căutat nimic între timp (circuit breaker-ul se deschide
    # doar la o căutare reală eșuată).
    sched.add_job(
        _check_tmview_health,
        CronTrigger(minute="*/10"),
        id="tmview_health",
        replace_existing=True,
    )

    # Funcțiile corutină se dau direct: AsyncIOScheduler le rulează pe event loop. Înainte erau
    # lambda: asyncio.ensure_future(...) — APScheduler rulează funcțiile normale într-un thread
    # fără event loop, deci fiecare job murea cu RuntimeError, silențios (niciun job nu rula).
    sched.start()
    print("[SCHEDULER] Started — daily 07:00, weekly Mon 07:30, monthly 1st 08:00 UTC, "
          "bulletin prefetch hourly 06-19 UTC (mon-fri), TMview health check every 10 min")

    # Rulează prefetch-ul și imediat la pornire, nu doar la următorul tick orar —
    # altfel, chiar după un deploy (care șterge cache-ul de fișiere), primul
    # utilizator tot ar aștepta descărcarea/parsarea completă.
    asyncio.ensure_future(_prefetch_bulletins())


def stop_scheduler():
    sched = _get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
