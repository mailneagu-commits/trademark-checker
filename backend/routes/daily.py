"""
Monitorizarea zilnică pe buletine — pornire manuală (cu dată aleasă), starea rulării, lista
rulărilor pe zile / ani și tabelele „Results” / „Conflicts” ale unei zile.
"""
import asyncio
from collections import defaultdict
from datetime import date as date_type
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db import get_db
from monitor_models import BulletinMark, MonitorConflict, MonitorRun

router = APIRouter(prefix="/api/monitor/daily", tags=["daily-monitor"])


def _check_source(source: str) -> str:
    if source not in ("osim", "euipo", "wipo"):
        raise HTTPException(400, "Sursa trebuie să fie osim, euipo sau wipo.")
    return source


@router.post("/run")
async def start_daily_run(date: Optional[str] = None, source: str = "both"):
    import daily_monitor
    try:
        td = date_type.fromisoformat(date) if date else date_type.today()
    except ValueError:
        raise HTTPException(400, f"Dată invalidă: {date}. Formatul este YYYY-MM-DD.")
    if td > date_type.today():
        raise HTTPException(400, "Data nu poate fi în viitor.")
    sources = (daily_monitor.SOURCES if source == "both" else daily_monitor.ALL_SOURCES if source == "all"
               else (_check_source(source),))
    if daily_monitor.job_status().get("running"):
        return {"status": "already_running", "job": daily_monitor.job_status()}
    asyncio.ensure_future(daily_monitor.run_daily_monitor(td.isoformat(), sources, trigger="manual"))
    await asyncio.sleep(0.2)
    return {"status": "started", "job": daily_monitor.job_status()}


@router.get("/status")
def daily_status():
    import daily_monitor
    return daily_monitor.job_status()


@router.get("/runs")
def list_runs(db: Session = Depends(get_db)):
    """Rulările pe sursă, cele mai noi primele, plus totalurile pe ani."""
    out: Dict = {}
    for source in ("osim", "euipo", "wipo"):
        runs = (db.query(MonitorRun).filter_by(source=source)
                .order_by(MonitorRun.bulletin_date.desc()).all())
        years: Dict = defaultdict(lambda: {"results": 0, "conflicts": 0, "days": 0})
        for r in runs:
            if r.status == "ok":
                y = years[r.bulletin_date[:4]]
                y["results"] += r.results or 0
                y["conflicts"] += r.conflicts or 0
                y["days"] += 1
        out[source] = {
            "runs": [{
                "date": r.bulletin_date, "status": r.status, "results": r.results or 0,
                "conflicts": r.conflicts or 0, "watch_count": r.watch_count or 0,
                "trigger": r.trigger, "message": r.message or "",
                "finished_at": r.finished_at.isoformat() + "Z" if r.finished_at else None,
            } for r in runs],
            "years": [{"year": y, **v} for y, v in sorted(years.items(), reverse=True)],
        }
    return out


def _applicant_block(m: Dict) -> str:
    """731: numele + adresa, pe rânduri (ca în buletin)."""
    addr = (m.get("applicantAddress") or "").strip()
    names = [a.get("name", "") for a in (m.get("applicants") or []) if a.get("name")] \
        or list(m.get("applicantName") or [])
    # Adresa din buletin (OSIM 732 / EUIPO 731 din PDF) începe deja cu numele titularului.
    return addr or "; ".join(names)


def _representative_block(m: Dict) -> str:
    addr = (m.get("representativeAddress") or "").strip()
    if addr:
        return addr
    reps = [r.get("name") or r.get("fullName") or "" for r in (m.get("representatives") or [])]
    return "; ".join(r for r in reps if r) or (m.get("representative") or "")


def _mark_row(source: str, day: str, row: BulletinMark) -> Dict:
    m = row.data or {}
    return {
        "application_number": row.application_number,
        "image":              m.get("markImageURI") or "",
        "application_date":   (row.application_date or "")[:10],
        "publication_date":   (row.publication_date or day)[:10],
        "name":               row.trademark_name or "",
        "applicant":          _applicant_block(m),
        "representative":     _representative_block(m),
        "applicant_names":    [a.get("name", "") for a in (m.get("applicants") or []) if a.get("name")]
                              or list(m.get("applicantName") or []),
        "representative_names": [r.get("name") or r.get("fullName") or "" for r in (m.get("representatives") or [])
                                 if r.get("name") or r.get("fullName")],
        "classes":            [int(c) if str(c).isdigit() else c for c in (row.nice_classes or [])],
        "link":               f"/mark.html?source={source}&date={day}&app={row.application_number}",
    }


@router.get("/results")
def day_results(source: str, date: str, db: Session = Depends(get_db)):
    _check_source(source)
    rows = (db.query(BulletinMark).filter_by(source=source, bulletin_date=date)
            .order_by(BulletinMark.application_number).all())
    run = db.query(MonitorRun).filter_by(source=source, bulletin_date=date).first()
    return {"source": source, "date": date, "total": len(rows),
            "status": run.status if run else None,
            "marks": [_mark_row(source, date, r) for r in rows]}


@router.get("/conflicts")
def day_conflicts(source: str, date: str, db: Session = Depends(get_db)):
    _check_source(source)
    conflicts = (db.query(MonitorConflict).filter_by(source=source, bulletin_date=date)
                 .order_by(MonitorConflict.like.desc(), MonitorConflict.application_number).all())
    marks = {r.application_number: r for r in
             db.query(BulletinMark).filter_by(source=source, bulletin_date=date)}
    out: List[Dict] = []
    for c in conflicts:
        bm = marks.get(c.application_number)
        mark = _mark_row(source, date, bm) if bm else {"application_number": c.application_number}
        out.append({**mark, "like": c.like, "identical": c.identical,
                    "watch_item_id": c.watch_item_id, "monitor": c.watch or {}})
    run = db.query(MonitorRun).filter_by(source=source, bulletin_date=date).first()
    return {"source": source, "date": date, "total": len(out),
            "status": run.status if run else None, "conflicts": out}
