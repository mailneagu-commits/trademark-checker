"""
Baza de date cu mărcile din buletinele OSIM/EUIPO: toate informațiile aduse despre fiecare marcă
(buletin + detalii TMview/API EUIPO + clase descrise), salvate ca să le putem consulta oricând,
chiar dacă fișierele din cache (PDF-uri, JSON) au dispărut.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import Integer, cast, func, or_

from db import SessionLocal
from monitor_models import BulletinMark, BulletinImage


def _applicant_text(m: Dict) -> str:
    names = [a.get("name", "") for a in (m.get("applicants") or []) if a.get("name")]
    return "; ".join(names) or "; ".join(m.get("applicantName") or [])


def _representative_text(m: Dict) -> str:
    names = [r.get("name") or r.get("fullName") or "" for r in (m.get("representatives") or [])]
    return "; ".join(n for n in names if n) or (m.get("representative") or "")


def _classes(m: Dict) -> List[str]:
    return sorted({str(c) for c in (m.get("niceClass") or [])}, key=lambda c: int(c) if c.isdigit() else 0)


def save_bulletin_marks(source: str, bulletin_date: str, marks: List[Dict]) -> Dict:
    """Upsert pe (sursă, nr. cerere, data buletinului). Nu suprascrie o marcă deja îmbogățită cu
    una neîmbogățită — conținutul unui buletin publicat nu se schimbă, doar se completează."""
    db = SessionLocal()
    added = updated = 0
    try:
        existing = {r.application_number: r for r in
                    db.query(BulletinMark).filter_by(source=source, bulletin_date=bulletin_date)}
        now = datetime.utcnow()
        for pos, m in enumerate(marks):
            app = str(m.get("applicationNumber") or "").strip()
            if not app:
                continue
            row = existing.get(app)
            if row is None:
                row = BulletinMark(source=source, bulletin_date=bulletin_date, application_number=app,
                                   first_saved_at=now)
                db.add(row)
                existing[app] = row
                added += 1
            elif row.detail_enriched and not m.get("_detail_enriched"):
                row.position = pos          # nu pierdem detaliile deja salvate
                continue
            else:
                updated += 1
            row.position         = pos
            row.trademark_name   = m.get("tmName") or ""
            row.applicant        = _applicant_text(m)
            row.representative   = _representative_text(m)
            row.nice_classes     = _classes(m)
            row.status           = m.get("markCurrentStatusCode") or m.get("tradeMarkStatus") or ""
            row.application_date = (m.get("applicationDate") or "")[:10]
            row.publication_date = (m.get("publicationDate") or "")[:10]
            row.detail_enriched  = bool(m.get("_detail_enriched"))
            row.data             = m
            row.updated_at       = now
        db.commit()
        return {"added": added, "updated": updated, "total": len(marks)}
    finally:
        db.close()


def load_bulletin_marks(source: str, bulletin_date: str) -> List[Dict]:
    db = SessionLocal()
    try:
        rows = (db.query(BulletinMark).filter_by(source=source, bulletin_date=bulletin_date)
                .order_by(BulletinMark.position, BulletinMark.id).all())
        return [dict(r.data or {}) for r in rows]
    finally:
        db.close()


def list_saved_bulletins() -> List[Dict]:
    db = SessionLocal()
    try:
        q = (db.query(BulletinMark.source, BulletinMark.bulletin_date, func.count(BulletinMark.id),
                      func.sum(cast(BulletinMark.detail_enriched, Integer)), func.max(BulletinMark.updated_at))
             .group_by(BulletinMark.source, BulletinMark.bulletin_date)
             .order_by(BulletinMark.bulletin_date.desc(), BulletinMark.source))
        return [{"source": s, "date": d, "total": int(n), "enriched": int(e or 0),
                 "updated_at": u.isoformat() if u else None} for s, d, n, e, u in q.all()]
    finally:
        db.close()


def search_saved_marks(q: str = "", source: str = "", date: str = "", nice_class: str = "",
                       limit: int = 100, offset: int = 0) -> Dict:
    db = SessionLocal()
    try:
        query = db.query(BulletinMark)
        if source:
            query = query.filter(BulletinMark.source == source)
        if date:
            query = query.filter(BulletinMark.bulletin_date == date)
        q = (q or "").strip()
        if q:
            like = f"%{q}%"
            query = query.filter(or_(BulletinMark.trademark_name.ilike(like),
                                     BulletinMark.applicant.ilike(like),
                                     BulletinMark.representative.ilike(like),
                                     BulletinMark.application_number.ilike(like)))
        query = query.order_by(BulletinMark.bulletin_date.desc(), BulletinMark.source, BulletinMark.position)
        rows = query.all()
        if nice_class:
            rows = [r for r in rows if str(nice_class) in (r.nice_classes or [])]
        total = len(rows)
        page = rows[offset:offset + limit]
        return {"total": total, "offset": offset, "limit": limit, "marks": [{
            "source": r.source, "bulletin_date": r.bulletin_date, "application_number": r.application_number,
            "name": r.trademark_name, "applicant": r.applicant, "representative": r.representative,
            "classes": r.nice_classes or [], "status": r.status, "application_date": r.application_date,
            "publication_date": r.publication_date, "detail_enriched": r.detail_enriched,
        } for r in page]}
    finally:
        db.close()


# ── imagini ──────────────────────────────────────────────────────────────────

def save_image(source: str, application_number: str, data: bytes, content_type: str = "image/png") -> None:
    if not data:
        return
    db = SessionLocal()
    try:
        row = db.get(BulletinImage, (source, application_number))
        if row is None:
            db.add(BulletinImage(source=source, application_number=application_number,
                                 content_type=content_type, data=data))
        else:
            row.data, row.content_type, row.saved_at = data, content_type, datetime.utcnow()
        db.commit()
    finally:
        db.close()


def get_image(source: str, application_number: str) -> Optional[Dict]:
    db = SessionLocal()
    try:
        row = db.get(BulletinImage, (source, application_number))
        return {"data": bytes(row.data), "content_type": row.content_type} if row else None
    finally:
        db.close()


def has_image(source: str, application_number: str) -> bool:
    db = SessionLocal()
    try:
        return db.get(BulletinImage, (source, application_number)) is not None
    finally:
        db.close()
