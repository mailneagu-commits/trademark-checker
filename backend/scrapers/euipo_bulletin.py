"""
EUIPO EU Trade Marks Bulletin scraper.

EUIPO publică buletinul oficial în fiecare zi lucrătoare.
API descoperit via /copla/conf/all:
  - list:     https://euipo.europa.eu/copla/bulletin/data/list/CTM/{year}   → XML
  - download: https://euipo.europa.eu/copla/bulletin/data/download/CTM/{year}/{bulletinNumber}

XML list format:
  <bulletins>
    <bulletin>
      <idbulletin>NR</idbulletin>
      <datebulletin>DD/MM/YYYY</datebulletin>
      <valuebulletin>FILE_ID</valuebulletin>
    </bulletin>
    ...
  </bulletins>

Dacă API-ul nu e accesibil, fallback pe EUIPO Search API cu filtru pe applicationDate.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Tuple

import requests

EUIPO_COPLA_BASE   = "https://euipo.europa.eu/copla"
BULLETIN_LIST_URL  = f"{EUIPO_COPLA_BASE}/bulletin/data/list/CTM"        # /{year}
# Download: /copla/bulletin/data/download/CTM/{value}/{lang}
# NOTE: requires EUIPO SSO browser session — returns 404 without it
BULLETIN_DL_URL    = f"{EUIPO_COPLA_BASE}/bulletin/data/download/ctm"   # /{value}/{lang} — minuscule; "CTM" (majuscule) dă 404, nu e o problemă de autentificare

CACHE_DIR      = os.path.join(os.path.dirname(__file__), "..", "..", "data", "bulletins", "euipo")
PROCESSED_FILE = os.path.join(CACHE_DIR, "_processed.json")
REQUEST_TIMEOUT = 90   # buletinul PDF poate avea 15-20 MB

os.makedirs(CACHE_DIR, exist_ok=True)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/xml,text/xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection":      "keep-alive",
}


# ── Processed index ───────────────────────────────────────────────────────────

def _load_processed() -> dict:
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return json.load(f)
    return {}


def _save_processed(data: dict) -> None:
    with open(PROCESSED_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Marks cache (rezultatul parsării, nu doar PDF-ul brut) ────────────────────
# Parsarea PDF-ului cu pdfplumber (sute de pagini pentru buletinul EUIPO) durează
# zeci de secunde — fără acest cache, se repeta la fiecare cerere (ex. la fiecare
# rulare a comparației buletin↔monitorizare), chiar dacă PDF-ul era deja descărcat.

def _marks_cache_path(slug: str) -> str:
    return os.path.join(CACHE_DIR, f"{slug}.marks.json")


def _load_marks_cache(slug: str) -> Optional[List[Dict]]:
    path = _marks_cache_path(slug)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_marks_cache(slug: str, marks: List[Dict]) -> None:
    with open(_marks_cache_path(slug), "w") as f:
        json.dump(marks, f, ensure_ascii=False)


# ── Date helpers ──────────────────────────────────────────────────────────────

def _prev_working_day(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _date_slug(d: date) -> str:
    return f"euipo-{d.isoformat()}"


# ── Bulletin list (COPLA API) ─────────────────────────────────────────────────

def _fetch_bulletin_list(year: int) -> List[Dict]:
    """
    Returnează lista buletinelor disponibile pentru un an.
    Fiecare element: {"id": str, "date": date, "value": str}
    """
    url = f"{BULLETIN_LIST_URL}/{year}"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            print(f"[EUIPO Bulletin] List {url} → {r.status_code}")
            return []
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"[EUIPO Bulletin] List error: {e}")
        return []

    result = []
    for bul in root.iter("bulletin"):
        id_el    = bul.find("idbulletin")
        date_el  = bul.find("datebulletin")
        value_el = bul.find("valuebulletin")
        if id_el is None or date_el is None or value_el is None:
            continue
        date_str = (date_el.text or "").strip()   # "DD/MM/YYYY"
        try:
            d, m, y = date_str.split("/")
            bul_date = date(int(y), int(m), int(d))
        except Exception:
            continue
        result.append({
            "id":    (id_el.text or "").strip(),
            "date":  bul_date,
            "value": (value_el.text or "").strip(),
        })

    print(f"[EUIPO Bulletin] {len(result)} bulletins found for {year}")
    return result


def _find_bulletin_for_date(target: date) -> Optional[Dict]:
    """Găsește buletinul cel mai apropiat de data target (<=target)."""
    bulletins = _fetch_bulletin_list(target.year)

    # Încearcă și anul precedent dacă target e la început de an
    if not bulletins or (target.month == 1 and target.day <= 15):
        bulletins += _fetch_bulletin_list(target.year - 1)

    candidates = [b for b in bulletins if b["date"] <= target]
    if not candidates:
        return None
    return max(candidates, key=lambda b: b["date"])


# ── Bulletin download + parse ─────────────────────────────────────────────────

def _download_bulletin(value: str, slug: str, lang: str = "EN") -> Tuple[Optional[str], Optional[str]]:
    """
    Descarcă buletinul oficial (PDF) și returnează (cale_locală, eroare).
    URL: /copla/bulletin/data/download/ctm/{value}/{lang} — public, fără autentificare
    (verificat manual: minuscule "ctm", nu "CTM" — cu majuscule dă 404 și părea o
    problemă de SSO, dar era doar case-sensitivity în URL).
    """
    local = os.path.join(CACHE_DIR, f"{slug}.pdf")
    if os.path.exists(local):
        print(f"[EUIPO Bulletin] Using cached {local}")
        return local, None

    url = f"{BULLETIN_DL_URL}/{value}/{lang}"
    # Transferul mare (15-20 MB) se întrerupe intermitent la conexiune (verificat: ~40-50%
    # rată de succes per încercare, chiar și local — nu e blocaj, doar instabilitate de
    # rețea pe fișiere mari), de multe ori aproape de final (ex. 17.7 din 20 MB citiți).
    # Reluăm de unde am rămas via header Range în loc să redescărcăm tot de la zero la
    # fiecare încercare — dacă serverul nu suportă Range (răspunde 200 în loc de 206 la
    # o cerere cu Range), renunțăm la ce aveam și pornim din nou de la zero.
    max_attempts = 5
    last_err: Optional[str] = None
    content = bytearray()
    for attempt in range(1, max_attempts + 1):
        resuming = len(content) > 0
        headers = dict(_HEADERS)
        if resuming:
            headers["Range"] = f"bytes={len(content)}-"
            print(f"[EUIPO Bulletin] Downloading {url} (attempt {attempt}/{max_attempts}, "
                  f"resuming from {len(content)//1024} KB)")
        else:
            print(f"[EUIPO Bulletin] Downloading {url} (attempt {attempt}/{max_attempts})")
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True)
            if resuming and r.status_code == 200:
                print("[EUIPO Bulletin] Serverul nu suportă reluare (Range) — reia de la zero")
                content = bytearray()
                resuming = False
            elif r.status_code not in (200, 206):
                last_err = f"http_{r.status_code}"
                print(f"[EUIPO Bulletin] Download {url} → {r.status_code}")
                if r.status_code == 416:   # range invalid — starea locală nu mai e de încredere
                    content = bytearray()
                continue

            for chunk in r.iter_content(chunk_size=1024 * 256):
                content.extend(chunk)

            if not resuming and (not content or content[:5] != b"%PDF-"):
                last_err = f"not_pdf: {bytes(content[:80])!r}"
                print(f"[EUIPO Bulletin] Răspuns neașteptat (nu PDF): {bytes(content[:80])}")
                content = bytearray()
                continue

            if b"%%EOF" not in bytes(content[-2048:]):
                last_err = f"truncated: {len(content)} bytes so far, no %%EOF trailer"
                print(f"[EUIPO Bulletin] PDF trunchiat (attempt {attempt}): {len(content)} bytes, "
                      f"fără %%EOF — reluăm de unde am rămas")
                continue

            data = bytes(content)
            with open(local, "wb") as f:
                f.write(data)
            size_kb = len(data) // 1024
            print(f"[EUIPO Bulletin] Saved {local} ({size_kb} KB, attempt {attempt})")
            return local, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[EUIPO Bulletin] Download error (attempt {attempt}, "
                  f"{len(content)//1024} KB acumulați până acum): {e}")
            # păstrăm `content` — e o cădere de conexiune la mijlocul transferului,
            # nu date corupte, deci merită reluat de unde a rămas
    return None, last_err


_RE_EUIPO_210   = re.compile(r'\b210\s+(\d{8,9})\b')
_RE_EUIPO_220   = re.compile(r'\b220\s+(\d{2})/(\d{2})/(\d{4})')
_RE_EUIPO_541   = re.compile(r'\b541\s+(.*?)(?=\n\d{3}\s|\Z)', re.DOTALL)
_RE_EUIPO_731   = re.compile(r'\b731\s+(.*?)(?=\n740\s|\n270\s|\n511\s|\n\d{3}\s|\Z)', re.DOTALL)
_RE_EUIPO_740   = re.compile(r'\b740\s+(.*?)(?=\n270\s|\n511\s|\n\d{3}\s|\Z)', re.DOTALL)
_RE_EUIPO_511   = re.compile(r'\b511\s+(.*)\Z', re.DOTALL)
_RE_EUIPO_CLASS = re.compile(r'(?:^|\n)\s*(\d{1,2})\s*-\s')
_RE_FOOTER      = re.compile(r'\n\s*\d{4}/\d{2,4}\s*(?=\n|\Z)')   # "2026/158" (nr. buletin, subsol pagină)


def _euipo_page_text(page) -> str:
    """Text în ordinea de citire (coloana stângă completă, apoi dreapta) —
    un rând al buletinului poate continua pe coloana următoare/pagina următoare."""
    left  = page.crop((0, 0, page.width / 2, page.height)).extract_text() or ""
    right = page.crop((page.width / 2, 0, page.width, page.height)).extract_text() or ""
    return _RE_FOOTER.sub("", left + "\n" + right)


def _norm_field(s: str) -> str:
    return re.sub(r'\s*\n\s*', ', ', s.strip()) if s else ""


def _parse_euipo_entry(block: str) -> Optional[Dict]:
    m210 = _RE_EUIPO_210.search(block)
    if not m210:
        return None
    app_num = m210.group(1)

    m220 = _RE_EUIPO_220.search(block)
    app_date = None
    if m220:
        d, mo, y = m220.group(1), m220.group(2), m220.group(3)
        app_date = f"{y}-{mo}-{d}T00:00:00.000Z"

    m541 = _RE_EUIPO_541.search(block)
    tm_name = _norm_field(m541.group(1)).rstrip(", ") if m541 else ""

    m731 = _RE_EUIPO_731.search(block)
    applicant_full = _norm_field(m731.group(1)) if m731 else ""
    applicant_name = applicant_full.split(",")[0].strip() if applicant_full else ""

    m740 = _RE_EUIPO_740.search(block)
    rep_full = _norm_field(m740.group(1)) if m740 else ""
    rep_name = rep_full.split(",")[0].strip() if rep_full else ""

    m511 = _RE_EUIPO_511.search(block)
    classes: List[int] = []
    if m511:
        classes = sorted(set(int(c) for c in _RE_EUIPO_CLASS.findall(m511.group(1))))

    return {
        "ST13":              f"EM{app_num}",
        "tmName":            tm_name,
        "tmOffice":          "EM",
        "tradeMarkStatus":   "APPLICATION_PUBLISHED",
        "niceClass":         classes,
        "applicantName":     [applicant_name] if applicant_name else [],
        "applicantAddress":  applicant_full,
        "representative":    rep_name,
        "representativeAddress": rep_full,
        "applicationDate":   app_date,
        "applicationNumber": app_num,
        "registrationDate":  None,
        "expiryDate":        None,
        "markImageURI":      f"/api/monitor/bulletin-image?source=euipo&app_num={app_num}",
        "goodAndServices":   [],
        "_source":           "euipo_bulletin_pdf",
    }


def _parse_bulletin_pdf(path: str) -> List[Dict]:
    """Parsează buletinul oficial PDF (Part A — cereri publicate), cod cu cod INID
    (WIPO ST.60): (210) nr. cerere, (220) dată depunere, (541) element verbal,
    (731) solicitant, (740) reprezentant, (511) clase Nice + produse/servicii.
    Pagină pe 2 coloane, fără watermark (spre deosebire de OSIM)."""
    try:
        import pdfplumber
    except ImportError:
        print("[EUIPO Bulletin] pdfplumber not installed")
        return []

    pages_text: List[str] = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages):
                txt = _euipo_page_text(page)
                # Part A (cereri publicate) e prima secțiune; ne oprim la Part B.
                if i > 3 and re.search(r'\bPART\s+B\b', txt.upper()):
                    print(f"[EUIPO Bulletin] Stopped at page {i} (Part B reached)")
                    break
                pages_text.append(txt)
    except Exception as e:
        print(f"[EUIPO Bulletin] PDF parse error: {e}")
        return []

    combined = "\n".join(pages_text)
    starts = [m.start() for m in re.finditer(r'\n?210\s+\d{8,9}\b', combined)]

    marks: List[Dict] = []
    seen: set = set()
    skipped_stubs = 0
    for i, pos in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(combined)
        entry = _parse_euipo_entry(combined[pos:end])
        if not entry or entry["applicationNumber"] in seen:
            continue
        # Intrări "Part A.2" — corecturi/trimiteri către o publicare anterioară
        # (cod 400, ex. "11/11/2024 - 2024/216 - A.1"), fără (541)/(731) proprii,
        # deci fără nume de marcă și fără solicitant. Nu sunt cereri noi publicate
        # aici — datele reale au apărut deja în buletinul referit. Fără nume,
        # nu pot fi comparate prin similaritate, deci nu au valoare de monitorizare.
        if not entry["tmName"] and not entry["applicantName"]:
            skipped_stubs += 1
            continue
        seen.add(entry["applicationNumber"])
        marks.append(entry)
    if skipped_stubs:
        print(f"[EUIPO Bulletin] Skipped {skipped_stubs} correction-reference stubs (Part A.2, no name/applicant)")

    print(f"[EUIPO Bulletin] Extracted {len(marks)} marks from PDF ({len(pages_text)} pages)")
    return marks


# ── EUIPO Search API fallback ─────────────────────────────────────────────────

def _fetch_via_api(target: date) -> Tuple[List[Dict], Optional[str]]:
    """Fallback: returnează mărci via EUIPO Search API cu filtru pe applicationDate.
    Returnează (marks, error) — error e None dacă totul a mers bine (chiar și cu 0 marks)."""
    try:
        from agents.euipo_agent import euipo_available, _get_access_token, EUIPO_SEARCH_URL, EUIPO_CLIENT_ID, _to_internal
    except ImportError as e:
        return [], f"import_error: {e}"

    if not euipo_available():
        return [], "not_configured"

    try:
        token = _get_access_token()
    except Exception as e:
        return [], f"token_error: {e}"

    date_from = (target - timedelta(days=1)).isoformat()
    date_to   = (target + timedelta(days=1)).isoformat()

    headers = {
        "X-IBM-Client-Id": EUIPO_CLIENT_ID,
        "Authorization":   f"Bearer {token}",
        "Accept":          "application/json",
    }

    all_marks: List[Dict] = []
    seen: set = set()
    page = 0

    while True:
        query = f"applicationDate>={date_from};applicationDate<={date_to}"
        try:
            resp = requests.get(
                EUIPO_SEARCH_URL,
                headers=headers,
                params={"query": query, "size": 100, "page": page, "sort": "applicationDate:desc"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                return all_marks, f"http_{resp.status_code}: {resp.text[:200]}"
            batch = resp.json().get("trademarks") or []
            if not batch:
                break
            for tm in batch:
                key = tm.get("applicationNumber", "")
                if key and key not in seen:
                    seen.add(key)
                    internal = _to_internal(tm)
                    internal["_source"] = "euipo_api_daily"
                    all_marks.append(internal)
            if len(batch) < 100:
                break
            page += 1
        except Exception as e:
            return all_marks, f"request_error: {type(e).__name__}: {e}"

    print(f"[EUIPO API] {len(all_marks)} marks for {target.isoformat()}")
    return all_marks, None


# ── Public API ────────────────────────────────────────────────────────────────

# Descărcarea PDF-ului (15-30 MB, conexiune instabilă) poate depăși limita de
# 300s a gateway-ului Railway dacă rulează sincron într-un request HTTP.
# in_progress ține evidența job-urilor de fundal pornite, ca să nu pornim
# de două ori aceeași descărcare dacă utilizatorul apasă din nou butonul.
_in_progress: set = set()


def is_bulletin_cached(target: date) -> bool:
    """True dacă PDF-ul buletinului pentru această dată e deja în cache local
    (deci fetch_euipo_for_date() va răspunde instant, fără descărcare)."""
    working = _prev_working_day(target)
    slug    = _date_slug(working)
    return os.path.exists(os.path.join(CACHE_DIR, f"{slug}.pdf"))


def is_fetch_in_progress(target: date) -> bool:
    working = _prev_working_day(target)
    return _date_slug(working) in _in_progress


def should_run_sync(target: date, cooldown_seconds: int = 300) -> bool:
    """True dacă fetch_euipo_for_date() poate rula sincron (rapid) — fie PDF-ul
    e deja în cache, fie am mai încercat recent și avem deja un rezultat (chiar
    via fallback API) în processed.json. Fără asta, fiecare cerere după un
    fallback API reușit ar porni din nou un job de fundal pentru PDF, la
    infinit, pentru că is_bulletin_cached() singur nu vede fallback-ul API."""
    if is_bulletin_cached(target):
        return True
    working = _prev_working_day(target)
    slug    = _date_slug(working)
    entry   = _load_processed().get(slug)
    if not entry or "at" not in entry:
        return False
    try:
        age = (datetime.utcnow() - datetime.fromisoformat(entry["at"])).total_seconds()
    except ValueError:
        return False
    return age < cooldown_seconds


def fetch_euipo_for_date(target: date, skip_pdf_download: bool = False) -> Tuple[List[Dict], dict]:
    """
    Returnează mărcile EUIPO din buletinul pentru data specificată.
    Încearcă mai întâi COPLA bulletin API; fallback pe EUIPO Search API.

    skip_pdf_download: sare peste încercarea de descărcare PDF (lentă, 15-30 MB,
    conexiune instabilă) dacă PDF-ul nu e deja în cache — direct la API, rapid.
    Folosit când tocmai am încercat descărcarea recent (vezi should_run_sync) și
    nu vrem să repetăm cele 5 încercări la fiecare cerere sincronă.
    """
    processed = _load_processed()
    working   = _prev_working_day(target)
    slug      = _date_slug(working)

    info = {
        "slug":        slug,
        "target_date": target.isoformat(),
        "working_day": working.isoformat(),
    }

    # Încearcă COPLA bulletin (sau folosește direct PDF-ul deja în cache)
    bulletin = _find_bulletin_for_date(working)
    if bulletin and (not skip_pdf_download or is_bulletin_cached(target)):
        info["bulletin_id"]   = bulletin["id"]
        info["bulletin_date"] = bulletin["date"].isoformat()
        local, dl_error = _download_bulletin(bulletin["value"], slug)
        if local:
            marks = _load_marks_cache(slug)
            if marks is None:
                marks = _parse_bulletin_pdf(local)
                if marks:
                    _save_marks_cache(slug, marks)
            if marks:
                info["status"] = "ok_bulletin"
                info["source"] = "copla_bulletin"
                info["marks"]  = len(marks)
                info["at"]     = datetime.utcnow().isoformat()
                processed[slug] = info
                _save_processed(processed)
                return marks, info
            info["pdf_attempt"] = "parse_error: 0 marks extracted from downloaded PDF"
        else:
            info["pdf_attempt"] = f"download_failed: {dl_error}"

    # Fallback: EUIPO Search API
    marks, api_error = _fetch_via_api(working)
    if api_error:
        info["api_error"] = api_error
    if marks:
        info["status"] = "ok_api"
        info["source"] = "api"
        info.pop("error", None)
    else:
        if "status" not in info:
            if api_error:
                info["status"] = "api_error"
                info["error"]  = f"Buletin EUIPO: eroare la EUIPO Search API — {api_error}"
            else:
                info["status"] = "no_results"
                info["error"]  = "Buletin EUIPO: nicio marcă găsită pentru această dată."
        info["source"] = info.get("source", "none")
    info["marks"]  = len(marks)
    info["at"]     = datetime.utcnow().isoformat()
    processed[slug] = info
    _save_processed(processed)
    return marks, info


def fetch_latest_euipo(max_days: int = 2) -> List[Dict]:
    """Descarcă buletinele pentru ultimele N zile lucrătoare neprocesate."""
    all_marks: List[Dict] = []
    processed = _load_processed()
    d         = _prev_working_day(date.today())
    tried     = 0

    while tried < max_days:
        slug = _date_slug(d)
        if slug not in processed:
            marks, _ = fetch_euipo_for_date(d)
            all_marks.extend(marks)
            tried += 1
        d = _prev_working_day(d - timedelta(days=1))

    return all_marks


def list_processed() -> List[dict]:
    processed = _load_processed()
    return sorted(processed.values(), key=lambda x: x.get("at", ""), reverse=True)
