"""
OSIM BOPI scraper — Buletinul Oficial de Proprietate Industrială (Mărci).

BOPI este publicat zilnic (zile lucrătoare).
URL: https://www.osim.ro/images/Publicatii/Marci/{YYYY}/bopi{DDMMYYYY}.pdf
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Tuple

import requests

OSIM_BASE      = "https://www.osim.ro/images/Publicatii/Marci"
CACHE_DIR      = os.path.join(os.path.dirname(__file__), "..", "..", "data", "bulletins", "osim")
PROCESSED_FILE = os.path.join(CACHE_DIR, "_processed.json")
REQUEST_TIMEOUT = 30

RE_APP_NUM = re.compile(r'\bM[\s\-]?(\d{4})[\s\-]?(\d{4,6})\b', re.IGNORECASE)
RE_NICE    = re.compile(r'(?:Cl(?:ase?)?\.?\s*:?\s*)((?:\d{1,2}[,;\s]+)*\d{1,2})', re.IGNORECASE)
RE_DATE    = re.compile(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})')

# ── Secțiunea detaliată (coduri INID, WIPO ST.60) ──────────────────────────────
# BOPI publică, pe lângă indexul compact, un capitol cu fiecare marcă detaliat
# (nume+adresă solicitant, reprezentant, clasificare Viena, culori, clase Nice).
# Pagina e pe 2 coloane, cu un watermark suprapus (font Arial-Black, distinct
# de textul real) — filtrat la extracție, nu prin regex post-hoc.
_WATERMARK_FONT = "Arial-Black"
RE_D_APPNUM = re.compile(r'\(210\)\s*(M\s*\d{4}\s*\d+)', re.IGNORECASE)
RE_D_DATE   = re.compile(r'\(151\)\s*(\d{2})\.(\d{2})\.(\d{4})')
RE_D_732    = re.compile(r'\(732\)\s*(.*?)(?=\(740\)|\(540\)|$)', re.DOTALL)
RE_D_740    = re.compile(r'\(740\)\s*(.*?)(?=\(540\)|$)', re.DOTALL)
RE_D_540    = re.compile(r'\(540\)\s*(.*?)(?=\(531\)|\(591\)|\(511\)|$)', re.DOTALL)
RE_D_531    = re.compile(r'\(531\)\s*Clasificare Viena:\s*(.*?)(?=\(591\)|\(511\)|$)', re.DOTALL)
RE_D_591    = re.compile(r'\(591\)\s*Culori revendicate:\s*(.*?)(?=\(511\)|$)', re.DOTALL)
RE_D_511    = re.compile(r'\(511\)\s*(.*?)$', re.DOTALL)
RE_D_CLASS  = re.compile(r'(?:^|\n)\s*(\d{1,2})\.\s')

os.makedirs(CACHE_DIR, exist_ok=True)


# ── Processed index ───────────────────────────────────────────────────────────

def _load_processed() -> dict:
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return json.load(f)
    return {}


def _save_processed(data: dict) -> None:
    with open(PROCESSED_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Date helpers ──────────────────────────────────────────────────────────────

def _prev_working_day(d: date) -> date:
    """Întoarce ultima zi lucrătoare <= d."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _bopi_url(d: date) -> str:
    """URL-ul PDF-ului BOPI pentru o zi lucrătoare."""
    return f"{OSIM_BASE}/{d.year}/bopi{d.day:02d}{d.month:02d}{d.year}.pdf"


def _date_slug(d: date) -> str:
    return f"osim-{d.isoformat()}"


# ── Download ──────────────────────────────────────────────────────────────────

def _download_pdf(d: date) -> Optional[str]:
    slug  = _date_slug(d)
    local = os.path.join(CACHE_DIR, f"{slug}.pdf")
    if os.path.exists(local):
        print(f"[OSIM] Using cached {local}")
        return local

    url = _bopi_url(d)
    print(f"[OSIM] Trying {url}")
    try:
        r = requests.get(
            url, timeout=REQUEST_TIMEOUT, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TrademarkMonitor/1.0)"},
        )
        if r.status_code == 404:
            print(f"[OSIM] 404 for {url}")
            return None
        r.raise_for_status()
        with open(local, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        size_kb = os.path.getsize(local) // 1024
        print(f"[OSIM] Downloaded {local} ({size_kb} KB)")
        return local
    except Exception as e:
        print(f"[OSIM] Download error: {e}")
        return None


# ── PDF parsing ───────────────────────────────────────────────────────────────

def _clean_cell(val: Optional[str]) -> str:
    """Elimină literele-watermark (caractere singure pe linie) din celule."""
    if not val:
        return ""
    lines = [l for l in val.split("\n") if not (len(l.strip()) == 1 and l.strip().isalpha())]
    return " ".join(l.strip() for l in lines if l.strip())


def _parse_index_table(path: str) -> List[Dict]:
    """Parsează indexul compact BOPI (Nr.Crt/Nr.Depozit/Data/Titular/Denumire) —
    dă mereu rezultate de bază, chiar dacă secțiunea detaliată nu poate fi parsată."""
    try:
        import pdfplumber
    except ImportError:
        print("[OSIM] pdfplumber not installed")
        return []

    marks:   List[Dict] = []
    seen_nums: set      = set()

    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                clean_page = page.filter(lambda o: o.get("fontname") != _WATERMARK_FONT)
                tables = clean_page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    # Check if this looks like the BOPI trademark table (5 columns)
                    if len(table[0]) < 5:
                        continue
                    for row in table[1:]:   # skip header
                        if len(row) < 5:
                            continue
                        app_raw  = _clean_cell(row[1])
                        date_raw = _clean_cell(row[2])
                        holder   = _clean_cell(row[3])
                        tm_raw   = _clean_cell(row[4])

                        m = RE_APP_NUM.search(app_raw)
                        if not m:
                            continue
                        year_str, num_str = m.group(1), m.group(2)
                        app_num = f"M{year_str}{num_str}"
                        if app_num in seen_nums:
                            continue
                        seen_nums.add(app_num)

                        app_date = None
                        dm = RE_DATE.search(date_raw)
                        if dm:
                            try:
                                d, mo, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
                                app_date = f"{y}-{mo:02d}-{d:02d}T00:00:00.000Z"
                            except ValueError:
                                pass

                        tm_name = tm_raw.strip()
                        if not tm_name or len(tm_name) < 2:
                            continue

                        marks.append({
                            "ST13":              f"RO{app_num}",
                            "tmName":            tm_name,
                            "tmOffice":          "RO",
                            "tradeMarkStatus":   "Filed",
                            "niceClass":         [],
                            "applicantName":     [holder] if holder else [],
                            "applicationDate":   app_date,
                            "applicationNumber": app_num,
                            "registrationDate":  None,
                            "expiryDate":        None,
                            "markImageURI":      None,
                            "goodAndServices":   [],
                            "_source":           "osim_bopi",
                        })
    except Exception as e:
        print(f"[OSIM] PDF parse error: {e}")

    print(f"[OSIM] Index: {len(marks)} marks from {os.path.basename(path)}")
    return marks


def _norm(s: Optional[str]) -> str:
    return re.sub(r'\s*\n\s*', ' ', s).strip(' ,') if s else ""


def _parse_detail_block(block: str) -> Optional[Dict]:
    m_app = RE_D_APPNUM.search(block)
    m_date = RE_D_DATE.search(block)
    if not m_app or not m_date:
        return None
    app_num = re.sub(r'\s+', '', m_app.group(1)).upper()
    y, mo, d = m_date.group(3), m_date.group(2), m_date.group(1)

    m732 = RE_D_732.search(block)
    holder_full = _norm(m732.group(1)) if m732 else ""
    holder_name = holder_full.split(',')[0].strip() if holder_full else ""

    m740 = RE_D_740.search(block)
    rep_full = _norm(m740.group(1)) if m740 else ""
    rep_name = rep_full.split(',')[0].strip() if rep_full else ""

    m540 = RE_D_540.search(block)
    tm_name = _norm(m540.group(1)) if m540 else ""

    m531 = RE_D_531.search(block)
    vienna = _norm(m531.group(1)) if m531 else ""

    m591 = RE_D_591.search(block)
    colors = _norm(m591.group(1)) if m591 else ""

    m511 = RE_D_511.search(block)
    classes: List[int] = []
    if m511:
        classes = sorted(set(int(c) for c in RE_D_CLASS.findall(m511.group(1))))

    return {
        "applicationNumber": app_num,
        "applicationDate":   f"{y}-{mo}-{d}T00:00:00.000Z",
        "holderName":        holder_name,
        "holderAddress":     holder_full,
        "representative":    rep_name,
        "representativeAddress": rep_full,
        "tmName":            tm_name,
        "viennaClasses":     vienna,
        "colorsClaimed":     colors,
        "niceClass":         classes,
    }


def _image_dir_for(path: str) -> str:
    slug = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(CACHE_DIR, "images", slug)


def get_bulletin_image_path(bulletin_slug: str, app_num: str) -> Optional[str]:
    """Calea locală a imaginii mărcii (salvată la parsare), pentru servire via API."""
    safe = re.sub(r'[^A-Za-z0-9_-]', '', app_num)
    p = os.path.join(CACHE_DIR, "images", bulletin_slug, f"{safe}.png")
    return p if os.path.exists(p) else None


def _parse_detail_pages(path: str) -> Dict[str, Dict]:
    """Parsează capitolul detaliat BOPI (coduri INID (210)(151)(732)(740)(540)
    (531)(591)(511)) — pagină pe 2 coloane, watermark filtrat după font
    (Arial-Black, distinct de textul real). Returnează dict cheiat pe nr. cerere.

    Asociază și imaginea mărcii (pentru mărci figurative), după poziția ei
    verticală față de cel mai apropiat cod (210) aflat deasupra, în aceeași
    coloană — PDF-ul nu leagă explicit imaginea de un anumit nr. de cerere."""
    try:
        import pdfplumber
    except ImportError:
        return {}

    img_dir: Optional[str] = None
    detail: Dict[str, Dict] = {}
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                clean = page.filter(lambda o: o.get("fontname") != _WATERMARK_FONT)
                halves = [(0, 0, page.width / 2, page.height),
                          (page.width / 2, 0, page.width, page.height)]
                for box in halves:
                    col  = clean.crop(box)
                    text = col.extract_text() or ""
                    for block in re.split(r'─{3,}', text):
                        if "(210)" not in block:
                            continue
                        parsed = _parse_detail_block(block)
                        if parsed:
                            detail[parsed["applicationNumber"]] = parsed

                    if not col.images:
                        continue
                    matches = col.search(r'\(210\)\s*(M\s*\d{4}\s*\d+)', regex=True)
                    matches.sort(key=lambda m: m["top"])
                    for img in col.images:
                        owner = None
                        for m in matches:
                            if m["top"] <= img["top"]:
                                owner = m
                            else:
                                break
                        if not owner:
                            continue
                        app_num = re.sub(r'\s+', '', owner["groups"][0]).upper()
                        if app_num not in detail:
                            continue
                        try:
                            if img_dir is None:
                                img_dir = _image_dir_for(path)
                                os.makedirs(img_dir, exist_ok=True)
                            img_path = os.path.join(img_dir, f"{app_num}.png")
                            if not os.path.exists(img_path):
                                bbox = (img["x0"], img["top"], img["x1"], img["bottom"])
                                page.crop(bbox).to_image(resolution=200).save(img_path)
                            detail[app_num]["hasImage"] = True
                        except Exception as e:
                            print(f"[OSIM] Image save error for {app_num}: {e}")
    except Exception as e:
        print(f"[OSIM] Detail parse error: {e}")

    print(f"[OSIM] Detail: {len(detail)} marks from {os.path.basename(path)}")
    return detail


def _parse_pdf(path: str) -> List[Dict]:
    """Combină indexul compact (întotdeauna disponibil) cu secțiunea detaliată
    (clase Nice, adresă solicitant, reprezentant, Viena, culori) — îmbogățește
    fiecare marcă din index cu datele detaliate găsite după nr. cerere, și
    adaugă orice marcă găsită doar în detaliu (index-ul poate rata rânduri la
    întreruperi de pagină ale tabelului)."""
    marks  = _parse_index_table(path)
    detail = _parse_detail_pages(path)
    slug   = os.path.splitext(os.path.basename(path))[0]

    def _image_uri(app_num: str) -> Optional[str]:
        return f"/api/monitor/bulletin-image?source=osim&slug={slug}&app_num={app_num}"

    seen = {m["applicationNumber"] for m in marks}
    for m in marks:
        d = detail.get(m["applicationNumber"])
        if not d:
            continue
        if d.get("hasImage"):
            m["markImageURI"] = _image_uri(m["applicationNumber"])
        if d["tmName"]:
            # numele din secțiunea detaliată e curățat de watermark (filtrat după
            # font); cel din indexul-tabel poate avea litere de watermark scăpate
            # în mijlocul cuvântului, unde _clean_cell nu le poate elimina.
            m["tmName"] = d["tmName"]
        if d["niceClass"]:
            m["niceClass"] = d["niceClass"]
        if d["holderAddress"]:
            m["applicantName"]    = [d["holderName"]] if d["holderName"] else m["applicantName"]
            m["applicantAddress"] = d["holderAddress"]
        if d["representative"]:
            m["representative"]        = d["representative"]
            m["representativeAddress"] = d["representativeAddress"]
        if d["viennaClasses"]:
            m["viennaClasses"] = d["viennaClasses"]
        if d["colorsClaimed"]:
            m["colorsClaimed"] = d["colorsClaimed"]

    # Mărci găsite doar în secțiunea detaliată (index-ul le-a ratat)
    for app_num, d in detail.items():
        if app_num in seen:
            continue
        marks.append({
            "ST13":              f"RO{app_num}",
            "tmName":            d["tmName"],
            "tmOffice":          "RO",
            "tradeMarkStatus":   "Filed",
            "niceClass":         d["niceClass"],
            "applicantName":     [d["holderName"]] if d["holderName"] else [],
            "applicantAddress":  d["holderAddress"],
            "representative":    d["representative"],
            "representativeAddress": d["representativeAddress"],
            "viennaClasses":     d["viennaClasses"],
            "colorsClaimed":     d["colorsClaimed"],
            "applicationDate":   d["applicationDate"],
            "applicationNumber": app_num,
            "registrationDate":  None,
            "expiryDate":        None,
            "markImageURI":      _image_uri(app_num) if d.get("hasImage") else None,
            "goodAndServices":   [],
            "_source":           "osim_bopi_detail",
        })

    print(f"[OSIM] Combined: {len(marks)} marks from {os.path.basename(path)}")
    return marks


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_osim_for_date(target: date) -> Tuple[List[Dict], dict]:
    """
    Descarcă și parsează buletinul BOPI pentru data specificată (sau ultima zi lucrătoare).
    Returnează (marks, info_dict).
    """
    processed = _load_processed()
    working   = _prev_working_day(target)
    slug      = _date_slug(working)

    info = {
        "slug":        slug,
        "target_date": target.isoformat(),
        "working_day": working.isoformat(),
        "url":         _bopi_url(working),
    }

    local = _download_pdf(working)
    if not local:
        info["status"] = "not_found"
        info["error"]  = f"PDF indisponibil pentru {working.isoformat()} — buletinul poate să nu fi apărut încă"
        info["at"]     = datetime.utcnow().isoformat()
        processed[slug] = info
        _save_processed(processed)
        return [], info

    marks = _parse_pdf(local)
    info["status"] = "ok"
    info["marks"]  = len(marks)
    info["at"]     = datetime.utcnow().isoformat()
    processed[slug] = info
    _save_processed(processed)
    return marks, info


def fetch_latest_osim(max_days: int = 3) -> List[Dict]:
    """Descarcă buletinele pentru ultimele N zile lucrătoare neprocesate."""
    all_marks: List[Dict] = []
    processed = _load_processed()
    d         = _prev_working_day(date.today())
    tried     = 0

    while tried < max_days:
        slug = _date_slug(d)
        if slug not in processed:
            marks, _ = fetch_osim_for_date(d)
            all_marks.extend(marks)
            tried += 1
        d = _prev_working_day(d - timedelta(days=1))

    return all_marks


def list_processed() -> List[dict]:
    processed = _load_processed()
    return sorted(processed.values(), key=lambda x: x.get("at", ""), reverse=True)
