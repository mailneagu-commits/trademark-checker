"""
Registrul public de mărci OSIM — site separat de aplicația internă de monitorizare.
Căutare (denumire / titular / reprezentant / clase), pagina mărcii, statistici, clasamentul
reprezentanților, cereri recente și formularul de solicitare informații.
"""
import os
from collections import Counter
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

import ingest
from bopi_parser import display_app
from db import IS_PG, Lead, Mark, MarkImage, SourceFile, get_db, init_db, norm

app = FastAPI(title="Registru mărci OSIM", docs_url=None, redoc_url=None)
STATIC = os.path.join(os.path.dirname(__file__), "static")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


@app.on_event("startup")
def _startup():
    init_db()
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone="Europe/Bucharest")
    # Buletinul zilnic apare după-amiaza; reîncercăm seara dacă întârzie.
    sched.add_job(ingest.start_background, "cron", day_of_week="mon-fri", hour="16,19", minute=30,
                  id="bopi_ingest")
    import mailer
    sched.add_job(mailer.keepalive, "cron", day_of_week="mon", hour=9, id="brevo_keepalive")
    sched.start()
    if os.environ.get("AUTO_INGEST", "1") == "1":
        ingest.start_background()


@app.middleware("http")
async def _cache_headers(request: Request, call_next):
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if ct.startswith(("text/html", "text/javascript", "application/javascript", "text/css")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ── Serializare ─────────────────────────────────────────────────────────────────

def _status(m: Mark) -> str:
    if m.status == "Înregistrată" and m.expiry_date and m.expiry_date < date.today().isoformat():
        return "Expirată"
    return m.status


def _row(m: Mark) -> dict:
    return {
        "app_number": m.app_number, "app": display_app(m.app_number), "name": m.name,
        "status": _status(m), "mark_type": m.mark_type, "filing_date": m.filing_date,
        "publication_date": m.publication_date, "reg_number": m.reg_number, "reg_date": m.reg_date,
        "expiry_date": m.expiry_date or ingest.expiry_of(m), "holder": m.holder, "representative": m.representative,
        "classes": m.classes or [], "image": f"/api/image/{m.app_number}" if m.has_image else "",
        # Mărci reînnoite înregistrate înainte de arhiva disponibilă: denumirea nu apare în BOPI.
        "placeholder": "denumire necunoscută — marcă reînnoită" if m.app_number.startswith("R") else "",
    }


def _detail(m: Mark) -> dict:
    d = _row(m)
    d.update(holder_address=m.holder_address, representative_address=m.representative_address,
             goods=m.goods or {}, vienna=m.vienna, colors=m.colors, disclaimer=m.disclaimer, events=m.events or [],
             status_date=m.status_date)
    return d


# ── API ─────────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    total = db.query(func.count(Mark.app_number)).scalar() or 0
    holders = db.query(func.count(func.distinct(Mark.holder_norm))).filter(Mark.holder_norm != "").scalar() or 0
    reps = db.query(func.count(func.distinct(Mark.rep_norm))).filter(Mark.rep_norm != "").scalar() or 0
    by_status = dict(db.query(Mark.status, func.count(Mark.app_number)).group_by(Mark.status).all())
    last = db.query(func.max(Mark.publication_date)).scalar() or ""
    week = {}
    if last:
        start = (date.fromisoformat(last) - timedelta(days=6)).isoformat()
        rows = db.query(Mark).filter(Mark.publication_date >= start, Mark.publication_date <= last).all()
        cls = Counter(c for r in rows for c in (r.classes or []))
        week = {
            "from": start, "to": last, "published": len(rows),
            "many_classes": sum(1 for r in rows if len(r.classes or []) >= 3),
            "figurative": sum(1 for r in rows if r.has_image),
            "verbal": sum(1 for r in rows if not r.has_image),
            "top_classes": [{"class": c, "count": n} for c, n in cls.most_common(5)],
        }
    valid = db.query(func.count(Mark.app_number)).filter(Mark.status == "Înregistrată",
                                                        Mark.expiry_date >= date.today().isoformat()).scalar() or 0
    return {"marks": total, "holders": holders, "representatives": reps, "by_status": by_status, "valid": valid,
            "last_bulletin": last, "week": week}


@app.get("/api/search")
def search(name: str = "", holder: str = "", representative: str = "", classes: str = "",
           status: str = "", mode: str = "contains", page: int = 1, size: int = 20,
           db: Session = Depends(get_db)):
    size = min(max(size, 5), 100)
    page = max(page, 1)
    q = db.query(Mark)
    n, h, r = norm(name), norm(holder), norm(representative)
    if not (n or h or r or classes.strip()):
        raise HTTPException(400, "Introduceți o denumire, un titular, un reprezentant sau o clasă.")
    order = []
    if n:
        if mode == "similar" and IS_PG:
            sim = func.similarity(Mark.name_norm, n)
            q = q.filter(or_(Mark.name_norm.op("%")(n), Mark.name_norm.contains(n)))
            order.append(sim.desc())
        else:
            q = q.filter(Mark.name_norm.contains(n))
            order += [case((Mark.name_norm == n, 0), (Mark.name_norm.startswith(n), 1), else_=2),
                      func.length(Mark.name_norm)]
    if h:
        q = q.filter(Mark.holder_norm.contains(h))
    if r:
        q = q.filter(Mark.rep_norm.contains(r))
    wanted = [int(c) for c in classes.replace(";", ",").replace(" ", ",").split(",") if c.strip().isdigit()]
    today = date.today().isoformat()
    if status == "Valabile":
        q = q.filter(Mark.status == "Înregistrată", Mark.expiry_date >= today)
    elif status == "Expirată":
        q = q.filter(Mark.status == "Înregistrată", Mark.expiry_date != "", Mark.expiry_date < today)
    elif status == "Înregistrată":
        q = q.filter(Mark.status == "Înregistrată", or_(Mark.expiry_date == "", Mark.expiry_date >= today))
    elif status:
        q = q.filter(Mark.status == status)
    order.append(Mark.filing_date.desc())
    if wanted:
        # Clasele sunt în JSON — filtrăm în Python pe un set deja restrâns.
        rows = [m for m in q.order_by(*order).limit(5000).all()
                if set(wanted) & set(m.classes or [])]
        total = len(rows)
        rows = rows[(page - 1) * size: page * size]
    else:
        total = q.count()
        rows = q.order_by(*order).offset((page - 1) * size).limit(size).all()
    return {"total": total, "page": page, "size": size, "results": [_row(m) for m in rows]}


@app.get("/api/mark/{app_number}")
def mark(app_number: str, db: Session = Depends(get_db)):
    m = db.get(Mark, app_number.replace(" ", "").upper())
    if not m:
        raise HTTPException(404, "Marca nu există în registru.")
    return _detail(m)


@app.get("/api/image/{app_number}")
def image(app_number: str, db: Session = Depends(get_db)):
    img = db.get(MarkImage, app_number)
    if not img:
        raise HTTPException(404)
    return Response(img.data, media_type=img.content_type,
                    headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/recent")
def recent(day: str = "", db: Session = Depends(get_db)):
    dates = [d for (d,) in db.query(Mark.publication_date).filter(Mark.publication_date != "")
             .group_by(Mark.publication_date).order_by(Mark.publication_date.desc()).limit(30)]
    day = day or (dates[0] if dates else "")
    rows = db.query(Mark).filter(Mark.publication_date == day).order_by(Mark.app_number).all() if day else []
    counts = dict(db.query(Mark.publication_date, func.count(Mark.app_number))
                  .filter(Mark.publication_date.in_(dates)).group_by(Mark.publication_date).all()) if dates else {}
    return {"date": day, "dates": [{"date": d, "count": counts.get(d, 0)} for d in dates],
            "results": [_row(m) for m in rows]}


class LeadIn(BaseModel):
    kind: str = "info"
    name: str
    email: str
    phone: str = ""
    message: str = ""
    marks: List[str] = []


@app.post("/api/lead")
def lead(body: LeadIn, db: Session = Depends(get_db)):
    if "@" not in body.email or not body.name.strip():
        raise HTTPException(400, "Completați numele și o adresă de email validă.")
    db.add(Lead(kind=body.kind if body.kind in ("info", "cercetare") else "info", name=body.name[:200],
                email=body.email[:200], phone=body.phone[:50], message=body.message[:4000],
                marks=body.marks[:50]))
    db.commit()
    import mailer
    mailer.notify_lead(body.kind, body.name, body.email, body.phone, body.message, body.marks[:50])
    return {"ok": True}


# ── Administrare (token în ADMIN_TOKEN) ─────────────────────────────────────────

def _admin(x_admin_token: str = Header("")):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Acces interzis.")


@app.get("/api/status")
def public_status(db: Session = Depends(get_db)):
    """Progresul încărcării buletinelor (fără detalii sensibile)."""
    by = dict(db.query(SourceFile.status, func.count(SourceFile.url)).group_by(SourceFile.status).all())
    last = db.query(func.max(SourceFile.bulletin_date)).filter(SourceFile.status == "ok").scalar()
    return {"running": ingest.job["running"], "current": ingest.job["current"], "done": ingest.job["done"],
            "total": ingest.job["total"], "sources": by, "last_bulletin": last,
            "marks": db.query(func.count(Mark.app_number)).scalar() or 0}


@app.get("/api/mail-status")
def mail_status():
    import mailer
    return {"configured": mailer.available(), "provider": mailer.provider(), "sender": mailer._sender(),
            "to": mailer.LEAD_EMAIL, "last": mailer.last}


@app.get("/api/quality")
def quality(db: Session = Depends(get_db)):
    """Raport de calitate a datelor citite din BOPI (doar date publice): câte mărci au câmpuri
    lipsă sau suspecte, cu exemple, ca problemele de citire a PDF-urilor să fie găsite ușor."""
    import re as _re
    checks = {
        "fara_denumire_si_fara_logo": lambda m: not m.name and not m.has_image,
        "fara_clase": lambda m: not (m.classes or []),
        "fara_titular": lambda m: not m.holder,
        "fara_data_depozit": lambda m: not m.filing_date,
        "denumire_lunga": lambda m: len(m.name or "") > 70,
        "denumire_cu_coduri_inid": lambda m: bool(_re.search(r"\(\d{3}\)|Clasificare|Produse și|BULETIN", m.name or "")),
        "titular_lung": lambda m: len(m.holder or "") > 90,
        "titular_fara_adresa": lambda m: bool(m.holder) and m.holder_address == m.holder,
        "inregistrata_fara_nr": lambda m: m.status == "Înregistrată" and not m.reg_number,
        "clase_fara_produse": lambda m: any(not (m.goods or {}).get(str(c)) for c in (m.classes or [])),
    }
    counts = {k: 0 for k in checks}
    examples = {k: [] for k in checks}
    total = 0
    for m in db.query(Mark).yield_per(2000):
        total += 1
        for k, f in checks.items():
            if f(m):
                counts[k] += 1
                if len(examples[k]) < 12:
                    examples[k].append({"app": m.app_number, "name": (m.name or "")[:120],
                                        "holder": (m.holder or "")[:100], "status": m.status,
                                        "bopi": (m.events or [{}])[-1].get("bopi", "")})
    return {"total": total, "counts": counts, "examples": examples}


@app.get("/api/admin/ingest", dependencies=[Depends(_admin)])
def ingest_status(db: Session = Depends(get_db)):
    by = dict(db.query(SourceFile.status, func.count(SourceFile.url)).group_by(SourceFile.status).all())
    errors = [{"label": s.label, "error": s.error} for s in
              db.query(SourceFile).filter(SourceFile.status == "error").limit(20)]
    return {"job": ingest.job, "sources": by, "errors": errors}


@app.post("/api/admin/ingest", dependencies=[Depends(_admin)])
def ingest_start(limit: Optional[int] = None):
    return {"started": ingest.start_background(limit)}


@app.get("/api/admin/leads", dependencies=[Depends(_admin)])
def leads(db: Session = Depends(get_db)):
    return [{"id": l.id, "kind": l.kind, "name": l.name, "email": l.email, "phone": l.phone,
             "message": l.message, "marks": l.marks, "created_at": l.created_at.isoformat() + "Z"}
            for l in db.query(Lead).order_by(Lead.id.desc()).limit(500)]


# ── Pagini ──────────────────────────────────────────────────────────────────────

@app.get("/robots.txt")
def robots():
    # Până la lansare (INDEXING=1 în Railway) site-ul nu e indexat de motoarele de căutare.
    if os.environ.get("INDEXING") == "1":
        return Response("User-agent: *\nAllow: /\n", media_type="text/plain")
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.middleware("http")
async def _noindex(request: Request, call_next):
    resp = await call_next(request)
    if os.environ.get("INDEXING") != "1":
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/marca/{app_number}")
def mark_page(app_number: str):
    return FileResponse(os.path.join(STATIC, "marca.html"))


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
