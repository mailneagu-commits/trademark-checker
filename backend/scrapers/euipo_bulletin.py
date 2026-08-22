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
BULLETIN_DL_URL    = f"{EUIPO_COPLA_BASE}/bulletin/data/download/CTM"   # /{value}/{lang}

CACHE_DIR      = os.path.join(os.path.dirname(__file__), "..", "..", "data", "bulletins", "euipo")
PROCESSED_FILE = os.path.join(CACHE_DIR, "_processed.json")
REQUEST_TIMEOUT = 60

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

def _download_bulletin(value: str, slug: str, lang: str = "EN") -> Optional[str]:
    """
    Descarcă buletinul și returnează calea locală.
    URL: /copla/bulletin/data/download/CTM/{value}/{lang}
    Notă: EUIPO necesită sesiune SSO — va da 404 fără browser autentificat.
    """
    local = os.path.join(CACHE_DIR, f"{slug}.xml")
    if os.path.exists(local):
        print(f"[EUIPO Bulletin] Using cached {local}")
        return local

    url = f"{BULLETIN_DL_URL}/{value}/{lang}"
    print(f"[EUIPO Bulletin] Downloading {url}")
    try:
        r = requests.get(url, headers=_HEADERS, timeout=REQUEST_TIMEOUT, stream=True)
        if r.status_code != 200:
            print(f"[EUIPO Bulletin] Download {url} → {r.status_code} (sesiune SSO necesară)")
            return None
        content = r.content
        if not content or content[:5] not in (b"<?xml", b"<bull"):
            print(f"[EUIPO Bulletin] Răspuns neașteptat (nu XML): {content[:80]}")
            return None
        with open(local, "wb") as f:
            f.write(content)
        size_kb = len(content) // 1024
        print(f"[EUIPO Bulletin] Saved {local} ({size_kb} KB)")
        return local
    except Exception as e:
        print(f"[EUIPO Bulletin] Download error: {e}")
        return None


def _parse_bulletin_xml(path: str) -> List[Dict]:
    """Parsează XML ST.96 al buletinului EUIPO și extrage mărcile."""
    marks: List[Dict] = []
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"[EUIPO Bulletin] XML parse error: {e}")
        return []

    NS_TM = "{http://www.wipo.int/standards/XMLSchema/ST96/Trademark}"
    NS_CM = "{http://www.wipo.int/standards/XMLSchema/ST96/Common}"

    def _text(el, *tags) -> str:
        for tag in tags:
            for ns in ("", NS_TM, NS_CM):
                found = el.find(f".//{ns}{tag}")
                if found is not None and found.text:
                    return found.text.strip()
        return ""

    def _all(el, tag) -> list:
        result = []
        for ns in ("", NS_TM, NS_CM):
            result.extend(el.findall(f".//{ns}{tag}"))
        return result

    def _iter_trademarks(node):
        local = node.tag.split("}")[-1] if "}" in node.tag else node.tag
        if local in ("TradeMark", "TrademarkDetail", "trademark"):
            yield node
        for child in node:
            yield from _iter_trademarks(child)

    for tm in _iter_trademarks(root):
        app_num      = _text(tm, "ApplicationNumber", "applicationNumber")
        verbal       = _text(tm, "VerbalElement", "WordMark", "MarkText")
        app_date_raw = _text(tm, "ApplicationDate", "FilingDate")
        status       = _text(tm, "MarkCurrentStatusCode", "Status", "TradeMarkStatus")
        nc_raw       = _text(tm, "NiceClassification", "NiceClass")
        nice         = [c.strip() for c in re.split(r'[\s,;]+', nc_raw) if c.strip().isdigit()]
        applicants   = [el.text.strip() for el in _all(tm, "ApplicantName")
                        if el.text and el.text.strip()]

        if not app_num and not verbal:
            continue

        app_date = None
        if app_date_raw:
            try:
                app_date = datetime.fromisoformat(app_date_raw[:10]).strftime("%Y-%m-%dT00:00:00.000Z")
            except ValueError:
                pass

        marks.append({
            "ST13":              f"EM{app_num}" if app_num else "",
            "tmName":            verbal,
            "tmOffice":          "EM",
            "tradeMarkStatus":   status or "Filed",
            "niceClass":         [int(c) for c in nice if c.isdigit()],
            "applicantName":     applicants,
            "applicationDate":   app_date,
            "applicationNumber": app_num,
            "registrationDate":  None,
            "expiryDate":        None,
            "markImageURI":      None,
            "goodAndServices":   [],
            "_source":           "euipo_bulletin_xml",
        })

    print(f"[EUIPO Bulletin] Extracted {len(marks)} marks from XML")
    return marks


# ── EUIPO Search API fallback ─────────────────────────────────────────────────

def _fetch_via_api(target: date) -> List[Dict]:
    """Fallback: returnează mărci via EUIPO Search API cu filtru pe applicationDate."""
    try:
        from agents.euipo_agent import euipo_available, _get_access_token, EUIPO_SEARCH_URL, EUIPO_CLIENT_ID, _to_internal
    except ImportError:
        return []

    if not euipo_available():
        return []

    try:
        token = _get_access_token()
    except Exception as e:
        print(f"[EUIPO API] Token error: {e}")
        return []

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
        query = f"applicationDate=ge={date_from};applicationDate=le={date_to}"
        try:
            resp = requests.get(
                EUIPO_SEARCH_URL,
                headers=headers,
                params={"query": query, "size": 100, "page": page, "sort": "applicationDate,desc"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                print(f"[EUIPO API] {resp.status_code}: {resp.text[:200]}")
                break
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
            print(f"[EUIPO API] Error: {e}")
            break

    print(f"[EUIPO API] {len(all_marks)} marks for {target.isoformat()}")
    return all_marks


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_euipo_for_date(target: date) -> Tuple[List[Dict], dict]:
    """
    Returnează mărcile EUIPO din buletinul pentru data specificată.
    Încearcă mai întâi COPLA bulletin API; fallback pe EUIPO Search API.
    """
    processed = _load_processed()
    working   = _prev_working_day(target)
    slug      = _date_slug(working)

    info = {
        "slug":        slug,
        "target_date": target.isoformat(),
        "working_day": working.isoformat(),
    }

    # Încearcă COPLA bulletin
    bulletin = _find_bulletin_for_date(working)
    if bulletin:
        info["bulletin_id"]   = bulletin["id"]
        info["bulletin_date"] = bulletin["date"].isoformat()
        local = _download_bulletin(bulletin["value"], slug)
        if local:
            marks = _parse_bulletin_xml(local)
            info["status"] = "ok_bulletin"
            info["source"] = "copla_bulletin"
            info["marks"]  = len(marks)
            info["at"]     = datetime.utcnow().isoformat()
            processed[slug] = info
            _save_processed(processed)
            return marks, info
        # Bulletin found but download requires EUIPO portal authentication
        info["status"] = "auth_required"
        info["error"]  = (
            f"Buletinul EUIPO {bulletin['id']} ({bulletin['date'].isoformat()}) a fost identificat, "
            "dar descărcarea necesită sesiune autentificată în portalul EUIPO (OIDC/CAS). "
            "Monitorizarea continuă prin căutare TMview."
        )

    # Fallback: EUIPO Search API
    marks = _fetch_via_api(working)
    if marks:
        info["status"] = "ok_api"
        info["source"] = "api"
    else:
        if "status" not in info:
            info["status"] = "no_results"
            info["error"]  = "Buletin EUIPO: descărcarea necesită autentificare în portalul EUIPO. Configurați EUIPO_CLIENT_ID/SECRET pentru acces API."
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
