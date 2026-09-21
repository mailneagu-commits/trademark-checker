"""
Verificările de disponibilitate salvate: fiecare căutare (denumire + clase + teritorii) se păstrează
automat cu rezultatele complete, se poate redeschide fără să mai repetăm căutarea și se poate șterge
pe măsură ce nu mai e nevoie de ea.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import SessionLocal
from monitor_models import SavedCheck

router = APIRouter(prefix="/api/checks", tags=["checks"])


def _key(name: str, classes, offices) -> str:
    cl = ",".join(sorted({str(c) for c in classes or []}, key=lambda c: (len(c), c)))
    of = ",".join(sorted({str(o).upper() for o in offices or []}))
    return f"{(name or '').strip().upper()}|{cl}|{of}"


def save_check(request, result: Dict) -> Optional[int]:
    """Salvează (sau actualizează) verificarea. Nu aruncă erori — salvarea nu trebuie să strice căutarea.
    Rezultatele demo (TMview blocat → date fictive) nu se salvează."""
    try:
        source = str(result.get("source") or "")
        if source.startswith("demo"):
            return None
        name    = result.get("query") or request.trademark_name
        classes = result.get("nice_classes") or request.nice_classes
        offices = result.get("offices") or request.offices
        key = _key(name, classes, offices)
        now = datetime.utcnow()
        db = SessionLocal()
        try:
            row = db.query(SavedCheck).filter_by(check_key=key).first()
            if row is None:
                row = SavedCheck(check_key=key, created_at=now, check_count=0, note="")
                db.add(row)
            row.trademark_name  = name
            row.nice_classes    = list(classes)
            row.offices         = list(offices)
            row.include_expired = bool(getattr(request, "include_expired", True))
            row.total_found     = int(result.get("total_found") or 0)
            row.risky_count     = int(result.get("risky_marks") or 0)
            row.similar_count   = int(result.get("similar_marks") or 0)
            row.ended_count     = len(result.get("ended_marks") or []) + len(result.get("terminated_marks") or [])
            row.expired_count   = len(result.get("expired_conflicts") or []) + len(result.get("expired_similar") or [])
            row.source          = source
            row.check_count     = (row.check_count or 0) + 1
            row.data            = result
            row.updated_at      = now
            db.commit()
            return row.id
        finally:
            db.close()
    except Exception as e:
        print(f"[CHECKS] Salvare eșuată: {type(e).__name__}: {e}")
        return None


def _summary(r: SavedCheck) -> Dict:
    return {
        "id": r.id, "name": r.trademark_name, "classes": r.nice_classes or [], "offices": r.offices or [],
        "total_found": r.total_found, "risky": r.risky_count, "similar": r.similar_count,
        "ended": r.ended_count, "expired": r.expired_count, "source": r.source, "note": r.note or "",
        "check_count": r.check_count,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("")
def list_checks(q: str = "", limit: int = 200, offset: int = 0):
    db = SessionLocal()
    try:
        query = db.query(SavedCheck)
        q = (q or "").strip()
        if q:
            like = f"%{q}%"
            query = query.filter(SavedCheck.trademark_name.ilike(like) | SavedCheck.note.ilike(like))
        total = query.count()
        rows = query.order_by(SavedCheck.updated_at.desc()).offset(max(offset, 0)).limit(min(max(limit, 1), 500)).all()
        return {"total": total, "checks": [_summary(r) for r in rows]}
    finally:
        db.close()


@router.get("/{check_id}")
def get_check(check_id: int):
    db = SessionLocal()
    try:
        r = db.get(SavedCheck, check_id)
        if not r:
            raise HTTPException(404, "Verificarea nu mai există (a fost ștearsă).")
        return {**_summary(r), "result": {**(r.data or {}), "check_id": r.id}}
    finally:
        db.close()


class NoteBody(BaseModel):
    note: str = ""


@router.patch("/{check_id}")
def update_note(check_id: int, body: NoteBody):
    db = SessionLocal()
    try:
        r = db.get(SavedCheck, check_id)
        if not r:
            raise HTTPException(404, "Verificarea nu mai există.")
        r.note = body.note.strip()[:500]
        db.commit()
        return _summary(r)
    finally:
        db.close()


@router.put("/{check_id}/data")
def update_data(check_id: int, result: Dict):
    """Actualizează rezultatul salvat (de ex. după ce pagina a adus detaliile reprezentanților)."""
    if not isinstance(result, dict) or "results" not in result:
        raise HTTPException(400, "Rezultat invalid.")
    db = SessionLocal()
    try:
        r = db.get(SavedCheck, check_id)
        if not r:
            raise HTTPException(404, "Verificarea nu mai există.")
        result = {k: v for k, v in result.items() if k != "check_id"}
        r.data = result
        r.updated_at = datetime.utcnow()
        db.commit()
        return {"status": "updated"}
    finally:
        db.close()


@router.delete("/{check_id}")
def delete_check(check_id: int):
    db = SessionLocal()
    try:
        r = db.get(SavedCheck, check_id)
        if not r:
            raise HTTPException(404, "Verificarea nu mai există.")
        db.delete(r)
        db.commit()
        return {"status": "deleted", "deleted": 1}
    finally:
        db.close()


class BulkDelete(BaseModel):
    ids: List[int] = []
    older_than_days: Optional[int] = None    # șterge tot ce n-a mai fost verificat de N zile


@router.post("/delete")
def delete_many(body: BulkDelete):
    if not body.ids and body.older_than_days is None:
        raise HTTPException(400, "Trimite ids sau older_than_days.")
    db = SessionLocal()
    try:
        query = db.query(SavedCheck)
        if body.ids:
            query = query.filter(SavedCheck.id.in_(body.ids))
        else:
            query = query.filter(SavedCheck.updated_at < datetime.utcnow() - timedelta(days=max(body.older_than_days, 0)))
        n = query.delete(synchronize_session=False)
        db.commit()
        return {"status": "deleted", "deleted": n}
    finally:
        db.close()
