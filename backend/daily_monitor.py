"""
Monitorizarea zilnică pe buletine: pentru o zi aleasă, descarcă buletinul OSIM (BOPI) și buletinul
EUIPO, compară fiecare marcă publicată cu tot portofoliul (mărcile din lista de monitorizare) și
salvează în DB conflictele găsite — afișate apoi în tabelele „Results” / „Conflicts” pe zile.

Regula de conflict (stabilită cu utilizatorul):
  • scor de similaritate ≥ pragul mărcii din portofoliu (`monitor_like`, implicit 0.6)
    ȘI cel puțin o clasă Nisa comună; SAU
  • marcă identică (scor 1), indiferent de clase.

Rulează automat în fiecare zi lucrătoare după 15:00 (ora României), după ce apar buletinele, și
manual din pagina Monitorizare, pentru orice dată.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import date as date_type, datetime
from typing import Dict, List, Optional

from db import SessionLocal
from monitor_models import MonitorConflict, MonitorRun, WatchItem

DEFAULT_MONITOR_LIKE = 0.6
SOURCES = ("osim", "euipo")

# Starea rulării curente (una singură odată) — pentru bara de progres din pagină.
_job: Dict = {"running": False}


def _norm_identity(name: str) -> str:
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)


def _watch_snapshot(item: WatchItem) -> Dict:
    return {
        "name":               item.trademark_name,
        "classes":            [str(c) for c in (item.nice_classes or [])],
        "holder":             item.holder_name or "",
        "portfolio":          item.portfolio or "",
        "origin_office":      item.origin_office or "",
        "application_number": item.application_number or "",
        "monitor_like":       item.monitor_like if item.monitor_like is not None else DEFAULT_MONITOR_LIKE,
        "reference_image":    item.reference_image or "",
    }


def compute_conflicts(marks: List[Dict], items: List[WatchItem]) -> List[Dict]:
    """Perechile (marcă din buletin, marcă din portofoliu) care respectă regula de conflict.
    CPU-bound (rapidfuzz + fonetic) — se apelează într-un executor."""
    from monitor_service import _similarity
    from scrapers.bulletin_store import _classes

    by_app = {str(m.get("applicationNumber") or ""): m for m in marks}
    rows: List[Dict] = []
    for item in items:
        name = (item.trademark_name or "").strip()
        if not name:
            continue
        threshold = item.monitor_like if item.monitor_like is not None else DEFAULT_MONITOR_LIKE
        watch_classes = {str(c) for c in (item.nice_classes or [])}
        watch_norm = _norm_identity(name)
        analysis = _similarity.analyze(name, marks, item.nice_classes or [], user_offices=item.offices)
        seen = set()
        for key in ("conflicts", "similar", "expired_conflicts", "expired_similar"):
            for entry in analysis.get(key, []):
                app = str(entry.get("applicationNumber") or "")
                if not app or app in seen:
                    continue
                orig = by_app.get(app) or entry
                identical = bool(watch_norm) and _norm_identity(orig.get("tmName") or "") == watch_norm
                # Scorul brut (textual 70% + fonetic 30%), fără bonusurile din verificarea manuală
                # (cuvânt comun / rădăcină comună ridicate la 0.60–0.75): acolo vrem să nu scăpăm
                # nimic, aici ar face din orice „RESTAURANT …” / „… SOCIETY” un conflict.
                sim = entry["similarity"]
                like = 1.0 if identical else round((sim["textual_score"] * 0.70 + sim["phonetic_score"] * 0.30) / 100.0, 2)
                overlap = watch_classes & set(_classes(orig))
                if not (identical or (overlap and like >= threshold)):
                    continue
                seen.add(app)
                rows.append({"application_number": app, "watch_item_id": item.id,
                             "like": min(like, 1.0), "identical": identical,
                             "watch": _watch_snapshot(item)})
    rows.sort(key=lambda r: -r["like"])
    return rows


def _set_run(source: str, day: str, **fields) -> None:
    db = SessionLocal()
    try:
        run = db.query(MonitorRun).filter_by(source=source, bulletin_date=day).first()
        if run is None:
            run = MonitorRun(source=source, bulletin_date=day)
            db.add(run)
        for k, v in fields.items():
            setattr(run, k, v)
        db.commit()
    finally:
        db.close()


def _save_conflicts(source: str, day: str, rows: List[Dict]) -> None:
    db = SessionLocal()
    try:
        db.query(MonitorConflict).filter_by(source=source, bulletin_date=day).delete()
        for r in rows:
            db.add(MonitorConflict(source=source, bulletin_date=day, **r))
        db.commit()
    finally:
        db.close()


def _fetch(source: str, td: date_type):
    if source == "osim":
        from scrapers.osim_bulletin import fetch_osim_for_date
        return fetch_osim_for_date(td)
    from scrapers.euipo_bulletin import fetch_euipo_for_date
    # Fără PDF la rularea principală (15–30 MB, minute întregi): mărcile vin din API-ul EUIPO;
    # adresele din PDF se completează după (vezi _merge_euipo_addresses).
    return fetch_euipo_for_date(td, skip_pdf_download=True)


def _merge_euipo_addresses(day: str) -> int:
    """API-ul EUIPO nu dă adresele solicitantului/reprezentantului; le luăm din PDF-ul buletinului
    (codurile 731 / 740) și le adăugăm mărcilor salvate. Întoarce câte mărci au primit adresă."""
    import json
    import os
    from scrapers.euipo_bulletin import (CACHE_DIR, _date_slug, _download_bulletin,
                                         _find_bulletin_for_date, _parse_bulletin_pdf)
    from monitor_models import BulletinMark

    td = date_type.fromisoformat(day)
    slug = _date_slug(td)
    addr_path = os.path.join(CACHE_DIR, f"{slug}.addresses.json")
    addresses: Optional[Dict] = None
    if os.path.exists(addr_path):
        try:
            with open(addr_path) as f:
                addresses = json.load(f)
        except Exception:
            addresses = None
    if addresses is None:
        bulletin = _find_bulletin_for_date(td)
        if not bulletin or bulletin["date"] != td:
            return 0
        local, err = _download_bulletin(bulletin["value"], slug)
        if not local:
            print(f"[DAILY] PDF EUIPO {day} indisponibil: {err}")
            return 0
        addresses = {m["applicationNumber"]: {"applicantAddress": m.get("applicantAddress", ""),
                                              "representativeAddress": m.get("representativeAddress", "")}
                     for m in _parse_bulletin_pdf(local)}
        with open(addr_path, "w") as f:
            json.dump(addresses, f, ensure_ascii=False)
        try:
            os.remove(local)           # PDF-ul e mare; ne trebuie doar adresele
        except OSError:
            pass

    db = SessionLocal()
    n = 0
    try:
        for row in db.query(BulletinMark).filter_by(source="euipo", bulletin_date=day):
            a = addresses.get(row.application_number)
            if not a or not (a.get("applicantAddress") or a.get("representativeAddress")):
                continue
            data = dict(row.data or {})
            if data.get("applicantAddress") == a.get("applicantAddress") and \
                    data.get("representativeAddress") == a.get("representativeAddress"):
                continue
            data.update({k: v for k, v in a.items() if v})
            row.data = data
            n += 1
        db.commit()
    finally:
        db.close()
    return n


async def run_daily_monitor(date_str: str, sources=SOURCES, trigger: str = "manual") -> Dict:
    """Rulează monitorizarea pentru buletinele din `date_str` (ultima zi lucrătoare ≤ data)."""
    from routes.monitor import _bulletin_day, _load_bulletin_marks, _persist_bulletin

    if _job.get("running"):
        return {"status": "already_running", **_job}
    td = date_type.fromisoformat(date_str)
    loop = asyncio.get_event_loop()
    _job.clear()
    _job.update({"running": True, "date": date_str, "trigger": trigger, "sources": {},
                 "started_at": datetime.utcnow().isoformat() + "Z"})
    summary: Dict = {}
    try:
        db = SessionLocal()
        try:
            items = db.query(WatchItem).filter(WatchItem.active == True).all()  # noqa: E712
            for it in items:           # încărcăm coloanele înainte de închiderea sesiunii
                _ = (it.trademark_name, it.nice_classes, it.offices, it.monitor_like, it.portfolio)
            db.expunge_all()
        finally:
            db.close()

        for source in sources:
            day = _bulletin_day(source, td)
            state = _job["sources"][source] = {"day": day, "phase": "descărcare buletin"}
            _set_run(source, day, status="running", trigger=trigger, message="",
                     started_at=datetime.utcnow(), finished_at=None)
            try:
                marks, info = await loop.run_in_executor(None, _fetch, source, td)
                if not marks:
                    msg = info.get("error") or "Buletinul nu a fost publicat (încă) pentru această zi."
                    _set_run(source, day, status="no_bulletin", results=0, conflicts=0,
                             message=msg, finished_at=datetime.utcnow())
                    state.update({"phase": "fără buletin", "message": msg})
                    summary[source] = {"day": day, "status": "no_bulletin", "message": msg}
                    continue
                await loop.run_in_executor(None, _persist_bulletin, source, date_str)
                marks = await loop.run_in_executor(None, _load_bulletin_marks, source, date_str)
                state.update({"phase": f"comparare {len(marks)} mărci × {len(items)} din portofoliu"})
                rows = await loop.run_in_executor(None, compute_conflicts, marks, items)
                await loop.run_in_executor(None, _save_conflicts, source, day, rows)
                _set_run(source, day, status="ok", results=len(marks), conflicts=len(rows),
                         watch_count=len(items), finished_at=datetime.utcnow())
                state.update({"phase": "gata", "results": len(marks), "conflicts": len(rows)})
                summary[source] = {"day": day, "status": "ok", "results": len(marks), "conflicts": len(rows)}
            except Exception as e:
                import traceback
                traceback.print_exc()
                msg = f"{type(e).__name__}: {e}"
                _set_run(source, day, status="error", message=msg, finished_at=datetime.utcnow())
                state.update({"phase": "eroare", "message": msg})
                summary[source] = {"day": day, "status": "error", "message": msg}

        if summary.get("euipo", {}).get("status") == "ok":
            _job["sources"]["euipo"]["phase"] = "gata (se completează adresele din PDF…)"
            try:
                n = await loop.run_in_executor(None, _merge_euipo_addresses, summary["euipo"]["day"])
                print(f"[DAILY] Adrese EUIPO completate din PDF: {n}")
            except Exception as e:
                print(f"[DAILY] Adrese EUIPO din PDF: {type(e).__name__}: {e}")
            _job["sources"]["euipo"]["phase"] = "gata"
    finally:
        _job["running"] = False
        _job["finished_at"] = datetime.utcnow().isoformat() + "Z"
        _job["summary"] = summary
    print(f"[DAILY] {date_str} ({trigger}): {summary}")
    return {"status": "done", "summary": summary}


def job_status() -> Dict:
    return dict(_job)


def needs_auto_run(source: str, day: str) -> bool:
    """Rularea automată se reia (la fiecare oră, până seara) cât timp buletinul zilei nu a fost
    găsit; o rulare reușită nu se mai repetă automat."""
    db = SessionLocal()
    try:
        run = db.query(MonitorRun).filter_by(source=source, bulletin_date=day).first()
        return run is None or run.status not in ("ok", "running")
    finally:
        db.close()
