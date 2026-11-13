"""
Registrul public de mărci OSIM — site separat de aplicația internă de monitorizare.
Căutare (denumire / titular / reprezentant / clase), pagina mărcii, statistici, clasamentul
reprezentanților, cereri recente și formularul de solicitare informații.
"""
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

import courts
import ingest

# Litigiile din Portalul instanțelor (courts.py) — puse pe pauză; datele rămân în baza de date.
COURTS_ENABLED = os.environ.get("COURTS_ENABLED", "0") == "1"
from bopi_parser import display_app
from db import IS_PG, Lead, Mark, MarkChange, MarkImage, SourceFile, get_db, init_db, norm

app = FastAPI(title="Registru mărci OSIM", docs_url=None, redoc_url=None)
STATIC = os.path.join(os.path.dirname(__file__), "static")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


@app.on_event("startup")
def _startup():
    init_db()
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone="Europe/Bucharest")
    # Buletinul zilnic apare după-amiaza: îl citim la 15:00 (osim.ro, nu TMview — nu încurcă fereastra
    # ProMonitor), ca mărcile noi să apară în ProWatch odată cu monitorizarea; reîncercăm la 16:00 și 19:00.
    sched.add_job(ingest.start_background, "cron", day_of_week="mon-fri", hour="15,16,19", minute=0,
                  id="bopi_ingest", misfire_grace_time=1800, coalesce=True)
    import mailer
    sched.add_job(mailer.keepalive, "cron", day_of_week="mon", hour=9, id="brevo_keepalive")
    # Denumirile mărcilor reînnoite vechi, din TMview — la 20 de minute, în paralel cu încărcarea.
    # misfire_grace_time: cât timp încărcarea PDF-urilor ține procesorul ocupat, planificatorul se
    # trezește cu întârziere, iar fără toleranță (implicit 1 s) rulările se pierd.
    sched.add_job(ingest.enrich_renewed, "interval", minutes=20, id="tmview_enrich",
                  misfire_grace_time=900, coalesce=True)
    sched.add_job(ingest.enrich_logos, "interval", minutes=20, id="tmview_logos",
                  misfire_grace_time=1800, coalesce=True)
    # Produsele și serviciile mărcilor reînnoite vechi, din fișa TMview.
    sched.add_job(ingest.enrich_goods, "interval", minutes=5, id="tmview_goods",
                  misfire_grace_time=900, coalesce=True)
    # Starea cererilor în curs (opoziție, contestație…) din TMview.
    sched.add_job(ingest.enrich_status, "interval", minutes=15, id="tmview_status",
                  misfire_grace_time=1800, coalesce=True)
    # Dosarele cu obiect „marcă” din Portalul instanțelor — săptămânal (duminică noaptea).
    # Oprit deocamdată, la cererea utilizatorului (reluăm după finalizarea bazei de mărci).
    if COURTS_ENABLED:
        sched.add_job(courts.sync, "cron", day_of_week="sun", hour=2, id="courts_sync",
                      misfire_grace_time=7200, coalesce=True)
        courts.start_backfill_if_empty()
    # Siglele originale din TMview, pentru toate mărcile — pornește singur după terminarea încărcării.
    sched.add_job(ingest.enrich_logos_hq, "interval", minutes=15, id="tmview_logos_hq",
                  misfire_grace_time=1800, coalesce=True)
    # Mărcile vechi (depozit până în 2001) încetate înainte de arhiva BOPI online — din TMview, treptat.
    sched.add_job(ingest.enrich_old, "interval", minutes=10, id="tmview_old",
                  misfire_grace_time=1800, coalesce=True)
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

GRACE = "În termen de grație"
RENEW_MAYBE = "Posibil în curs de reînnoire"


def _grace_start(months: int = 6) -> str:
    """Reînnoirea se mai poate cere 6 luni după expirare (cu taxă suplimentară) — OSIM o arată
    „în curs de reînnoire”; până apare reînnoirea în BOPI, marca nu e încă expirată definitiv."""
    t = date.today()
    k = t.year * 12 + t.month - 1 - months
    return date(k // 12, k % 12 + 1, min(t.day, 28)).isoformat()


def _status(m: Mark) -> str:
    today = date.today().isoformat()
    if not m.status:
        return "În curs de completare"
    if m.status == "Înregistrată" and m.expiry_date and m.expiry_date < today:
        if "renew" in (m.tm_status or "").lower():
            return "În curs de reînnoire"
        if m.expiry_date >= _grace_start():
            return GRACE
        # Regula confirmată cu utilizatorul: 6–12 luni după expirare, fără reînnoire publicată, dar TMview o
        # arată tot „Registered” — reînnoirea poate fi cerută și nepublicată încă (OSIM: „în curs de
        # reînnoire”), ex. M 2016 01750. După 12 luni, sau dacă TMview o dă expirată → „Expirată”.
        if (m.tm_status or "").lower().startswith("registered") and m.expiry_date >= _grace_start(12):
            return RENEW_MAYBE
        return "Expirată"
    if m.status == "Înregistrată" and "renew" in (m.tm_status or "").lower():
        return "În curs de reînnoire"
    # Cereri în curs: opoziția / contestația / retragerea apar doar în TMview, nu în BOPI.
    if m.status in ("Cerere publicată", "Admisă la înregistrare") and ingest.tm_status_ro(m.tm_status or ""):
        return ingest.tm_status_ro(m.tm_status)
    # Mărci înregistrate anulate / decăzute / la care s-a renunțat — după TMview, dacă e mai nou decât BOPI.
    if m.status == "Înregistrată" and ingest.tm_ended_ro(m.tm_status or "") and \
            (m.tm_status_date or "9999") >= (m.status_date or ""):
        return ingest.tm_ended_ro(m.tm_status)
    return m.status


def _renewals(m: Mark) -> int:
    """De câte ori a fost reînnoită marca: din depozit și expirare (fiecare reînnoire = +10 ani);
    fără data de depozit — câte reînnoiri apar în BOPI (cel puțin una pentru mărcile „R…”)."""
    # Numărul tipărit de OSIM în lista de reînnoiri („008649(5R)”) are prioritate.
    printed = max((e.get("renewal_no") or 0 for e in (m.events or [])), default=0)
    if printed:
        return printed
    exp = m.expiry_date or ""
    if m.filing_date and exp:
        n = round((int(exp[:4]) - int(m.filing_date[:4])) / 10) - 1
        if n >= 0:
            return n
    n = sum(1 for e in (m.events or []) if (e.get("chapter") or "").startswith("Reînnoire"))
    return max(n, 1 if m.app_number.startswith("R") else 0)


def _reg_display(m: Mark) -> str:
    """Nr. de marcă cu prefixul reînnoirilor: R129804 (o dată), 2R129804, 3R… """
    if not m.reg_number:
        return ""
    n = _renewals(m)
    return f"{'' if n < 1 else 'R' if n == 1 else f'{n}R'}{m.reg_number}"


def _deposit(m: Mark) -> str:
    """Nr. de depozit afișat. Mărcile reînnoite vechi („R…”) îl au doar din TMview sau din tabelele
    BOPI (până la unirea cu fișa originală din arhivă); altfel rămâne necunoscut."""
    if not m.app_number.startswith("R"):
        return display_app(m.app_number)
    for e in reversed(m.events or []):
        app = re.sub(r"\s+", "", e.get("app") or "")
        if re.fullmatch(r"M\d{8,10}", app):
            return display_app(app)
        d = re.search(r"nr\. depozit ([A-Z]?[\d ]{4,})", e.get("detail") or "")
        if d and d.group(1).strip():
            return display_app(re.sub(r"\s+", "", d.group(1)))
    return ""


def _row(m: Mark) -> dict:
    reg = _reg_display(m)
    return {
        "app_number": m.app_number, "name": m.name, "reg_display": reg, "deposit": _deposit(m),
        "app": reg if m.app_number.startswith("R") else display_app(m.app_number),
        "status": _status(m), "mark_type": m.mark_type, "filing_date": m.filing_date,
        "publication_date": m.publication_date, "reg_number": m.reg_number, "reg_date": m.reg_date,
        "expiry_date": m.expiry_date or ingest.expiry_of(m), "holder": m.holder, "representative": m.representative,
        "classes": m.classes or [], "image": f"/api/image/{m.app_number}" if m.has_image else "",
        # Mărci reînnoite înregistrate înainte de arhiva disponibilă: denumirea nu apare în BOPI.
        "placeholder": "denumire necunoscută — marcă reînnoită" if m.app_number.startswith("R") and not m.name
                       else "date de bază din TMview — fișa completă urmează" if "|skel" in (m.tmview_checked or "") else "",
    }


def _cases(db, app: str) -> list:
    from db import CourtCase, CourtLink
    out = []
    for link in db.query(CourtLink).filter(CourtLink.app_number == app):
        c = db.get(CourtCase, link.case_id)
        if c is None:
            continue
        out.append({"numar": c.numar, "instanta": courts.INSTITUTIONS.get(c.institutie, c.institutie),
                    "data": c.data, "stadiu": c.stadiu, "obiect": c.obiect, "departament": c.departament,
                    "parti": c.parti or [], "sedinte": (c.sedinte or [])[:6], "cai_atac": c.cai_atac or [],
                    "potrivire": link.kind})
    return sorted(out, key=lambda c: (c["potrivire"] != "nr", c["data"]), reverse=False)


def _detail(m: Mark, db=None) -> dict:
    d = _row(m)
    if db is not None and COURTS_ENABLED:
        d["cases"] = _cases(db, m.app_number)
    d.update(holder_address=m.holder_address, representative_address=m.representative_address,
             goods=m.goods or {}, vienna=m.vienna, colors=m.colors, disclaimer=m.disclaimer, events=m.events or [],
             status_date=m.status_date, tm_status=m.tm_status or "", tm_status_date=m.tm_status_date or "")
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


# Același reprezentant, scris în BOPI în multe feluri (și preluat între timp de altă formă juridică):
# o căutare după oricare variantă le dă pe toate. PROTECTMARK SRL a preluat mărcile Cabinetului individual
# Cătălin Neagu; schimbarea de reprezentant nu apare întotdeauna în BOPI (ex. OPERA share management).
REP_GROUPS = [
    ("protectmark", "protect mark", "catalin neagu", "neagu c catalin", "neagu catalin"),
]


@app.get("/api/search")
def search(name: str = "", holder: str = "", representative: str = "", classes: str = "",
           status: str = "", mode: str = "contains", page: int = 1, size: int = 20,
           db: Session = Depends(get_db)):
    size = min(max(size, 5), 100)
    page = max(page, 1)
    q = db.query(Mark)
    n, h, r = norm(name), norm(holder), norm(representative)
    if not (n or h or r or classes.strip() or status):
        raise HTTPException(400, "Introduceți o denumire, un titular, un reprezentant, o clasă sau alegeți o stare.")
    order = []
    num = re.sub(r"[\s/.-]", "", name).upper()
    if n and re.fullmatch(r"(M\d{8,10}|\d{1,7}|\d?R\d{1,7})", num):
        # Căutare după nr. de depozit / de marcă (032709, M 2016 01750, 3R29570) — sau denumire cu cifre.
        digits = re.sub(r"^\d?R", "", num)
        keys = {num, digits.zfill(6), "R" + digits.lstrip("0")}
        byno = or_(Mark.app_number.in_(keys), Mark.reg_number == digits.lstrip("0"))
        stub = _by_deposit(db, num) if num.startswith("M") else None
        if stub is not None:
            byno = or_(byno, Mark.app_number == stub.app_number)
        if not num.startswith("R") and "R" not in num[:2] and not db.query(Mark.app_number).filter(byno).first() \
                and not ingest._tmview_paused():
            ingest.fetch_old(num)          # marcă veche, încetată înainte de arhiva BOPI online
        q = q.filter(or_(byno, Mark.name_norm.contains(n)))
        order.append(case((byno, 0), else_=1))
        n = ""
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
        group = next((g for g in REP_GROUPS if any(a in r for a in g)), None)
        q = q.filter(or_(*[Mark.rep_norm.contains(a) for a in group]) if group else Mark.rep_norm.contains(r))
    wanted = [int(c) for c in classes.replace(";", ",").replace(" ", ",").split(",") if c.strip().isdigit()]
    today = date.today().isoformat()
    # Încetate după TMview (anulare, decădere, renunțare) — nu mai sunt valabile.
    ended = or_(*[Mark.tm_status.ilike(f"%{k}%") for k, _ in ingest.TM_ENDED])
    alive = or_(Mark.tm_status.is_(None), ~ended)
    if status == "Valabile":
        q = q.filter(Mark.status == "Înregistrată", Mark.expiry_date >= today, alive)
    elif status == "Expirată":
        q = q.filter(Mark.status == "Înregistrată", Mark.expiry_date != "", Mark.expiry_date < _grace_start(),
                     ~(Mark.tm_status.ilike("registered%") & (Mark.expiry_date >= _grace_start(12))))
    elif status == RENEW_MAYBE:
        q = q.filter(Mark.status == "Înregistrată", Mark.expiry_date < _grace_start(),
                     Mark.expiry_date >= _grace_start(12), Mark.tm_status.ilike("registered%"))
    elif status == GRACE:
        q = q.filter(Mark.status == "Înregistrată", Mark.expiry_date >= _grace_start(), Mark.expiry_date < today)
    elif status == "Înregistrată":
        q = q.filter(Mark.status == "Înregistrată", or_(Mark.expiry_date == "", Mark.expiry_date >= today), alive)
    elif status == "Încetată":
        q = q.filter(Mark.status == "Înregistrată", ended)
    elif status in ("În opoziție", "În contestație", "În examinare"):
        kw = {"În opoziție": "%ppos%", "În contestație": "%ppeal%", "În examinare": "%xamin%"}[status]
        q = q.filter(Mark.status.in_(["Cerere publicată", "Admisă la înregistrare"]), Mark.tm_status.ilike(kw))
    elif status in ("Retrasă", "Respinsă"):
        kw = "%ithdraw%" if status == "Retrasă" else "%efus%"
        q = q.filter(or_(Mark.status == status, Mark.status.in_(["Cerere publicată", "Admisă la înregistrare"])
                         & Mark.tm_status.ilike(kw)))
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


def _by_deposit(db, key: str):
    """Marca reînnoită veche („R…”) care are acest nr. de depozit în istoricul ei (tabelele BOPI / TMview)."""
    from sqlalchemy import Text, cast
    pat = cast(Mark.events, Text)
    return db.query(Mark).filter(Mark.app_number.like("R%"), or_(
        pat.like(f'%"app": "{key}"%'), pat.like(f'%"app":"{key}"%'), pat.like(f'%RO50000{key}"%'))).first()


@app.get("/api/mark/{app_number}")
def mark(app_number: str, db: Session = Depends(get_db)):
    key = app_number.replace(" ", "").upper()
    m = db.get(Mark, key)
    if not m and re.fullmatch(r"M\d{8,10}", key):
        m = _by_deposit(db, key)          # marcă reînnoită veche, păstrată ca „R…” (ex. M 2000 02551 → 2R42921)
        if not m and not ingest._tmview_paused():
            got = ingest.fetch_old(key)       # M 2000 / M 2001 încetată înainte de arhiva BOPI online
            m = db.get(Mark, got) if got else None
    if not m and key.isdigit():
        # Marcă veche (depozit dinainte de 2001), încetată înainte de arhiva BOPI online — din TMview.
        key = key.zfill(6)
        m = db.get(Mark, key)
        if not m and not ingest._tmview_paused():
            got = ingest.fetch_old(key)
            m = db.get(Mark, got) if got else None
    if not m:
        raise HTTPException(404, "Marca nu există în registru.")
    week = (date.today() - timedelta(days=7)).isoformat()
    if (m.tm_status_checked or "") < week or "|gs3" not in (m.tmview_checked or ""):
        ingest.refresh_status(m.app_number)        # starea din TMview, reverificată în fundal
    return _detail(m, db)


@app.get("/api/image/{app_number}")
def image(app_number: str, hq: int = 0, db: Session = Depends(get_db)):
    # Logo-ul original din TMview, când îl avem; altfel cel decupat din BOPI.
    from db import MarkImageHQ
    h = db.get(MarkImageHQ, app_number)
    if h is not None and h.status == "ok" and h.data:
        return Response(h.data, media_type=h.content_type, headers={"Cache-Control": "public, max-age=604800"})
    img = db.get(MarkImage, app_number)
    if not img:
        raise HTTPException(404)
    if hq and h is None:
        ingest.request_logo_hq(app_number)       # fișa mărcii: originalul se aduce în fundal
    # Până vine originalul, imaginea din BOPI nu se păstrează mult în cache-ul browserului.
    return Response(img.data, media_type=img.content_type,
                    headers={"Cache-Control": "public, max-age=" + ("604800" if h is not None else "3600")})


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
    stubs = db.query(func.count(Mark.app_number)).filter(Mark.app_number.like("R%")).scalar() or 0
    named = db.query(func.count(Mark.app_number)).filter(Mark.app_number.like("R%"), Mark.name != "").scalar() or 0
    return {"running": ingest.job["running"], "current": ingest.job["current"], "done": ingest.job["done"],
            "total": ingest.job["total"], "sources": by, "last_bulletin": last,
            "first_bulletin": db.query(func.min(SourceFile.bulletin_date)).filter(SourceFile.status == "ok").scalar(),
            "publications": [db.query(func.min(Mark.publication_date)).filter(Mark.publication_date != "").scalar(),
                             db.query(func.max(Mark.publication_date)).scalar()],
            "renewed_without_original": stubs, "renewed_named_from_tmview": named, "enrich": ingest.enrich_job, "goods": ingest.goods_job, "status_sync": ingest.status_job, "logos_hq": ingest.hq_job, "old_marks": dict(ingest.old_job, skeletons=db.query(func.count(Mark.app_number)).filter(Mark.tmview_checked.like("%|skel%")).scalar() or 0), "courts": courts.job, "tmview_pause_until": ingest.tmview_pause["until"].isoformat() + "Z",
            "errors": [{"bopi": e.label, "error": (e.error or "")[:160]} for e in
                       db.query(SourceFile).filter(SourceFile.status == "error").limit(10)],
            "changes_applied": dict(db.query(MarkChange.kind, func.count(MarkChange.id))
                                    .filter(MarkChange.applied == True).group_by(MarkChange.kind).all()),  # noqa: E712
            "marks": db.query(func.count(Mark.app_number)).scalar() or 0}


@app.get("/api/mail-status")
def mail_status():
    import mailer
    return {"configured": mailer.available(), "provider": mailer.provider(), "sender": mailer._sender(),
            "to": mailer.LEAD_EMAIL, "last": mailer.last}


_BAD_CHARS = re.compile(r"[A-Za-z][|Õõ¹º³þÞãÃ][a-z]|\bÕi\b")


@app.get("/api/quality-chars")
def quality_chars(limit: int = 4000, ch: str = "", db: Session = Depends(get_db)):
    """Contextele caracterelor stricate (pentru tabelul de corespondență a diacriticelor)."""
    from collections import Counter
    if ch:
        out = []
        for m in db.query(Mark).yield_per(2000):
            text = " ".join((m.goods or {}).values()) + " | " + (m.name or "") + " | " + (m.holder or "")
            i = text.find(ch)
            if i >= 0:
                out.append({"app": m.app_number, "ctx": text[max(0, i - 60):i + 60]})
                if len(out) >= 25:
                    break
        return {"ch": ch, "examples": out}
    ctx = Counter()
    n = 0
    for m in db.query(Mark).yield_per(2000):
        text = " ".join((m.goods or {}).values()) + " " + (m.name or "") + " " + (m.holder or "")
        for x in re.finditer(r"\w*[^\x00-\x7FăâîșşțţĂÂÎȘŞȚŢéèäöüßáíóúçñ ,.;:()'\"/&\-–\n\r\t]\w*|\w+\|\w+", text):
            ctx[x.group(0)[:30]] += 1
        n += 1
        if len(ctx) > limit:
            break
    return {"scanned": n, "top": ctx.most_common(300)}


@app.get("/api/coverage")
def coverage(year: int = 0, db: Session = Depends(get_db)):
    """Acoperirea bazei: mărci pe an de depozit (după nr. „M an …”) și ce a dat fiecare buletin citit."""
    if year:
        rows = db.query(SourceFile).filter(SourceFile.bulletin_date.like(f"{year}-%")).order_by(SourceFile.bulletin_date).all()
        return {"year": year, "sources": [{"label": r.label, "kind": r.kind, "date": r.bulletin_date, "status": r.status,
                                           "marks": r.marks, "error": (r.error or "")[:200]} for r in rows]}
    yr = func.substr(Mark.app_number, 2, 4)
    by = dict(db.query(yr, func.count(Mark.app_number)).filter(Mark.app_number.like("M%")).group_by(yr).all())
    return {"marks_by_deposit_year": dict(sorted(by.items()))}


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
        "fara_tip": lambda m: not m.mark_type,
        # Doar unde data publicării cererii e disponibilă (buletinele zilnice, din 2022) — la mărcile din
        # arhivă s-a renunțat (12.11.2026).
        "fara_data_publicare": lambda m: not m.publication_date and (m.filing_date or "") >= "2021-12-01",
        "inregistrata_fara_data_inregistrare": lambda m: m.status == "Înregistrată" and not m.reg_date,
        "inregistrata_fara_expirare": lambda m: m.status == "Înregistrată" and not m.expiry_date,
        "figurativa_fara_logo": lambda m: m.mark_type in ("Figurativă", "Combinată") and not m.has_image,
        "fara_stare": lambda m: not m.status,
        "fara_nr_depozit": lambda m: not _deposit(m),
        # Diacritice stricate din fonturile vechi ale PDF-urilor BOPI („Õi”, „b|uturi”).
        "diacritice_stricate": lambda m: bool(_BAD_CHARS.search(" ".join((m.goods or {}).values()) + " " + (m.name or "") + " " + (m.holder or ""))),
    }
    # Aceleași verificări doar pe mărcile cu fișă BOPI (fără mărcile reînnoite vechi „R<nr.>”, care
    # n-au prin natura lor data depozitului, adresa titularului sau produsele) — „<verificare>_M”.
    for k, f in list(checks.items()):
        checks[k + "_M"] = (lambda f: lambda m: not m.app_number.startswith("R") and f(m))(f)
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
