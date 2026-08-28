"""
Monitoring router — watch item CRUD, Excel import/template, manual run, history.
"""
import io
import os
import re
from datetime import datetime
from datetime import date as dt_date
from typing import List, Optional, Dict

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from monitor_models import WatchItem, SeenTrademark, AlertLog
from monitor_service import run_watch_item
from paths import DATA_DIR

router = APIRouter(prefix="/api/monitor", tags=["monitor"])

# ── Pydantic schemas ──────────────────────────────────────────────────────────

class WatchItemCreate(BaseModel):
    trademark_name:     str
    holder_name:        str  = ""
    nice_classes:       List[str] = []
    offices:            List[str] = ["RO", "EM"]
    notification_email: str
    frequency:          str  = "weekly"   # daily / weekly / monthly
    application_number: str  = ""
    registration_number: str = ""
    filing_date:        str  = ""


class WatchItemOut(BaseModel):
    id:                 int
    trademark_name:     str
    holder_name:        str
    nice_classes:       List[str]
    offices:            List[str]
    notification_email: str
    frequency:          str
    reference_image:    Optional[str] = None
    application_number: Optional[str] = None
    registration_number: Optional[str] = None
    filing_date:        Optional[str] = None
    active:             bool
    created_at:         datetime
    last_checked_at:    Optional[datetime]

    class Config:
        from_attributes = True


class AlertLogOut(BaseModel):
    id:            int
    sent_at:       datetime
    num_new_marks: int
    email_to:      str
    status:        str
    error_msg:     str

    class Config:
        from_attributes = True


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("/watches", response_model=List[WatchItemOut])
def list_watches(db: Session = Depends(get_db)):
    return db.query(WatchItem).order_by(WatchItem.created_at.desc()).all()


@router.post("/watch", response_model=WatchItemOut)
def create_watch(body: WatchItemCreate, db: Session = Depends(get_db)):
    if not body.trademark_name.strip():
        raise HTTPException(400, "Denumirea mărcii este obligatorie.")
    if not body.notification_email.strip():
        raise HTTPException(400, "Email-ul de notificare este obligatoriu.")
    item = WatchItem(**body.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/watch/{item_id}/toggle", response_model=WatchItemOut)
def toggle_watch(item_id: int, db: Session = Depends(get_db)):
    item = db.get(WatchItem, item_id)
    if not item:
        raise HTTPException(404, "Watch item negăsit.")
    item.active = not item.active
    db.commit()
    db.refresh(item)
    return item


@router.delete("/watch/{item_id}")
def delete_watch(item_id: int, db: Session = Depends(get_db)):
    item = db.get(WatchItem, item_id)
    if not item:
        raise HTTPException(404, "Watch item negăsit.")
    db.query(SeenTrademark).filter(SeenTrademark.watch_item_id == item_id).delete()
    db.query(AlertLog).filter(AlertLog.watch_item_id == item_id).delete()
    db.delete(item)
    db.commit()
    return {"status": "deleted"}


@router.get("/history/{item_id}", response_model=List[AlertLogOut])
def get_history(item_id: int, db: Session = Depends(get_db)):
    item = db.get(WatchItem, item_id)
    if not item:
        raise HTTPException(404, "Watch item negăsit.")
    return (
        db.query(AlertLog)
        .filter(AlertLog.watch_item_id == item_id)
        .order_by(AlertLog.sent_at.desc())
        .limit(50)
        .all()
    )


# ── Mărci găsite (cumulativ, nu doar cele noi din ultima rulare) ──────────────

@router.get("/watch/{item_id}/found-marks")
def get_found_marks(item_id: int, db: Session = Depends(get_db)):
    """Toate mărcile detectate vreodată pentru acest watch item (SeenTrademark
    se acumulează permanent — o rulare nouă doar adaugă, nu șterge nimic)."""
    item = db.get(WatchItem, item_id)
    if not item:
        raise HTTPException(404, "Watch item negăsit.")
    rows = (
        db.query(SeenTrademark)
        .filter(SeenTrademark.watch_item_id == item_id)
        .order_by(SeenTrademark.first_seen_at.desc())
        .all()
    )
    return [
        {
            "st13": r.st13,
            "tm_name": r.tm_name,
            "tm_office": r.tm_office,
            "similarity_level": r.similarity_level,
            "application_date": r.application_date,
            "first_seen_at": r.first_seen_at,
        }
        for r in rows
    ]


# ── Manual run ────────────────────────────────────────────────────────────────

@router.post("/watch/{item_id}/run")
async def manual_run(item_id: int, db: Session = Depends(get_db)):
    item = db.get(WatchItem, item_id)
    if not item:
        raise HTTPException(404, "Watch item negăsit.")
    result = await run_watch_item(item, db)
    return result


# ── Rulare monitorizare pentru toată lista (ex: după descărcarea buletinelor) ──

_run_all_progress: dict = {
    "running": False, "done": 0, "total": 0,
    "results": [], "started_at": None, "finished_at": None,
}


@router.post("/run-all")
async def run_all_watches():
    """Pornește în fundal verificarea tuturor mărcilor active din listă,
    folosind sursele deja disponibile (TMview/EUIPO API + buletinele OSIM/EUIPO
    deja descărcate în cache — dacă au fost descărcate recent, nu se redescarcă)."""
    import asyncio
    from db import SessionLocal

    if _run_all_progress["running"]:
        return {"status": "already_running", **_run_all_progress}

    db = SessionLocal()
    try:
        items = db.query(WatchItem).filter(WatchItem.active == True).all()  # noqa: E712
        item_ids = [i.id for i in items]
    finally:
        db.close()

    if not item_ids:
        return {"status": "no_items", "message": "Nu există mărci active în listă."}

    _run_all_progress.update({
        "running": True, "done": 0, "total": len(item_ids), "results": [],
        "started_at": datetime.utcnow().isoformat(), "finished_at": None,
    })

    async def _bg_run_all(ids=item_ids):
        for wid in ids:
            bg_db = SessionLocal()
            try:
                item = bg_db.get(WatchItem, wid)
                if item and item.active:
                    try:
                        result = await run_watch_item(item, bg_db)
                        _run_all_progress["results"].append(result)
                    except Exception as e:
                        _run_all_progress["results"].append({
                            "watch_item_id": wid, "error": str(e),
                        })
            finally:
                bg_db.close()
                _run_all_progress["done"] += 1
        _run_all_progress["running"] = False
        _run_all_progress["finished_at"] = datetime.utcnow().isoformat()

    asyncio.ensure_future(_bg_run_all())
    return {"status": "started", "total": len(item_ids)}


@router.get("/run-all-status")
def run_all_status():
    return _run_all_progress


# ── Excel template ────────────────────────────────────────────────────────────

@router.get("/template")
def download_template():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Mărci de monitorizat"

    # Ordinea coloanelor urmează codul INID (WIPO ST.60) numeric ascendent, pentru
    # câmpurile care au un cod corespunzător: (511) Clase NICE, (540) Denumire Marcă
    # + Imagine (aceeași reprezentare a mărcii, text și grafic), (731) Titular —
    # apoi câmpurile specifice aplicației, fără cod INID.
    headers = [
        "(511) Clase NICE (separate prin virgulă)*",
        "(540) Denumire Marcă*",
        "(540) Imagine (opțional — logo de referință)",
        "(731) Titular",
        "Teritorii (separate prin virgulă)*",
        "Email notificare*",
        "Frecvență (daily/weekly/monthly)",
        "(210) Nr. Depozit (opțional)",
        "(111) Nr. Înregistrare (opțional)",
        "(220) Data depunerii (opțional)",
    ]

    header_fill   = PatternFill("solid", fgColor="1A3C5E")
    header_font   = Font(bold=True, color="FFFFFF", size=11)
    thin_border   = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )
    col_widths = [35, 30, 30, 30, 30, 35, 30, 24, 24, 24]

    for col_idx, (header, width) in enumerate(zip(headers, col_widths), start=1):
        cell            = ws.cell(row=1, column=col_idx, value=header)
        cell.font       = header_font
        cell.fill       = header_fill
        cell.alignment  = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border     = thin_border
        ws.column_dimensions[cell.column_letter].width = width

    ws.row_dimensions[1].height = 36
    ws.row_dimensions[2].height = 60   # loc pentru o imagine mică în celula exemplu

    # Example row
    example = ["35, 42", "ACME", "", "ACME România SRL", "RO, EM", "office@firma.ro", "weekly",
               "019301780", "", "2025-03-12"]
    example_fill = PatternFill("solid", fgColor="EBF5FB")
    for col_idx, val in enumerate(example, start=1):
        cell           = ws.cell(row=2, column=col_idx, value=val)
        cell.fill      = example_fill
        cell.border    = thin_border
        cell.alignment = Alignment(vertical="center")

    # Note row
    ws.cell(row=3, column=1, value="* câmpuri obligatorii")
    ws.cell(row=3, column=1).font = Font(italic=True, color="888888")
    ws.cell(row=4, column=1, value="Teritorii acceptate: RO, EM, EU, DE, FR, IT, ES, UK, US, WO (sau orice cod de țară din TMview)")
    ws.cell(row=4, column=1).font = Font(italic=True, color="888888")
    ws.merge_cells("A4:J4")
    ws.cell(row=5, column=1,
            value='Imagine: inserați logo-ul direct în celulă (Excel: Insert → Pictures → Place in Cell), în dreptul mărcii — folosit pentru comparație vizuală cu mărcile din buletine.')
    ws.cell(row=5, column=1).font = Font(italic=True, color="888888")
    ws.merge_cells("A5:J5")
    ws.cell(row=6, column=1,
            value='Nr. Depozit / Nr. Înregistrare / Data depunerii: opționale, dar recomandate — dacă două rânduri au aceeași denumire, aceleași clase și aceeași imagine, sunt considerate aceeași marcă și nu se importă de două ori, ÎN AFARĂ de cazul în care au numere de depozit/înregistrare diferite (atunci sunt tratate ca mărci distincte, chiar dacă arată identic).')
    ws.cell(row=6, column=1).font = Font(italic=True, color="888888")
    ws.merge_cells("A6:J6")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="template_monitorizare_marci.xlsx"'},
    )


# ── Excel import ──────────────────────────────────────────────────────────────

def _parse_classes(raw: str) -> List[str]:
    return [c.strip() for c in re.split(r"[,;\s]+", str(raw)) if c.strip().isdigit()]


def _parse_offices(raw: str) -> List[str]:
    return [o.strip().upper() for o in re.split(r"[,;\s]+", str(raw)) if o.strip()]


def _norm_watch_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().upper())


def _parse_filing_date(raw) -> str:
    """Normalizează o dată de depunere (celulă Excel — poate fi datetime, sau text
    în diverse formate) la "YYYY-MM-DD". Întoarce "" dacă lipsește sau nu poate fi
    recunoscută (o păstrăm ca text brut în acel caz, nu aruncăm eroare — e un câmp
    opțional, folosit doar pentru diferențiere, nu validat strict)."""
    if raw is None or raw == "":
        return ""
    if isinstance(raw, (datetime, dt_date)):
        return raw.strftime("%Y-%m-%d")
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def _watch_identity_key(name: str, classes, image_hash: Optional[str]) -> tuple:
    """(511)+(540 verbal)+(540 imagine) — cele trei criterii de identitate cerute:
    aceeași denumire verbală, aceleași clase NICE, aceeași imagine (hash pe conținut,
    nu pe numele fișierului — două fișiere diferite cu exact același logo trebuie
    să dea același hash)."""
    return (_norm_watch_name(name), frozenset(str(c) for c in (classes or [])), image_hash)


def _is_duplicate_watch(existing: dict, candidate: dict) -> bool:
    """True doar dacă identitatea (nume+clase+imagine) coincide ȘI numerele de
    depozit/înregistrare (când sunt disponibile pe ambele) nu se contrazic — dacă
    ambele au un număr și acesta diferă, sunt mărci distincte, chiar dacă arată
    identic (ex. aceeași denumire refiled ulterior cu un nr. de depozit nou).
    Dacă niciuna dintre mărci nu are nici nr. de depozit, nici de înregistrare,
    folosim data depunerii ca ultimă diferențiere disponibilă."""
    if _watch_identity_key(existing["name"], existing["classes"], existing["image_hash"]) != \
       _watch_identity_key(candidate["name"], candidate["classes"], candidate["image_hash"]):
        return False

    e_app, c_app = (existing.get("application_number") or "").strip(), (candidate.get("application_number") or "").strip()
    if e_app and c_app and e_app != c_app:
        return False

    e_reg, c_reg = (existing.get("registration_number") or "").strip(), (candidate.get("registration_number") or "").strip()
    if e_reg and c_reg and e_reg != c_reg:
        return False

    if not e_app and not c_app and not e_reg and not c_reg:
        e_date, c_date = (existing.get("filing_date") or "").strip(), (candidate.get("filing_date") or "").strip()
        if e_date and c_date and e_date != c_date:
            return False

    return True


def _parse_frequency(raw: str) -> str:
    val = str(raw).strip().lower()
    return val if val in ("daily", "weekly", "monthly") else "weekly"


@router.get("/bulletin-status")
def bulletin_status():
    """Returnează lista tuturor buletinelor descărcate (OSIM + EUIPO)."""
    from scrapers.osim_bulletin  import list_processed as osim_list
    from scrapers.euipo_bulletin import list_processed as euipo_list
    return {
        "osim":  osim_list(),
        "euipo": euipo_list(),
    }


def _load_bulletin_marks(source: str, date: str) -> List[Dict]:
    """Încarcă mărcile dintr-un buletin deja descărcat (OSIM sau EUIPO), fără
    să pornească vreo descărcare — folosit atât de /bulletin-marks cât și de
    /bulletin-compare."""
    from datetime import date as date_type

    try:
        td = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, f"Dată invalidă: {date}")

    if source == "osim":
        from scrapers.osim_bulletin import _prev_working_day, _date_slug, parse_pdf_cached, CACHE_DIR
        import os
        working = _prev_working_day(td)
        slug    = _date_slug(working)
        pdf     = os.path.join(CACHE_DIR, f"{slug}.pdf")
        if not os.path.exists(pdf):
            raise HTTPException(404, "Buletinul OSIM pentru această dată nu a fost descărcat încă.")
        return parse_pdf_cached(pdf, slug)

    elif source == "euipo":
        from scrapers.euipo_bulletin import is_bulletin_cached, should_run_sync, fetch_euipo_for_date
        # Nu descărcăm niciodată aici — un PDF nedescărcat poate lua minute și
        # bloca acest request. Cere apelantului să pornească /bulletin-fetch
        # întâi (are logica async + cooldown); aici doar citim ce e deja gata.
        if not should_run_sync(td):
            raise HTTPException(425, "Buletinul EUIPO încă nu e disponibil pentru această dată — "
                                      "porniți întâi POST /api/monitor/bulletin-fetch și așteptați finalizarea.")
        skip_pdf = not is_bulletin_cached(td)
        marks, info = fetch_euipo_for_date(td, skip_pdf)
        if not marks and info.get("status") not in ("ok_api", "ok_bulletin"):
            raise HTTPException(404, info.get("error") or "Buletinul EUIPO pentru această dată nu a fost descărcat încă.")
        return marks

    else:
        raise HTTPException(400, "source trebuie să fie 'osim' sau 'euipo'")


@router.get("/bulletin-marks")
def get_bulletin_marks(source: str, date: str):
    """
    Returnează mărcile dintr-un buletin descărcat anterior.
    source: 'osim' | 'euipo'
    date: 'YYYY-MM-DD'
    """
    marks = _load_bulletin_marks(source, date)
    return {"source": source, "date": date, "total": len(marks), "marks": marks}


def _compute_bulletin_compare(source: str, date: str) -> Dict:
    """
    Compară fiecare marcă din buletinul descărcat (OSIM/EUIPO) cu toată lista
    de mărci monitorizate, folosind exact algoritmul de similaritate din
    monitorizare (SimilarityAgent), și întoarce perechile potrivite
    (marcă din buletin ↔ marcă monitorizată), sortate după nivel de risc și
    procent de asemănare. CPU-bound (rapidfuzz) — poate lua zeci de secunde
    pentru buletine mari (EUIPO are ~1500 mărci/zi față de ~40 la OSIM), de
    aceea rulează ca job de fundal, nu direct în request.

    Filtrare: o pereche apare doar dacă are AND (nu OR) — clase NICE comune
    ȘI similaritate de nume ≥ prag mediu (risc medium/high/very_high). Doar
    numele asemănător sau doar clasa comună, fără celălalt criteriu, produce
    prea multe potriviri irelevante (ex. nume complet diferite dar cu scor
    ridicat din cauza unui cuvânt comun scurt, sau mărci din domenii total
    diferite care întâmplător au aceeași clasă NICE).
    """
    from monitor_service import _similarity
    from db import SessionLocal

    marks = _load_bulletin_marks(source, date)

    db = SessionLocal()
    try:
        relevant_codes = ("RO", "EU") if source == "osim" else ("EM", "EU")
        items = db.query(WatchItem).filter(WatchItem.active == True).all()  # noqa: E712
        items = [it for it in items if any((o or "").upper() in relevant_codes for o in (it.offices or []))]

        rows = []
        for item in items:
            classes = item.nice_classes or []
            watch_classes = {str(c) for c in classes}
            analysis = _similarity.analyze(item.trademark_name, marks, classes, user_offices=item.offices)
            for entry in analysis["conflicts"] + analysis["similar"]:
                reps = entry.get("representatives") or []
                rep_str = ", ".join(r.get("name", "") for r in reps if r.get("name")) or entry.get("representative", "") or ""
                bulletin_classes = entry.get("niceClass") or []
                class_overlap = bool(watch_classes & set(bulletin_classes))
                if not class_overlap or entry["risk_level"] == "low":
                    continue
                rows.append({
                    "bulletin_app_number": entry.get("applicationNumber", ""),
                    "bulletin_name":       entry.get("tmName", ""),
                    "bulletin_holder":     ", ".join(entry.get("applicantName") or []),
                    "bulletin_representative": rep_str,
                    "bulletin_classes":    bulletin_classes,
                    "bulletin_image":      entry.get("markImageURI"),
                    "watch_item_id":       item.id,
                    "watch_item_name":     item.trademark_name,
                    "watch_item_classes":  classes,
                    "watch_item_holder":   item.holder_name or "",
                    "similarity_percent":  round(entry["similarity"]["combined_score"]),
                    "risk_level":          entry["risk_level"],
                    "class_overlap":       class_overlap,
                })
    finally:
        db.close()

    _risk_ord = {"very_high": 0, "high": 1, "medium": 2, "low": 3}
    rows.sort(key=lambda r: (
        _risk_ord.get(r["risk_level"], 4),
        0 if r["class_overlap"] else 1,
        -r["similarity_percent"],
    ))

    return {
        "source": source, "date": date,
        "total_bulletin_marks": len(marks),
        "total_matches": len(rows),
        "rows": rows,
    }


_compare_jobs: dict = {}   # "source:date" -> {"running": bool, "result": dict|None, "error": str|None}


@router.post("/bulletin-compare/start")
async def start_bulletin_compare(source: str, date: str):
    import asyncio

    key = f"{source}:{date}"
    job = _compare_jobs.get(key)
    if job and job.get("running"):
        return {"status": "already_running"}
    if job and job.get("result") is not None:
        return {"status": "done", **job["result"]}

    _compare_jobs[key] = {"running": True, "result": None, "error": None}

    loop = asyncio.get_event_loop()

    async def _bg():
        try:
            result = await loop.run_in_executor(None, _compute_bulletin_compare, source, date)
            _compare_jobs[key] = {"running": False, "result": result, "error": None}
        except HTTPException as e:
            _compare_jobs[key] = {"running": False, "result": None, "error": e.detail}
        except Exception as e:
            _compare_jobs[key] = {"running": False, "result": None, "error": str(e)}

    asyncio.ensure_future(_bg())
    return {"status": "started"}


@router.get("/bulletin-compare/status")
def bulletin_compare_status(source: str, date: str):
    key = f"{source}:{date}"
    job = _compare_jobs.get(key)
    if not job:
        return {"status": "not_started"}
    if job.get("running"):
        return {"status": "running"}
    if job.get("error"):
        return {"status": "error", "error": job["error"]}
    return {"status": "done", **job["result"]}


@router.get("/watch-image/{filename}")
def get_watch_image(filename: str):
    """Servește logo-ul de referință al unui watch item, inserat la import Excel."""
    from fastapi.responses import FileResponse
    safe = re.sub(r'[^a-fA-F0-9]', '', filename.rsplit(".", 1)[0]) + ".png"
    path = os.path.join(WATCH_IMAGE_DIR, safe)
    if not os.path.exists(path):
        raise HTTPException(404, "Imaginea nu a fost găsită.")
    return FileResponse(path, media_type="image/png")


@router.get("/bulletin-image")
def get_bulletin_image(source: str, app_num: str, slug: Optional[str] = None):
    """Servește imaginea unei mărci figurative.
    - osim: extrasă local din PDF-ul buletinului la parsare (necesită slug).
    - euipo: preluată live prin API-ul EUIPO autentificat (Bearer + X-IBM-Client-Id) —
      NU prin URL-ul public TMview, care nu are încă imaginea pentru mărci proaspăt
      depuse (întoarce un placeholder identic pentru toate, 1458 bytes)."""
    from fastapi.responses import FileResponse, Response

    if source == "osim":
        if not slug:
            raise HTTPException(400, "Parametrul 'slug' e obligatoriu pentru sursa 'osim'.")
        from scrapers.osim_bulletin import get_bulletin_image_path
        path = get_bulletin_image_path(slug, app_num)
        if not path:
            raise HTTPException(404, "Imaginea nu a fost găsită (posibil marcă doar textuală, sau buletinul nu a fost încă parsat).")
        return FileResponse(path, media_type="image/png")

    if source == "euipo":
        import requests
        from agents.euipo_agent import EUIPO_SEARCH_URL, EUIPO_CLIENT_ID, euipo_available, _get_access_token
        if not euipo_available():
            raise HTTPException(503, "EUIPO API neconfigurat.")
        try:
            token = _get_access_token()
            r = requests.get(
                f"{EUIPO_SEARCH_URL}/{app_num}/image/thumbnail",
                headers={"Authorization": f"Bearer {token}", "X-IBM-Client-Id": EUIPO_CLIENT_ID},
                timeout=15,
            )
        except Exception as e:
            raise HTTPException(502, f"Eroare la preluarea imaginii EUIPO: {e}")
        if r.status_code != 200:
            raise HTTPException(404 if r.status_code == 404 else 502,
                                 f"EUIPO nu a returnat imaginea (status {r.status_code}).")
        return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))

    raise HTTPException(400, "source trebuie să fie 'osim' sau 'euipo'")


@router.post("/bulletin-upload")
async def upload_bulletin_pdf(bulletin_id: str, file: UploadFile = File(...)):
    """
    Încarcă manual un PDF de buletin EUIPO deja descărcat (ex. prin browser, când
    descărcarea directă de pe server eșuează din cauza instabilității conexiunii pe
    fișiere mari). bulletin_id: formatul "YYYY/NNN" (ex. "2026/158").
    Salvează în cache-ul local, exact unde l-ar fi pus fetch_euipo_for_date() —
    următoarea cerere pentru acea dată îl folosește direct, fără să mai descarce.
    """
    import os
    from scrapers.euipo_bulletin import CACHE_DIR, _fetch_bulletin_list, _date_slug

    m = re.match(r'^(\d{4})/(\d+)$', bulletin_id.strip())
    if not m:
        raise HTTPException(400, 'bulletin_id trebuie în formatul "YYYY/NNN", ex. "2026/158"')
    year = int(m.group(1))

    bulletins = _fetch_bulletin_list(year)
    match = next((b for b in bulletins if b["id"] == bulletin_id), None)
    if not match:
        raise HTTPException(404, f"Buletinul {bulletin_id} nu a fost găsit în lista oficială EUIPO pentru {year}.")

    content = await file.read()
    if not content or content[:5] != b"%PDF-" or b"%%EOF" not in content[-2048:]:
        raise HTTPException(400, "Fișierul nu pare un PDF complet și valid (lipsește marcajul %%EOF).")

    slug  = _date_slug(match["date"])
    local = os.path.join(CACHE_DIR, f"{slug}.pdf")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(local, "wb") as f:
        f.write(content)

    return {"status": "saved", "bulletin_id": bulletin_id, "bulletin_date": match["date"].isoformat(),
            "slug": slug, "size_kb": len(content) // 1024}


@router.post("/bulletin-fetch")
async def trigger_bulletin_fetch(
    source: str = "both",          # "osim" | "euipo" | "both"
    target_date: Optional[str] = None,   # ISO date string "YYYY-MM-DD"
):
    """
    Descarcă buletinul pentru o dată specificată (sau cel mai recent dacă lipsește).
    source: "osim" | "euipo" | "both"
    target_date: "YYYY-MM-DD" (opțional, implicit azi)
    """
    import asyncio
    from datetime import date as date_type
    from scrapers.osim_bulletin  import fetch_osim_for_date,  fetch_latest_osim
    from scrapers.euipo_bulletin import fetch_euipo_for_date, fetch_latest_euipo

    if target_date:
        try:
            td = date_type.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(400, f"Dată invalidă: {target_date}. Folosiți formatul YYYY-MM-DD.")
    else:
        td = None

    loop = asyncio.get_event_loop()
    result: dict = {"source": source, "target_date": target_date or "latest"}

    if source in ("osim", "both"):
        if td:
            marks, info = await loop.run_in_executor(None, fetch_osim_for_date, td)
        else:
            marks = await loop.run_in_executor(None, fetch_latest_osim)
            info  = {}
        result["osim"] = {"marks": len(marks), **info}

    if source in ("euipo", "both"):
        from datetime import date as _date_type
        from scrapers.euipo_bulletin import should_run_sync, is_bulletin_cached, is_fetch_in_progress, _in_progress, _date_slug, _prev_working_day
        target_for_check = td or _date_type.today()
        run_sync = should_run_sync(target_for_check) if td else True

        if td and not run_sync:
            # Nu e în cache — descărcarea (15-30 MB, conexiune instabilă) poate
            # depăși limita de 300s a gateway-ului Railway dacă așteptăm sincron.
            # O pornim în fundal și răspundem imediat; clientul verifică progresul
            # prin GET /bulletin-status (reapare acolo cu status "ok_bulletin"
            # când e gata).
            working = _prev_working_day(target_for_check)
            slug    = _date_slug(working)
            if is_fetch_in_progress(target_for_check):
                result["euipo"] = {"status": "processing", "slug": slug,
                                    "message": "Descărcarea e deja în curs — verificați din nou peste ~30s."}
            else:
                _in_progress.add(slug)

                async def _bg_fetch(target=target_for_check, slug=slug):
                    try:
                        await loop.run_in_executor(None, fetch_euipo_for_date, target)
                    finally:
                        _in_progress.discard(slug)

                asyncio.ensure_future(_bg_fetch())
                result["euipo"] = {"status": "processing", "slug": slug,
                                    "message": "Descărcare pornită în fundal — poate dura 1-3 minute. Verificați din nou peste ~30s."}
        else:
            if td:
                # dacă am rulat sincron doar pe motiv de cooldown (nu PDF cache real),
                # sărim peste încercarea de descărcare — mergem direct la API, rapid.
                skip_pdf = not is_bulletin_cached(target_for_check)
                marks, info = await loop.run_in_executor(None, fetch_euipo_for_date, td, skip_pdf)
            else:
                marks = await loop.run_in_executor(None, fetch_latest_euipo)
                info  = {}
            result["euipo"] = {"marks": len(marks), **info}

    return result


WATCH_IMAGE_DIR = (
    os.path.join(DATA_DIR, "watch_images") if DATA_DIR
    else os.path.join(os.path.dirname(__file__), "..", "..", "data", "watch_images")
)


@router.post("/import")
def import_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Fișierul trebuie să fie .xlsx sau .xls")

    from openpyxl import load_workbook
    import hashlib
    import uuid

    content = file.file.read()
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True)
    except Exception as e:
        raise HTTPException(400, f"Fișier Excel invalid: {e}")

    ws = wb.active
    imported   = []
    skipped    = []
    errors     = []
    duplicates = []

    # Imaginile inserate direct în celule (logo de referință, coloana (540) Imagine) —
    # cheiate pe rândul (1-indexat) în care sunt ancorate, ca să le potrivim cu rândul
    # de date corespunzător.
    images_by_row: dict = {}
    for img in getattr(ws, "_images", []):
        try:
            row_num = img.anchor._from.row + 1
            data    = img.ref.getvalue() if hasattr(img.ref, "getvalue") else None
            if data:
                images_by_row[row_num] = data
        except Exception:
            continue

    # Identitatea unei mărci deja existente în listă (denumire + clase + imagine,
    # plus nr. depozit/înregistrare/dată depunere pentru diferențiere) — verificată
    # atât față de mărcile deja din DB, cât și față de rândurile deja importate din
    # ACEST fișier (ex. același rând dus de două ori în Excel).
    existing_keys = [
        {
            "name": w.trademark_name,
            "classes": w.nice_classes or [],
            "image_hash": w.image_hash,
            "application_number": w.application_number,
            "registration_number": w.registration_number,
            "filing_date": w.filing_date,
        }
        for w in db.query(WatchItem).all()
    ]

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        has_image = row_idx in images_by_row
        if (not row or all(v is None for v in row)) and not has_image:
            continue

        # Skip note rows (first cell starts with "*" or is italic note)
        first = str(row[0] or "").strip()
        if first.startswith("*") or first.startswith("Teritorii") or first.startswith("Imagine:") or first.startswith("câmp"):
            continue

        # Ordinea coincide cu antetul: (511) Clase NICE, (540) Denumire Marcă,
        # (540) Imagine, (731) Titular, apoi câmpurile fără cod INID.
        nice_classes_raw   = str(row[0] or "").strip() if len(row) > 0 else ""
        trademark_name     = str(row[1] or "").strip() if len(row) > 1 else ""
        holder_name        = str(row[3] or "").strip() if len(row) > 3 else ""
        offices_raw        = str(row[4] or "").strip() if len(row) > 4 else ""
        notification_email = str(row[5] or "").strip() if len(row) > 5 else ""
        frequency_raw      = str(row[6] or "").strip() if len(row) > 6 else "weekly"
        application_number = str(row[7] or "").strip() if len(row) > 7 else ""
        registration_number = str(row[8] or "").strip() if len(row) > 8 else ""
        filing_date         = _parse_filing_date(row[9] if len(row) > 9 else None)

        if not trademark_name:
            skipped.append({"row": row_idx, "reason": "Denumire marcă lipsă"})
            continue
        if not notification_email or "@" not in notification_email:
            errors.append({"row": row_idx, "trademark": trademark_name, "reason": "Email invalid sau lipsă"})
            continue

        nice_classes = _parse_classes(nice_classes_raw)
        if not nice_classes:
            errors.append({"row": row_idx, "trademark": trademark_name, "reason": "Clase NICE invalide sau lipsă"})
            continue

        offices   = _parse_offices(offices_raw) or ["RO", "EM"]
        frequency = _parse_frequency(frequency_raw)

        image_bytes = images_by_row.get(row_idx)
        image_hash  = hashlib.sha256(image_bytes).hexdigest() if image_bytes else None

        candidate_key = {
            "name": trademark_name, "classes": nice_classes, "image_hash": image_hash,
            "application_number": application_number, "registration_number": registration_number,
            "filing_date": filing_date,
        }
        dup = next((e for e in existing_keys if _is_duplicate_watch(e, candidate_key)), None)
        if dup:
            duplicates.append({
                "row": row_idx, "trademark": trademark_name,
                "reason": "Marcă identică (denumire + clase + imagine) deja în listă"
                          + (f" — nr. depozit/înregistrare {dup.get('application_number') or dup.get('registration_number')}"
                             if (dup.get("application_number") or dup.get("registration_number")) else ""),
            })
            continue

        reference_image = None
        if image_bytes:
            os.makedirs(WATCH_IMAGE_DIR, exist_ok=True)
            filename = f"{uuid.uuid4().hex}.png"
            with open(os.path.join(WATCH_IMAGE_DIR, filename), "wb") as f:
                f.write(image_bytes)
            reference_image = filename

        item = WatchItem(
            trademark_name     = trademark_name,
            holder_name        = holder_name,
            nice_classes       = nice_classes,
            offices            = offices,
            notification_email = notification_email,
            frequency          = frequency,
            reference_image    = reference_image,
            image_hash          = image_hash,
            application_number  = application_number or None,
            registration_number = registration_number or None,
            filing_date         = filing_date or None,
        )
        db.add(item)
        imported.append({"row": row_idx, "trademark": trademark_name, "email": notification_email})
        # rândul abia adăugat intră și el în lista de verificare, ca să prindem
        # și duplicate ÎNTRE rândurile acestui fișier, nu doar față de DB
        existing_keys.append(candidate_key)

    db.commit()

    return {
        "imported":   len(imported),
        "skipped":    len(skipped),
        "errors":     len(errors),
        "duplicates": len(duplicates),
        "details": {
            "imported":   imported,
            "skipped":    skipped,
            "errors":     errors,
            "duplicates": duplicates,
        },
    }
