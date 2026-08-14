import asyncio
import base64
import json
import os
import random
import re
import shlex
from datetime import date
import requests as _std_requests
from typing import List, Dict, Tuple, Optional

# TMDN API credentials (HTTP Basic Auth) - optional, from environment
_TMDN_API_KEY = os.environ.get("TMDN_API_KEY", "")
_TMDN_API_SECRET = os.environ.get("TMDN_API_SECRET", "")

# Proxy: doar PROXY_URL/PROXY_URLS manual configurate (ScraperAPI blocat de TMview — 499)
_proxy_list_raw = os.environ.get("PROXY_URLS", "") or os.environ.get("PROXY_URL", "")
_PROXY_LIST: List[str] = [p.strip() for p in _proxy_list_raw.split(",") if p.strip()]
_PROXY_URL  = _PROXY_LIST[0] if _PROXY_LIST else ""
_PROXIES    = {"https": _PROXY_URL, "http": _PROXY_URL} if _PROXY_URL else None
if _PROXY_LIST:
    print(f"[PROXY] {len(_PROXY_LIST)} proxy(s) configured. First: {_PROXY_URL[:40]}...")
else:
    print("[PROXY] No proxy configured — direct connection")
print(f"[TMDN] API credentials configured: {_TMDN_API_KEY[:8]}...:{_TMDN_API_SECRET[:8]}...")


def _make_proxies(url: str) -> dict:
    return {"https": url, "http": url}


def _is_expired_mark(mark: Dict) -> bool:
    status_raw = str(mark.get("status") or mark.get("tradeMarkStatus") or "").lower()
    if any(w in status_raw for w in (
        "expir", "lapsed", "cancelled", "refused", "withdrawn",
        "surrendered", "invalidated", "abandoned",
    )):
        return True

    exp_str = str(mark.get("expiryDate") or "")
    if exp_str and str(mark.get("expiryIsReal", True)).lower() not in ("false", "0", "none", ""):
        try:
            return date.fromisoformat(exp_str[:10]) < date.today()
        except Exception:
            return False
    return False


def _unique_terms(terms: Optional[List[str]]) -> List[str]:
    seen = set()
    result = []
    for term in terms or []:
        cleaned = (term or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


async def _try_with_proxies(coro_factory):
    """Încearcă corutina cu fiecare proxy din listă. Dacă toate eșuează, încearcă direct."""
    candidates = _PROXY_LIST + [""]   # "" = conexiune directă ca fallback
    for proxy_url in candidates:
        try:
            result = await coro_factory(proxy_url)
            if result:
                return result
        except Exception as e:
            print(f"[PROXY] Failed with {'direct' if not proxy_url else proxy_url[:30]}: {e}")
    return []

try:
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

from agents.variant_agent import (build_input_list, build_phonetic_variants,
                                   build_plural_stem_variants, build_vowel_variants,
                                   build_abbreviation_variants,
                                   build_offices_and_territories, MAX_PAGES_PER_TERM)
from agents.euipo_agent import euipo_available, search_euipo

TMVIEW_URL    = "https://www.tmdn.org/tmview/api/search/results?translate=true"
TMVIEW_DETAIL = "https://www.tmdn.org/tmview/api/trademark/detail/{st13}"
TMVIEW_HOME   = "https://www.tmdn.org/tmview/"

FIELDS = [
    "ST13", "markImageURI", "tmName", "tmOffice",
    "applicationNumber", "registrationNumber", "applicationDate", "tradeMarkStatus",
    "niceClass", "applicantName",
]

HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept":       "application/json",
    "Origin":       "https://www.tmdn.org",
    "Referer":      "https://www.tmdn.org/tmview/",
}

# Sesiune browser importată (cookie + headers din cURL copiat de user)
_browser_session: Dict = {}

_SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".session.json")


def _save_session(data: Dict) -> None:
    try:
        with open(_SESSION_FILE, "w") as _f:
            json.dump(data, _f)
    except Exception as _e:
        print(f"[SESSION] Save failed: {_e}")


def _load_session() -> None:
    global _browser_session
    try:
        if os.path.exists(_SESSION_FILE):
            with open(_SESSION_FILE) as _f:
                data = json.load(_f)
            if data.get("cookies") or data.get("headers"):
                _browser_session = data
                print("[SESSION] Restored from disk")
    except Exception as _e:
        print(f"[SESSION] Load failed: {_e}")


_load_session()


def parse_curl(curl_text: str) -> Dict:
    """Extrage cookie-uri și headers dintr-un cURL copiat din DevTools."""
    headers = {}
    cookies = {}

    # Normalizare: elimină backslash-newline
    text = curl_text.replace("\\\n", " ").replace("\\\r\n", " ").strip()

    # Extrage toate -H / --header
    for m in re.finditer(r"""-H\s+['"]([^'"]+)['"]""", text):
        raw = m.group(1)
        if ":" in raw:
            k, _, v = raw.partition(":")
            k = k.strip().lower()
            v = v.strip()
            if k == "cookie":
                for part in v.split(";"):
                    part = part.strip()
                    if "=" in part:
                        ck, _, cv = part.partition("=")
                        cookies[ck.strip()] = cv.strip()
            else:
                headers[k] = v

    return {"headers": headers, "cookies": cookies}


def set_browser_session(curl_text: str) -> bool:
    """Setează sesiunea din cURL. Returnează True dacă a găsit cookie-uri."""
    global _browser_session
    parsed = parse_curl(curl_text)
    if parsed["cookies"] or parsed["headers"]:
        _browser_session = parsed
        _save_session(parsed)
        _cb_reset()  # sesiune nouă = resetăm circuit breaker-ul
        return True
    return False


def has_browser_session() -> bool:
    return bool(_browser_session.get("cookies") or _browser_session.get("headers"))

DEMO_MARKS = [
    {"ST13":"DEMO001","tmName":"MUSCLE SAUCE","tmOffice":"WO","tradeMarkStatus":"Registered",
     "niceClass":[30],"applicantName":["Muscle Sauce Pty Ltd"],
     "applicationDate":"2024-03-21T12:00:00.000Z","applicationNumber":"1786830",
     "registrationDate":"2024-03-21T12:00:00.000Z","expiryDate":"2034-03-21T12:00:00.000Z",
     "markImageURI":None,
     "goodAndServices":[{"niceClass":"30","goodsAndServices":"Sauces; barbecue sauce; ketchup."}]},
    {"ST13":"DEMO002","tmName":"MUSCL SAUCE","tmOffice":"RO","tradeMarkStatus":"Filed",
     "niceClass":[30],"applicantName":["Prod RO SA"],
     "applicationDate":"2021-07-22T12:00:00.000Z","applicationNumber":"M2021009",
     "registrationDate":None,"expiryDate":None,"markImageURI":None,
     "goodAndServices":[{"niceClass":"30","goodsAndServices":"Sauces; condiments."}]},
    {"ST13":"DEMO003","tmName":"MUSCLES SAUCE","tmOffice":"DE","tradeMarkStatus":"Registered",
     "niceClass":[29,30],"applicantName":["GmbH Foods"],
     "applicationDate":"2018-11-05T12:00:00.000Z","applicationNumber":"DE30201800123",
     "registrationDate":"2019-04-01T12:00:00.000Z","expiryDate":"2029-04-01T12:00:00.000Z",
     "markImageURI":None,
     "goodAndServices":[
         {"niceClass":"29","goodsAndServices":"Meat; fish; dairy products."},
         {"niceClass":"30","goodsAndServices":"Sauces; condiments; mustard."}]},
]


async def _fetch_detail(session: "AsyncSession", st13: str) -> Dict:
    if not st13 or st13.startswith("DEMO"):
        return {}
    try:
        r = await asyncio.wait_for(
            session.get(
                TMVIEW_DETAIL.format(st13=st13),
                headers=_build_headers(),
                timeout=3,
            ),
            timeout=4,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        tm   = data.get("tradeMark", {})
        pubs = data.get("publication", [])

        # Detectăm câmpul corect pentru țările desemnate (Madrid)
        if st13.startswith("WO"):
            print(f"[DETAIL-WO] {st13} tm_keys={list(tm.keys())} root_keys={list(data.keys())}")
        designated = (
            tm.get("designatedCountries")
            or tm.get("designatedOffices")
            or tm.get("territories")
            or data.get("designatedCountries")
            or data.get("designatedOffices")
            or []
        )
        # Normalizăm la listă de stringuri
        if designated and isinstance(designated[0], dict):
            designated = [d.get("code") or d.get("name") or str(d) for d in designated]
        # Fallback: designationUnderMadridProtocol e un string "BR-CA-EM-GB-..."
        if not designated:
            madrid_str = tm.get("designationUnderMadridProtocol", "")
            if madrid_str:
                designated = [c.strip() for c in madrid_str.split("-") if c.strip()]

        return {
            "goodAndServices":       tm.get("goodAndServices", []),
            "registrationDate":      (tm.get("codeRegistrationDate") or "")[:10],
            "expiryDate":            (tm.get("expiryDate") or "")[:10],
            "applicationNumber":     tm.get("applicationNumber", ""),
            "publicationDate":       (pubs[0].get("date", "") or "")[:10] if pubs else "",
            "markCurrentStatusCode": tm.get("markCurrentStatusCode", ""),
            "markCurrentStatusDate": (tm.get("markCurrentStatusDate") or "")[:10],
            "markFeature":           tm.get("markFeature", ""),
            "kindMark":              tm.get("kindMark", ""),
            "oppositionStartDate":   (tm.get("oppositionStartDate") or "")[:10],
            "oppositionEndDate":     (tm.get("oppositionEndDate") or "")[:10],
            "viennaCodes":           [v.get("code", "") for v in data.get("viennaCodes", [])],
            "designatedCountries":   designated,
            "applicants_detail":     data.get("applicants", []),
            "representatives":       data.get("representatives", []),
            "officeUrl":             data.get("officeUrl", ""),
        }
    except Exception:
        return {}


def _build_headers() -> Dict:
    """Construiește headers îmbogățiți cu sesiunea din browser și autentificarea TMDN."""
    hdrs = dict(HEADERS)
    
    # Adaugă autentificarea HTTP Basic Auth pentru TMDN API
    if _TMDN_API_KEY and _TMDN_API_SECRET:
        credentials = f"{_TMDN_API_KEY}:{_TMDN_API_SECRET}"
        encoded = base64.b64encode(credentials.encode()).decode()
        hdrs["Authorization"] = f"Basic {encoded}"
    
    if has_browser_session():
        cookies = _browser_session.get("cookies", {})
        if cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        # Preluăm și User-Agent / Accept-Language din sesiunea browser
        for key in ("user-agent", "accept-language", "accept"):
            val = _browser_session.get("headers", {}).get(key)
            if val:
                hdrs[key] = val
    return hdrs


async def _search_page(session, term, nice_classes, offices, territories, criteria, page):
    payload = {
        "page": str(page), "pageSize": "30", "criteria": criteria,
        "basicSearch": term, "newPage": (page == 1),
        "fields": FIELDS,
    }
    if offices:      payload["offices"]     = offices
    if territories:  payload["territories"] = territories
    if nice_classes: payload["niceClass"]   = [int(c) if c.isdigit() else c for c in nice_classes]
    if _cb_is_open():
        return [], 0
    try:
        r = await session.post(TMVIEW_URL, json=payload, headers=_build_headers(), timeout=55 if _PROXIES else 10)
        print(f"[TMVIEW] POST status={r.status_code} crit={criteria} term={term[:20]} offices={sorted(offices)} territories={sorted(territories)}")
        if r.status_code == 200:
            # Check content-type before parsing — Imperva returns text/html (200) when blocking.
            # Do NOT count Imperva blocks as circuit-breaker failures (they're IP-level, not connection errors).
            ct = r.headers.get("content-type", "")
            body = r.text
            if not body.strip() or "json" not in ct.lower():
                print(f"[TMVIEW] IMPERVA BLOCK — ct={ct!r} body_len={len(body)}")
                return [], 0
            # Check if response is actually JSON (not HTML/redirect)
            try:
                data  = r.json()
            except ValueError:
                resp_preview = body[:100].lower()
                if "html" in resp_preview or "<!doctype" in resp_preview or "302" in str(r.status_code):
                    print(f"[TMVIEW] HTML response (possible IP ban/redirect): {resp_preview}")
                    return [], 0
                raise
            _cb_record_success()
            marks = data.get("tradeMarks", [])
            print(f"[TMVIEW] found {len(marks)} marks")
            for m in marks:
                m.setdefault("_found_by", term)
            return marks, int(data.get("totalResults") or data.get("total") or 0)
        elif r.status_code == 499:
            _cb_record_failure()
            print(f"[TMVIEW] 499 — IP partajat detectat (ScraperAPI datacenter ban)")
        elif r.status_code in (429, 503):
            _cb_record_failure()
            print(f"[TMVIEW] Rate-limit {r.status_code} — aștept 10s")
            await asyncio.sleep(10)
    except Exception as _e:
        err_str = str(_e).lower()
        if any(w in err_str for w in ("connection reset", "connection refused", "ssl", "json",
                                       "timed out", "timeout", "connection timed")):
            _cb_record_failure()
        print(f"[TMVIEW] _search_page error: {type(_e).__name__}: {_e}")
    return [], 0


async def _search_term(session, term, nice_classes, offices, territories, criteria, seen, max_pages=MAX_PAGES_PER_TERM):
    collected = []
    for page in range(1, max_pages + 1):
        marks, total = await _search_page(
            session, term, nice_classes, offices, territories, criteria, page)
        for m in marks:
            st13 = m.get("ST13", "")
            if st13 and st13 not in seen:
                seen.add(st13)
                collected.append(m)
        if not marks or len(marks) < 30 or page * 30 >= total:
            break
    return collected


TERRITORY_BATCH = 7   # teritorii per request — evită WAF blocking

# ── Circuit breaker ──────────────────────────────────────────────────────────
# Dacă TMview resetează conexiunea de N ori consecutiv, oprim requests automat
_cb_failures   = 0        # erori consecutive curente
_CB_THRESHOLD  = 3        # 3 erori consecutive = TMview ban, revino mai târziu
_cb_open       = False    # True = circuit deschis (requests oprite)

def _cb_record_success():
    global _cb_failures, _cb_open
    _cb_failures = 0
    _cb_open     = False
    print("[CIRCUIT BREAKER] Reset — conexiune TMview OK")

def _cb_record_failure():
    global _cb_failures, _cb_open
    _cb_failures += 1
    if _cb_failures >= _CB_THRESHOLD:
        _cb_open = True
        print(f"[CIRCUIT BREAKER] DESCHIS — TMview indisponibil, revin la demo marks")

def _cb_is_open() -> bool:
    return _cb_open

def _cb_reset():
    """Reseteaza manual circuit breaker daca vrei sa reincerci TMview."""
    global _cb_failures, _cb_open
    _cb_failures = 0
    _cb_open = False
    print("[CIRCUIT BREAKER] Manual reset — gata pentru reincercare")
# ─────────────────────────────────────────────────────────────────────────────

async def _search_batched(session, term, nice_classes, offices, territories, crit, seen, max_pages=5):
    """Caută cu împărțire automată în loturi dacă sunt multe teritorii."""
    collected = []
    if territories and len(territories) > TERRITORY_BATCH:
        batches = [territories[i:i+TERRITORY_BATCH]
                   for i in range(0, len(territories), TERRITORY_BATCH)]
    else:
        batches = [territories]

    for idx, batch in enumerate(batches):
        if _cb_is_open():
            print("[CIRCUIT BREAKER] Request omis — circuit deschis.")
            break
        if len(collected) >= 100:  # MAX_TOTAL e local in _fetch_tmview
            break
        marks = await _search_term(session, term, nice_classes, offices, batch, crit, seen,
                                   max_pages=max_pages)
        collected.extend(marks)
        if idx < len(batches) - 1:
            await asyncio.sleep(0.2)
    return collected


async def _fetch_tmview(name: str, nice_classes: List[str], user_offices: List[str], proxy_url: str = _PROXY_URL, include_expired: bool = True, extra_terms: Optional[List[str]] = None, wildcard_patterns: Optional[List[str]] = None) -> List[Dict]:
    if _cb_is_open():
        print("[CIRCUIT BREAKER] Circuit deschis — TMview requests oprite")
        return None  # Return None to signal unavailability (not empty list)

    offices, territories = build_offices_and_territories(user_offices)

    upper = name.upper().strip()

    many_territories = territories and len(territories) > TERRITORY_BATCH

    use_proxy = bool(proxy_url)
    proxies   = _make_proxies(proxy_url) if use_proxy else None

    # Pentru many_territories nu trimitem offices — batchurile de teritorii sunt suficiente
    # și trimiterea a 20+ coduri de offices provoacă 400 PARAMETER_INCORRECT_FORMAT în TMview.
    effective_offices = [] if many_territories else offices

    if use_proxy or many_territories:
        main_searches = [("E", upper), ("C", f"*{upper}*")]
    else:
        main_searches = [
            ("E", upper), ("F", upper),
            ("C", f"*{upper}*"), ("C", f"{upper}*"),
        ]

    all_phonetic = list(set(
        build_phonetic_variants(name) + build_vowel_variants(name) +
        build_plural_stem_variants(name)[:4]
    ))
    phonetic_terms = all_phonetic[:2] if (use_proxy or many_territories) else all_phonetic[:4]
    req_timeout = 55 if use_proxy else 6
    _extra_pool = [("C", term) for term in _unique_terms(extra_terms) if term.upper() != upper]
    extra_searches = [] if many_territories else _extra_pool[:4]

    async with AsyncSession(impersonate="chrome120", proxies=proxies, verify=not use_proxy) as session:
        if not has_browser_session():
            try:
                r = await session.get(TMVIEW_HOME, timeout=req_timeout)
                print(f"[TMVIEW] warmup GET status={r.status_code} proxy={'yes' if use_proxy else 'no'}")
            except Exception as e:
                print(f"[TMVIEW] warmup GET error: {type(e).__name__}: {e}")
            await asyncio.sleep(0.5)

        MAX_TOTAL = 100
        seen: set = set()
        all_marks: List[Dict] = []

        base_delay = 1.5 if use_proxy else 1.0

        for crit, term in main_searches:
            if len(all_marks) >= MAX_TOTAL or _cb_is_open():
                break
            marks = await _search_batched(session, term, nice_classes, effective_offices, territories, crit, seen,
                                               max_pages=(1 if many_territories else MAX_PAGES_PER_TERM))
            all_marks.extend(marks)
            await asyncio.sleep(random.uniform(0.15, 0.4))

        for crit, term in extra_searches:
            if len(all_marks) >= MAX_TOTAL or _cb_is_open():
                break
            marks = await _search_batched(session, term, nice_classes, effective_offices, territories, crit, seen,
                                               max_pages=(1 if many_territories else MAX_PAGES_PER_TERM))
            all_marks.extend(marks)
            await asyncio.sleep(random.uniform(0.15, 0.4))

        all_marks = all_marks[:MAX_TOTAL]

        # Variante fonetice — sărite complet pentru many_territories (27 state);
        # fiecare term folosea implicit MAX_PAGES_PER_TERM=5 pagini → depășea timeout-ul de 45s.
        if not many_territories:
            phon_ter = territories if not use_proxy else territories[:TERRITORY_BATCH]
            for term in phonetic_terms:
                if len(all_marks) >= MAX_TOTAL or _cb_is_open():
                    break
                marks = await _search_term(session, term, nice_classes, effective_offices, phon_ter, "C", seen,
                                           max_pages=2)
                for m in marks:
                    m["_phonetic"] = True
                all_marks.extend(marks)
                await asyncio.sleep(random.uniform(base_delay, base_delay + 0.8))
            all_marks = all_marks[:MAX_TOTAL]

        # Wildcard patterns cu poziții specifice → marcate ca "Risc Ridicat"
        if wildcard_patterns and not many_territories:
            wildcard_ter = territories if not use_proxy else territories[:TERRITORY_BATCH]
            for term in wildcard_patterns:
                if len(all_marks) >= MAX_TOTAL or _cb_is_open():
                    break
                marks = await _search_term(session, term, nice_classes, effective_offices, wildcard_ter, "C", seen,
                                           max_pages=2)
                for m in marks:
                    m["_risk_high"] = True  # Marchez ca risc ridicat
                all_marks.extend(marks)
                await asyncio.sleep(random.uniform(base_delay, base_delay + 1.0))
            all_marks = all_marks[:MAX_TOTAL]

        if not include_expired:
            all_marks = [m for m in all_marks if not _is_expired_mark(m)]

        # Returnează marci fără detail fetching (details pot fi fetched on-demand)
        return all_marks


def _demo_marks(name: str, nice_classes: List[str], offices: List[str]) -> List[Dict]:
    nc_ints = [int(c) for c in nice_classes if c.isdigit()]
    results = [dict(m) for m in DEMO_MARKS if any(c in m["niceClass"] for c in nc_ints)]
    for i in range(3):
        variant = name[:max(3, len(name) - i)] + ("S" * i)
        mark = {
            "ST13": f"DEMO_GEN_{i}", "tmName": variant,
            "tmOffice": (offices[i % len(offices)] if offices else "EM"),
            "tradeMarkStatus": random.choice(["Registered", "Filed"]),
            "niceClass": nc_ints[:1] or [30],
            "applicantName": [f"Test Company {i+1} SRL"],
            "applicationDate": "2022-01-01T12:00:00.000Z",
            "registrationDate": "2023-01-01T12:00:00.000Z",
            "expiryDate": "2033-01-01T12:00:00.000Z",
            "applicationNumber": f"TST{i:04d}", "markImageURI": None,
            "goodAndServices": [{"niceClass": str(nc_ints[0] if nc_ints else 30),
                                  "goodsAndServices": "Test products for demonstration."}],
        }
        # Marchez al doilea mark (i=1) cu risc ridicat pentru test
        if i == 1:
            mark["_risk_high"] = True
        results.append(mark)
    return results





async def _fetch_tmview_expired(name: str, nice_classes: List[str], user_offices: List[str]) -> List[Dict]:
    offices, territories = build_offices_and_territories(user_offices)

    # Aceleași optimizări proxy ca în _fetch_tmview
    if _PROXIES and "EM" in territories:
        from agents.variant_agent import ALL_EU_TERRITORIES
        territories = [t for t in territories if t not in ALL_EU_TERRITORIES and t != "EM"]
        offices = list(set(offices) | {"EM"})

    upper = name.upper().strip()
    exp_searches = [("E", upper), ("C", f"*{upper}*")] if _PROXIES else [("F", upper), ("C", f"*{upper}*"), ("E", upper)]
    req_timeout = 55 if _PROXIES else 20

    async with AsyncSession(impersonate="chrome120", proxies=_PROXIES, verify=not bool(_PROXIES)) as session:
        if not _PROXIES and not has_browser_session():
            await session.get(TMVIEW_HOME, timeout=20)
            await asyncio.sleep(1)

        MAX_TOTAL = 50
        seen: set = set()
        all_marks: List[Dict] = []

        ter_batches = ([territories[i:i+TERRITORY_BATCH]
                        for i in range(0, len(territories), TERRITORY_BATCH)]
                       if territories and len(territories) > TERRITORY_BATCH
                       else [territories])

        for crit, term in exp_searches:
            if len(all_marks) >= MAX_TOTAL:
                break
            for batch in ter_batches:
                if len(all_marks) >= MAX_TOTAL:
                    break
                payload = {
                    "page": "1", "pageSize": "30", "criteria": crit,
                    "basicSearch": term, "newPage": True, "fields": FIELDS,
                    "tmStatus": ["Expired", "Lapsed", "Cancelled", "Abandoned",
                                 "Invalidated", "Withdrawn", "Refused"],
                }
                if offices: payload["offices"]     = offices
                if batch:   payload["territories"] = batch
                if nice_classes:
                    payload["niceClass"] = [int(c) if c.isdigit() else c for c in nice_classes]
                try:
                    r = await session.post(TMVIEW_URL, json=payload,
                                           headers=_build_headers(), timeout=req_timeout)
                    if r.status_code == 200:
                        for m in r.json().get("tradeMarks", []):
                            st13 = m.get("ST13", "")
                            if st13 and st13 not in seen:
                                seen.add(st13)
                                m.setdefault("_found_by", term)
                                all_marks.append(m)
                except Exception as _e:
                    print(f"[TMVIEW] POST error: {type(_e).__name__}: {_e}")
                await asyncio.sleep(0.15)

        return all_marks[:MAX_TOTAL]


class SearchAgent:
    async def search(self, name: str, nice_classes: List[str], offices: List[str],
                     extra_terms: Optional[List[str]] = None,
                     wildcard_patterns: Optional[List[str]] = None,
                     include_expired: bool = True) -> Tuple[List[Dict], str]:
        if not HAS_CURL_CFFI:
            marks = _demo_marks(name, nice_classes, offices)
            if not include_expired:
                marks = [m for m in marks if not _is_expired_mark(m)]
            return marks, "demo (curl-cffi lipsă)"

        def _merge_marks(primary: List[Dict], secondary: List[Dict]) -> List[Dict]:
            merged = []
            seen = set()

            for mark in primary + secondary:
                key = mark.get("ST13") or mark.get("applicationNumber") or mark.get("tmName") or ""
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                merged.append(mark)

            return merged

        # \u00cencearc\u0103 EUIPO API primul (rapid, f\u0103r\u0103 blocaje Imperva)
        if euipo_available():
            try:
                loop = asyncio.get_event_loop()
                euipo_marks = await asyncio.wait_for(
                    loop.run_in_executor(None, search_euipo, name, nice_classes),
                    timeout=20.0
                )
                if not include_expired:
                    euipo_marks = [m for m in euipo_marks if not _is_expired_mark(m)]
                print(f"[EUIPO] Primary search: {len(euipo_marks)} marks")
                return euipo_marks, "live:euipo"
            except asyncio.TimeoutError:
                print("[EUIPO] Primary search timeout \u2014 falling back to TMview")
            except Exception as e:
                print(f"[EUIPO] Primary search error: {type(e).__name__}: {e}")

        # 2. Incearca TMview direct (fara proxy - ScraperAPI e blocat de TMview)
        _cb_reset()
        try:
            marks = await asyncio.wait_for(
                _fetch_tmview(name, nice_classes, offices, proxy_url="",
                              include_expired=include_expired,
                              extra_terms=extra_terms,
                              wildcard_patterns=wildcard_patterns),
                timeout=60.0
            )
            if marks is not None and len(marks) > 0:
                print(f"[TMVIEW] direct success: {len(marks)} marks")
                if not include_expired:
                    marks = [m for m in marks if not _is_expired_mark(m)]
                return marks, "live:tmview"
        except asyncio.TimeoutError:
            print("[TMVIEW] direct timeout")
        except Exception as e:
            print(f"[TMVIEW] direct error: {type(e).__name__}: {e}")

        return _demo_marks(name, nice_classes, offices), "demo (TMview/EUIPO indisponibil - date demonstrative)"

    async def search_expired(self, name: str, nice_classes: List[str],
                             offices: List[str]) -> Tuple[List[Dict], str]:
        if not HAS_CURL_CFFI:
            return [], "demo"
        try:
            marks = await asyncio.wait_for(
                _fetch_tmview_expired(name, nice_classes, offices),
                timeout=75.0 if _PROXIES else 40.0,
            )
            return marks, "live:tmview:expired"
        except Exception:
            return [], "error"
