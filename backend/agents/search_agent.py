import asyncio
import os
import random
import re
import shlex
from typing import List, Dict, Tuple, Optional

# Optional HTTP/SOCKS5 proxy to bypass Imperva WAF on cloud hosts.
# Set PROXY_URL env var, e.g.: http://scraperapi:KEY@proxy-server.scraperapi.com:8001
_PROXY_URL = os.environ.get("PROXY_URL", "").strip()
_PROXIES = {"https": _PROXY_URL, "http": _PROXY_URL} if _PROXY_URL else None
if _PROXY_URL:
    print(f"[PROXY] Configured: {_PROXY_URL[:40]}...")
else:
    print("[PROXY] No proxy configured — direct connection")

try:
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

from agents.variant_agent import (build_input_list, build_phonetic_variants,
                                   build_plural_stem_variants, build_vowel_variants,
                                   build_abbreviation_variants,
                                   build_offices_and_territories, MAX_PAGES_PER_TERM)

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
        r = await session.get(
            TMVIEW_DETAIL.format(st13=st13),
            headers=_build_headers(),
            timeout=20,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        tm   = data.get("tradeMark", {})
        pubs = data.get("publication", [])
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
            "designatedCountries":   tm.get("designatedCountries", []),
            "applicants_detail":     data.get("applicants", []),
            "representatives":       data.get("representatives", []),
            "officeUrl":             data.get("officeUrl", ""),
        }
    except Exception:
        return {}


def _build_headers() -> Dict:
    """Construiește headers îmbogățiți cu sesiunea din browser dacă există."""
    hdrs = dict(HEADERS)
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
    try:
        r = await session.post(TMVIEW_URL, json=payload, headers=_build_headers(), timeout=55 if _PROXIES else 25)
        if r.status_code == 200:
            data  = r.json()
            marks = data.get("tradeMarks", [])
            for m in marks:
                m.setdefault("_found_by", term)
            return marks, int(data.get("total") or 0)
    except Exception:
        pass
    return [], 0


async def _search_term(session, term, nice_classes, offices, territories, criteria, seen):
    collected = []
    for page in range(1, MAX_PAGES_PER_TERM + 1):
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

async def _search_batched(session, term, nice_classes, offices, territories, crit, seen):
    """Caută cu împărțire automată în loturi dacă sunt multe teritorii."""
    collected = []
    if territories and len(territories) > TERRITORY_BATCH:
        batches = [territories[i:i+TERRITORY_BATCH]
                   for i in range(0, len(territories), TERRITORY_BATCH)]
    else:
        batches = [territories]

    for batch in batches:
        marks = await _search_term(session, term, nice_classes, offices, batch, crit, seen)
        collected.extend(marks)
        if batches.index(batch) < len(batches) - 1:
            await asyncio.sleep(0.15)
    return collected


async def _fetch_tmview(name: str, nice_classes: List[str], user_offices: List[str]) -> List[Dict]:
    offices, territories = build_offices_and_territories(user_offices)
    upper = name.upper().strip()

    # Criterii native TMview + wildcard substring
    # Z=Fuzzy, C=Conține, S=Începe cu, E=Se termină cu, F=Exact
    many_territories = territories and len(territories) > TERRITORY_BATCH

    if _PROXIES or many_territories:
        # Proxy sau multe teritorii: minim de request-uri pentru a evita timeout
        main_searches = [
            ("Z", upper),          # Fuzzy
            ("C", f"*{upper}*"),   # Wildcard substring
        ]
    else:
        # Direct, teritorii puține → toate cele 8 criterii
        main_searches = [
            ("Z", upper),
            ("C", upper),
            ("S", upper),
            ("E", upper),
            ("F", upper),
            ("C", f"*{upper}*"),
            ("C", f"{upper}*"),
            ("C", f"*{upper}"),
        ]

    phonetic_set = set(
        build_phonetic_variants(name) +
        build_vowel_variants(name) +
        build_plural_stem_variants(name)[:4]
    )
    phonetic_terms = [] if _PROXIES else list(phonetic_set)
    req_timeout = 55 if _PROXIES else 25

    async with AsyncSession(impersonate="chrome120", proxies=_PROXIES, verify=not bool(_PROXIES)) as session:
        if not _PROXIES and not has_browser_session():
            try:
                r = await session.get(TMVIEW_HOME, timeout=req_timeout)
                print(f"[TMVIEW] warmup GET status={r.status_code}")
            except Exception as e:
                print(f"[TMVIEW] warmup GET error: {type(e).__name__}: {e}")
            await asyncio.sleep(1)

        MAX_TOTAL = 100
        seen: set = set()
        all_marks: List[Dict] = []

        # Termenul principal
        for crit, term in main_searches:
            if len(all_marks) >= MAX_TOTAL:
                break
            marks = await _search_batched(session, term, nice_classes, offices, territories, crit, seen)
            all_marks.extend(marks)
            if len(all_marks) >= MAX_TOTAL:
                all_marks = all_marks[:MAX_TOTAL]
                break
            await asyncio.sleep(0.15)

        # Variante fonetice/vocale/plurale (fără batching — territoriile principale deja acoperite)
        phon_ter = territories[:TERRITORY_BATCH] if many_territories else territories
        for term in phonetic_terms:
            if len(all_marks) >= MAX_TOTAL:
                break
            marks = await _search_term(session, term, nice_classes, offices, phon_ter, "C", seen)
            for m in marks:
                m["_phonetic"] = True
            all_marks.extend(marks)
            if len(all_marks) >= MAX_TOTAL:
                all_marks = all_marks[:MAX_TOTAL]
                break
            await asyncio.sleep(0.15)

        # Fetch detalii în paralel
        detail_tasks = [_fetch_detail(session, m.get("ST13", "")) for m in all_marks]
        details = await asyncio.gather(*detail_tasks, return_exceptions=True)

        enriched = []
        for tm, detail in zip(all_marks, details):
            merged = dict(tm)
            if isinstance(detail, dict) and detail:
                merged.update(detail)
            enriched.append(merged)

        return enriched


def _demo_marks(name: str, nice_classes: List[str], offices: List[str]) -> List[Dict]:
    nc_ints = [int(c) for c in nice_classes if c.isdigit()]
    results = [dict(m) for m in DEMO_MARKS if any(c in m["niceClass"] for c in nc_ints)]
    for i in range(3):
        variant = name[:max(3, len(name) - i)] + ("S" * i)
        results.append({
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
        })
    return results


async def _fetch_tmview_expired(name: str, nice_classes: List[str], user_offices: List[str]) -> List[Dict]:
    offices, territories = build_offices_and_territories(user_offices)
    upper = name.upper().strip()
    # Folosim aceleași loturi ca la căutarea principală
    exp_searches = [("F", upper), ("C", f"*{upper}*"), ("Z", upper)]

    async with AsyncSession(impersonate="chrome120", proxies=_PROXIES, verify=not bool(_PROXIES)) as session:
        if not has_browser_session():
            await session.get(TMVIEW_HOME, timeout=20)
            await asyncio.sleep(1)

        MAX_TOTAL = 50
        seen: set = set()
        all_marks: List[Dict] = []

        # Împărțim teritoriile în loturi (fix: teritoriile lipseau anterior)
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
                                           headers=_build_headers(), timeout=20)
                    if r.status_code == 200:
                        for m in r.json().get("tradeMarks", []):
                            st13 = m.get("ST13", "")
                            if st13 and st13 not in seen:
                                seen.add(st13)
                                m.setdefault("_found_by", term)
                                all_marks.append(m)
                except Exception:
                    pass
                await asyncio.sleep(0.15)

        return all_marks[:MAX_TOTAL]


class SearchAgent:
    async def search(self, name: str, nice_classes: List[str], offices: List[str],
                     extra_terms: Optional[List[str]] = None) -> Tuple[List[Dict], str]:
        if not HAS_CURL_CFFI:
            return _demo_marks(name, nice_classes, offices), "demo (curl-cffi lipsă)"
        _timeout = 75.0 if _PROXIES else 45.0
        _retries = 1 if _PROXIES else 2
        for attempt in range(_retries):
            try:
                if attempt > 0:
                    await asyncio.sleep(3)
                marks = await asyncio.wait_for(
                    _fetch_tmview(name, nice_classes, offices),
                    timeout=_timeout
                )
                if marks:
                    return marks, "live:tmview"
            except asyncio.TimeoutError:
                print(f"[TMVIEW] attempt {attempt}: TimeoutError")
            except Exception as e:
                err = str(e)
                print(f"[TMVIEW] attempt {attempt}: {type(e).__name__}: {err}")
                if "56" in err or "Connection" in err or "reset" in err.lower():
                    break
        return _demo_marks(name, nice_classes, offices), "demo (TMview indisponibil — date demonstrative)"

    async def search_expired(self, name: str, nice_classes: List[str],
                             offices: List[str]) -> Tuple[List[Dict], str]:
        if not HAS_CURL_CFFI:
            return [], "demo"
        try:
            marks = await asyncio.wait_for(
                _fetch_tmview_expired(name, nice_classes, offices),
                timeout=40.0,
            )
            return marks, "live:tmview:expired"
        except Exception:
            return [], "error"
