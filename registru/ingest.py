"""
Construirea și actualizarea bazei de mărci din buletinele BOPI publicate pe osim.ro.

Sursele (2022 →): buletinele zilnice (cereri publicate), listele „cereri admise” (2023 →) și
buletinele lunare (cap. 1 înregistrate, cap. 2 respinse). Se procesează în ordine
cronologică, câte un PDF, iar fiecare PDF terminat e notat în `source_files`, ca o repornire
să continue de unde a rămas. Rulează într-un fir de fundal al serverului.
"""
import os
import re
import tempfile
import threading
import traceback
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import requests

from bopi_parser import parse_changes, parse_pdf
from db import Mark, MarkChange, MarkImage, SessionLocal, SourceFile, norm

OSIM = "https://www.osim.ro"
INDEX_PAGE = OSIM + "/buletinul-oficial-de-proprietate-industriala-marci"
DAILY_URL = OSIM + "/images/Publicatii/Marci/{y}/bopi{d:%d%m%Y}.pdf"
START = date(2022, 1, 1)
UA = {"User-Agent": "Mozilla/5.0 (registru marci; contact: protectmark)"}

STATUS = {
    "published":  "Cerere publicată",
    "admitted":   "Admisă la înregistrare",
    "registered": "Înregistrată",
    "rejected":   "Respinsă",
}
_RANK = {"Cerere publicată": 1, "Admisă la înregistrare": 2, "Respinsă": 3, "Înregistrată": 4}
CHAPTER_LABEL = {
    "published":  "Cereri de marcă publicate",
    "admitted":   "Cereri de marcă admise la înregistrare",
    "registered": "Mărci înregistrate",
    "rejected":   "Cereri de marcă respinse",
}

job: Dict = {"running": False, "current": "", "done": 0, "total": 0, "marks": 0,
             "started_at": None, "finished_at": None, "error": ""}
_lock = threading.Lock()


# ── Lista surselor ──────────────────────────────────────────────────────────────

def _month_end(y: int, m: int) -> date:
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))


def list_sources(today: Optional[date] = None) -> List[Dict]:
    today = today or date.today()
    out: List[Dict] = []
    html = requests.get(INDEX_PAGE, headers=UA, timeout=60).text
    for href in sorted(set(re.findall(r'href="([^"]+\.pdf)"', html))):
        url = href if href.startswith("http") else OSIM + href
        name = href.rsplit("/", 1)[-1].lower()
        m = re.match(r"cereri_admise_(\d{2})(\d{2})(\d{4})\.pdf$", name)
        if m:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if d >= START:
                out.append({"url": url, "kind": "admise", "date": d,
                            "label": f"Cereri admise {d:%d.%m.%Y}"})
            continue
        m = re.match(r"mrc_(\d{1,2})[-_](?:partea-(\w+)_)?(\d{4})\.pdf$", name)
        if m and int(m.group(3)) >= START.year:
            y, mo = int(m.group(3)), int(m.group(1))
            part = f" partea {m.group(2).upper()}" if m.group(2) else ""
            out.append({"url": url, "kind": "monthly", "date": _month_end(y, mo),
                        "label": f"BOPI {mo:02d}/{y}{part}"})
            # Tabelele aceluiași buletin (transferuri, reînnoiri, radieri...) — pas separat.
            out.append({"url": url + "#changes", "kind": "changes", "date": _month_end(y, mo),
                        "label": f"BOPI {mo:02d}/{y}{part}"})
    d = START
    while d <= today:
        if d.weekday() < 5:
            out.append({"url": DAILY_URL.format(y=d.year, d=d), "kind": "daily", "date": d,
                        "label": f"BOPI {d:%d.%m.%Y}"})
        d += timedelta(days=1)
    # Aceeași zi: întâi publicarea, apoi admiterea, apoi buletinul lunar.
    order = {"daily": 0, "admise": 1, "monthly": 2, "changes": 3}
    out.sort(key=lambda s: (s["date"], order[s["kind"]], s["url"]))
    return out


# ── Salvarea unei mărci ─────────────────────────────────────────────────────────

def _add_years(iso: str, years: int) -> str:
    try:
        d = date.fromisoformat(iso)
        try:
            return d.replace(year=d.year + years).isoformat()
        except ValueError:
            return d.replace(year=d.year + years, day=28).isoformat()
    except ValueError:
        return ""


def _upsert(db, chapter: str, rec: Dict, src: Dict) -> None:
    app = rec["app_number"]
    m = db.get(Mark, app)
    if m is None:
        m = Mark(app_number=app, events=[], classes=[], goods={})
        db.add(m)
        db.flush()
    bdate = src["date"].isoformat()
    # (151) apare la toate tipurile de buletin și e data de depozit a cererii.
    if rec.get("date_151") and not m.filing_date:
        m.filing_date = rec["date_151"]
    if rec.get("filing_date"):
        m.filing_date = rec["filing_date"]
    for f in ("name", "holder", "holder_address", "representative", "representative_address",
              "vienna", "colors", "disclaimer"):
        if rec.get(f):
            setattr(m, f, rec[f])
    if rec.get("goods"):
        m.goods = rec["goods"]
        m.classes = rec["classes"]
    m.name_norm = norm(m.name)
    m.holder_norm = norm(m.holder)
    m.rep_norm = norm(m.representative)
    if chapter == "published" and not m.publication_date:
        m.publication_date = bdate
    if chapter == "registered":
        m.reg_number = rec.get("reg_number") or m.reg_number
        m.reg_date = bdate
    new_status = STATUS.get(chapter, "")
    if new_status and (_RANK.get(new_status, 0) >= _RANK.get(m.status, 0) or chapter == "rejected"):
        if not (m.status == "Înregistrată" and chapter != "registered"):
            m.status, m.status_date = new_status, bdate
    if rec.get("image") and not m.has_image:
        db.merge(MarkImage(app_number=app, data=rec["image"], content_type="image/webp"))
        m.has_image = True
    m.mark_type = ("Combinată" if m.has_image and m.name else
                   "Figurativă" if m.has_image else "Verbală")
    ev = {"date": bdate, "bopi": src["label"], "chapter": CHAPTER_LABEL.get(chapter, chapter)}
    if ev not in (m.events or []):
        m.events = sorted((m.events or []) + [ev], key=lambda e: e["date"])
    m.updated_at = datetime.utcnow()


CHANGE_LABEL = {
    "renewal": "Reînnoire", "representation": "Modificarea reprezentării mărcii", "license": "Licență înscrisă",
    "limitation": "Limitare la cererea titularului", "radiated": "Radiată la cererea titularului",
    "withdrawn": "Cerere retrasă",
}


def _apply_change(db, m: Mark, c: MarkChange) -> None:
    label = CHANGE_LABEL.get(c.kind, c.kind)
    detail = ""
    if c.kind in ("Cesiune", "Fuziune", "Modificare titular", "Modificare adresă titular") and c.new_value:
        m.holder_address = c.new_value
        m.holder = c.new_value.split(",")[0].strip()
        m.holder_norm = norm(m.holder)
        detail = f"titular nou: {m.holder}"
    elif c.kind == "representation" and c.new_value:
        detail = f"„{c.old_value}” → „{c.new_value}”"
        if norm(c.new_value) != norm(m.name):
            m.name, m.name_norm = c.new_value, norm(c.new_value)
    elif c.kind == "renewal":
        detail = f"valabilă până la {c.expiry[8:10]}.{c.expiry[5:7]}.{c.expiry[:4]}" if c.expiry else ""
    elif c.kind == "license":
        detail = c.new_value[:200]
    elif c.kind in ("radiated", "withdrawn"):
        m.status, m.status_date = ("Radiată" if c.kind == "radiated" else "Retrasă"), c.bulletin_date
    ev = {"date": c.bulletin_date, "bopi": c.label, "chapter": label}
    if detail:
        ev["detail"] = detail
    if c.kind == "renewal" and c.expiry:
        ev["expiry"] = c.expiry
    if ev not in (m.events or []):
        m.events = sorted((m.events or []) + [ev], key=lambda e: e["date"])
    m.updated_at = datetime.utcnow()
    c.applied = True


def _process_changes(db, path: str, src: Dict) -> int:
    n = 0
    bdate = src["date"].isoformat()
    for r in parse_changes(path):
        c = MarkChange(kind=r["kind"], app_number=r.get("app_number", ""), reg_number=r.get("reg_number", ""),
                       old_value=r.get("old", ""), new_value=r.get("new", ""), expiry=r.get("expiry", ""),
                       title=r.get("title", ""), bulletin_date=bdate, label=src["label"])
        db.add(c)
        m = db.get(Mark, c.app_number) if c.app_number else None
        if m is None and c.reg_number:
            m = db.query(Mark).filter(Mark.reg_number == c.reg_number).first()
        if m is not None:
            _apply_change(db, m, c)
        n += 1
        if n % 200 == 0:
            db.commit()
    return n


def expiry_of(m: Mark) -> str:
    """Protecția durează 10 ani de la data depozitului (Legea 84/1998, art. 39); o reînnoire
    publicată în BOPI o prelungește."""
    renewals = [e["expiry"] for e in (m.events or []) if e.get("expiry")]
    if renewals:
        return max(renewals)
    if m.status != "Înregistrată" or not m.filing_date:
        return ""
    exp = _add_years(m.filing_date, 10)
    # O dată de expirare anterioară înregistrării înseamnă că nu știm istoricul (reînnoiri etc.).
    return exp if not m.reg_date or exp > m.reg_date else ""


# ── Procesarea unui PDF ─────────────────────────────────────────────────────────

def _download(url: str) -> Optional[str]:
    url = url.split("#", 1)[0]
    r = requests.get(url, headers=UA, timeout=600, stream=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(fd, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    with open(path, "rb") as f:
        if f.read(5) != b"%PDF-":
            os.remove(path)
            return None
    return path


def process_source(src: Dict) -> int:
    path = _download(src["url"])
    db = SessionLocal()
    try:
        if not path:
            # Zilele fără buletin (sărbători) rămân „missing”; cele recente se reîncearcă.
            if src["date"] < date.today() - timedelta(days=7):
                db.merge(SourceFile(url=src["url"], kind=src["kind"], label=src["label"],
                                    bulletin_date=src["date"].isoformat(), status="missing",
                                    processed_at=datetime.utcnow()))
                db.commit()
            return 0
        n = 0
        if src["kind"] == "changes":
            n = _process_changes(db, path, src)
        else:
            for chapter, rec in parse_pdf(path, src["kind"]):
                _upsert(db, chapter, rec, src)
                n += 1
                if n % 200 == 0:
                    db.commit()
        db.merge(SourceFile(url=src["url"], kind=src["kind"], label=src["label"],
                            bulletin_date=src["date"].isoformat(), status="ok", marks=n,
                            processed_at=datetime.utcnow()))
        db.commit()
        return n
    finally:
        db.close()
        if path and os.path.exists(path):
            os.remove(path)


def run_ingest(limit: Optional[int] = None) -> None:
    """Procesează toate sursele încă neprocesate (în ordine cronologică)."""
    if not _lock.acquire(blocking=False):
        return
    try:
        job.update(running=True, error="", done=0, marks=0, started_at=datetime.utcnow().isoformat() + "Z",
                   finished_at=None)
        db = SessionLocal()
        try:
            done = {u for (u,) in db.query(SourceFile.url).filter(SourceFile.status.in_(("ok", "missing")))}
        finally:
            db.close()
        todo = [s for s in list_sources() if s["url"] not in done]
        if limit:
            todo = todo[:limit]
        job["total"] = len(todo)
        for s in todo:
            job["current"] = s["label"]
            try:
                job["marks"] += process_source(s)
            except Exception as e:
                traceback.print_exc()
                db = SessionLocal()
                try:
                    db.merge(SourceFile(url=s["url"], kind=s["kind"], label=s["label"],
                                        bulletin_date=s["date"].isoformat(), status="error",
                                        error=str(e)[:500], processed_at=datetime.utcnow()))
                    db.commit()
                finally:
                    db.close()
            job["done"] += 1
    except Exception as e:
        traceback.print_exc()
        job["error"] = str(e)
    finally:
        job.update(running=False, current="", finished_at=datetime.utcnow().isoformat() + "Z")
        _lock.release()


def start_background(limit: Optional[int] = None) -> bool:
    if job["running"]:
        return False
    threading.Thread(target=run_ingest, kwargs={"limit": limit}, daemon=True).start()
    return True
