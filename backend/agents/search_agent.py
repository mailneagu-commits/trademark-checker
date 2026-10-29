import asyncio
import base64
import contextvars
import json
import os
import random
import re
import shlex
import time
from datetime import date
import requests as _std_requests
from typing import List, Dict, Tuple, Optional
from rapidfuzz import fuzz

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
        "surrendered", "invalidated", "abandoned", "ended",
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
                                   build_offices_and_territories, MAX_PAGES_PER_TERM,
                                   NON_EU_OFFICES)
from agents.euipo_agent import euipo_available, search_euipo

TMVIEW_URL    = "https://www.tmdn.org/tmview/api/search/results?translate=true"
TMVIEW_DETAIL = "https://www.tmdn.org/tmview/api/trademark/detail/{st13}"
TMVIEW_HOME   = "https://www.tmdn.org/tmview/"

FIELDS = [
    "ST13", "markImageURI", "tmName", "tmOffice",
    "applicationNumber", "registrationNumber", "applicationDate", "tradeMarkStatus",
    "markCurrentStatusCode", "niceClass", "applicantName",
]

HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept":       "application/json",
    "Origin":       "https://www.tmdn.org",
    "Referer":      "https://www.tmdn.org/tmview/",
}

# Sesiune browser importată (cookie + headers din cURL copiat de user)
_browser_session: Dict = {}

from paths import DATA_DIR

_SESSION_FILE = (
    os.path.join(DATA_DIR, ".session.json") if DATA_DIR
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".session.json")
)
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)


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


# Diagnostic pentru cererile de detaliu (ultimul export): motivele eșecurilor și durata celor reușite.
DETAIL_DIAG: Dict = {"fail": {}, "ok_s": []}


def _diag_fail(reason: str) -> None:
    DETAIL_DIAG["fail"][reason] = DETAIL_DIAG["fail"].get(reason, 0) + 1


async def _fetch_detail(session: "AsyncSession", st13: str) -> Dict:
    if not st13 or st13.startswith("DEMO"):
        return {}
    _t0 = time.monotonic()
    # Conexiunea spre TMview e adesea instabilă (verificat live, toată sesiunea) —
    # timeout-ul inițial de 3-4s era prea agresiv, ratând aproape orice cerere cu
    # o mică întârziere și lăsând CLASE NISA PRODUSE/SERVICII gol la export, în
    # tăcere (exceptions înghițite mai jos). Reîncercăm de 2 ori, cu timeout mai
    # generos, înainte să renunțăm la acest ST13.
    # Măsurat live (export NUROFEN): cererile reușite durau ~14s = prima încercare expirată la 12s
    # + reîncercare reușită în ~1s — TMview „înghite” des prima cerere. Renunțăm repede la prima
    # (5s) și mai încercăm de două ori, cu timeout tot mai generos.
    r = None
    _timeouts = (5, 8, 12)
    for _try, _tmo in enumerate(_timeouts):
        try:
            r = await asyncio.wait_for(
                session.get(
                    TMVIEW_DETAIL.format(st13=st13),
                    headers=_build_headers(),
                    timeout=_tmo,
                ),
                timeout=_tmo + 2,
            )
            break
        except Exception as _e:
            if _try < len(_timeouts) - 1:
                await asyncio.sleep(0.5)
                continue
            _diag_fail(type(_e).__name__)
            return {}
    try:
        if r is None or r.status_code != 200:
            _diag_fail(f"HTTP {r.status_code if r is not None else '-'}")
            return {}
        if "json" not in r.headers.get("content-type", "").lower():
            _diag_fail("non-JSON (Imperva?)")
            return {}
        data = r.json()
        DETAIL_DIAG["ok_s"].append(round(time.monotonic() - _t0, 1))
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
            "markImageURI":          tm.get("markImageURI") or data.get("markImageURI") or "",
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
            "applicants_detail": [
                {
                    "name":    a.get("organizationName") or a.get("fullName") or a.get("name") or "",
                    "address": a.get("fullAddress") or a.get("address") or "",
                    "country": a.get("nationalityCode") or a.get("incorporationCountryCode") or a.get("country") or "",
                }
                for a in data.get("applicants", [])
                if a.get("organizationName") or a.get("fullName") or a.get("name")
            ],
            "representatives":       [
                {
                    "fullName":    r.get("fullName") or r.get("name") or r.get("organizationName") or "",
                    "fullAddress": r.get("fullAddress") or r.get("address") or "",
                    "country":     r.get("country", ""),
                }
                for r in (data.get("representatives") or tm.get("representatives") or [])
            ],
            "officeUrl":             data.get("officeUrl", ""),
        }
    except Exception:
        return {}


# Cererile de detaliu aflate în curs, per ST13 — pregătirea exportului din fundal și cererile
# paginii (/api/tm-detail) pot cere aceeași marcă simultan; o singură cerere spre TMview.
_detail_inflight: Dict[str, "asyncio.Future"] = {}


async def fetch_detail_once(session, st13: str) -> Dict:
    fut = _detail_inflight.get(st13)
    if fut is not None:
        return await asyncio.shield(fut)
    fut = asyncio.ensure_future(_fetch_detail(session, st13))
    _detail_inflight[st13] = fut
    try:
        return await asyncio.shield(fut)
    finally:
        if _detail_inflight.get(st13) is fut and fut.done():
            _detail_inflight.pop(st13, None)
        elif _detail_inflight.get(st13) is fut:
            fut.add_done_callback(lambda _f, k=st13: _detail_inflight.pop(k, None))


async def enrich_marks_with_detail(marks: list, concurrency: int = 10) -> list:
    """Fetch and merge TMview detail for each mark. Best-effort: falls back to original on error.
    Detaliile se iau întâi din cache-ul din DB (export_cache, valabil 7 zile) — doar mărcile care
    lipsesc de acolo se cer la TMview, câte o dată per ST13, apoi se salvează pentru exporturile
    următoare (Word → PDF → Excel ale aceleiași căutări nu mai refac nimic)."""
    if not HAS_CURL_CFFI or not marks:
        return marks
    import export_cache
    loop = asyncio.get_event_loop()
    st13s = sorted({m.get("ST13", "") for m in marks
                    if m.get("ST13") and not m.get("ST13", "").startswith("DEMO")})
    details: Dict[str, Dict] = await loop.run_in_executor(None, export_cache.get_details, st13s)
    missing = [s for s in st13s if s not in details]
    print(f"[ENRICH] {len(st13s)} mărci unice: {len(details)} din cache, {len(missing)} de la TMview")

    if missing:
        fresh: Dict[str, Dict] = {}
        sem = asyncio.Semaphore(concurrency)

        async def _fetch_one(session, st13):
            try:
                async with sem:
                    d = await fetch_detail_once(session, st13)
                if d:
                    fresh[st13] = d
            except Exception:
                pass

        try:
            async with AsyncSession(impersonate="chrome120", proxies=_PROXIES,
                                    verify=not bool(_PROXIES)) as session:
                # Dacă acest "warmup" GET eșuează o singură dată, funcția întreagă
                # renunța tăcut — nici măcar marca cu cea mai bună șansă nu mai era
                # încercată. O reîncercare rapidă înainte de a renunța la toată
                # îmbogățirea (nu doar la o marcă individuală).
                for _try in range(2):
                    try:
                        await session.get(TMVIEW_HOME, timeout=10, headers=_build_headers())
                        break
                    except Exception:
                        if _try == 0:
                            await asyncio.sleep(1.5)
                            continue
                        raise
                await asyncio.wait_for(
                    asyncio.gather(*(_fetch_one(session, s) for s in missing), return_exceptions=True),
                    timeout=90,
                )
        except Exception as e:
            print(f"[ENRICH] TMview: {type(e).__name__}: {e} — continui cu ce am adus")
        if fresh:
            await loop.run_in_executor(None, export_cache.save_details, dict(fresh))
            details.update(fresh)

    def _merge(mark):
        detail = details.get(mark.get("ST13", ""))
        if not detail:
            return mark
        merged = {**mark, **detail}
        # applicants_detail has normalized {name, address, country}
        if detail.get("applicants_detail"):
            merged["applicants"] = detail["applicants_detail"]
        # Nu suprascriem goodAndServices deja populat (ex. printr-un fetch
        # de detaliu declanșat mai devreme din UI la expandarea cardului)
        # cu un răspuns gol dintr-o nouă încercare server-side eșuată sau
        # incompletă — TMview e adesea instabil la request individual.
        if mark.get("goodAndServices") and not detail.get("goodAndServices"):
            merged["goodAndServices"] = mark["goodAndServices"]
        # Completăm imaginea din detail doar dacă lipsea la căutare —
        # unele mărci inactive (expirate) nu au markImageURI în
        # rezultatul de search, dar detail-ul o poate avea.
        if not merged.get("imageUrl") and detail.get("markImageURI"):
            merged["imageUrl"] = detail["markImageURI"]
        merged.pop("markImageURI", None)
        return merged

    return [_merge(m) for m in marks]


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


# Contorul cererilor spre TMview din căutarea curentă (ok / eșuate / sărite din cauza bugetului de
# timp sau a circuit breaker-ului) — ca o căutare la care TMview a căzut pe parcurs să fie raportată
# ca incompletă, nu afișată ca și cum ar fi completă.
_req_stats_ctx: "contextvars.ContextVar[Optional[Dict]]" = contextvars.ContextVar("_req_stats_ctx", default=None)


def _stat(key: str) -> None:
    st = _req_stats_ctx.get()
    if st is not None:
        st[key] = st.get(key, 0) + 1


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
        _stat("skipped")
        return [], 0
    _dl = _deadline_ctx.get()
    if _dl is not None and time.monotonic() > _dl:
        _stat("skipped")
        return [], 0

    # O căutare completă face zeci de cereri individuale (criterii, pagini, variante
    # fonetice) — verificat live că fiecare cerere poate eșua tranzitoriu ("Connection
    # reset by peer") chiar și când conexiunea, per ansamblu, e funcțională (un test
    # izolat imediat după reușea). Fără reîncercare AICI, la nivel de cerere, o singură
    # flotare izolată deschide circuit breaker-ul (după 3 eșecuri consecutive) și
    # prăbușește toată căutarea — chiar dacă restul cererilor ar fi mers bine.
    _PAGE_RETRIES = 3
    for _try in range(_PAGE_RETRIES):
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
                    _cb_record_failure()
                    _stat("failed")
                    return [], 0
                # Check if response is actually JSON (not HTML/redirect)
                try:
                    data  = r.json()
                except ValueError:
                    resp_preview = body[:100].lower()
                    if "html" in resp_preview or "<!doctype" in resp_preview or "302" in str(r.status_code):
                        print(f"[TMVIEW] HTML response (possible IP ban/redirect): {resp_preview}")
                        _stat("failed")
                        return [], 0
                    raise
                _cb_record_success()
                _stat("ok")
                marks = data.get("tradeMarks", [])
                print(f"[TMVIEW] found {len(marks)} marks")
                for m in marks:
                    m.setdefault("_found_by", term)
                return marks, int(data.get("totalResults") or data.get("total") or 0)
            elif r.status_code == 499:
                _cb_record_failure()
                print(f"[TMVIEW] 499 — IP partajat detectat (ScraperAPI datacenter ban)")
                break
            elif r.status_code in (429, 503):
                _cb_record_failure()
                print(f"[TMVIEW] Rate-limit {r.status_code} — aștept 10s")
                await asyncio.sleep(10)
                break
            else:
                break
        except Exception as _e:
            err_str = str(_e).lower()
            _is_conn_err = any(w in err_str for w in (
                "connection reset", "connection refused", "connection aborted",
                "remote end closed", "recv failure", "ssl", "json",
                "timed out", "timeout", "connection timed",
            ))
            if _is_conn_err and _try < _PAGE_RETRIES - 1:
                print(f"[TMVIEW] eroare de conexiune, reîncerc rapid ({_try + 1}/{_PAGE_RETRIES}): "
                      f"{type(_e).__name__}: {_e}")
                await asyncio.sleep(1.5)
                continue
            if _is_conn_err:
                _cb_record_failure()
            print(f"[TMVIEW] _search_page error: {type(_e).__name__}: {_e}")
            break
    _stat("failed")
    return [], 0


# Câte mărci păstrăm per variantă de căutare (cele mai apropiate de numele căutat) — setat de
# _fetch_tmview pe durata unei căutări. None = fără limită (comportamentul vechi, paginat).
_rank_ctx: "contextvars.ContextVar[Optional[Tuple[str, int]]]" = contextvars.ContextVar("_rank_ctx", default=None)
# Momentul (time.monotonic) după care nu mai trimitem cereri noi în căutarea curentă — întoarcem
# ce am găsit până atunci, în loc să fie aruncat tot de un timeout exterior.
_deadline_ctx: "contextvars.ContextVar[Optional[float]]" = contextvars.ContextVar("_deadline_ctx", default=None)
TMVIEW_BUDGET_SECONDS = 50.0       # căutare normală
TMVIEW_BUDGET_SECONDS_EU = 80.0    # EU_FULL (26 teritorii, mai multe loturi)


def _relevance(query: str, mark: Dict) -> float:
    """Cât de aproape e marca de numele căutat — întreg, pe cuvinte, sau ca parte din nume."""
    cand = re.sub(r"[^A-Z0-9 ]", "", str(mark.get("tmName") or "").upper())
    if not cand:
        return 0.0
    words = [w for w in cand.split() if len(w) >= 3] or [cand]
    q_words = [w for w in query.split() if len(w) >= 3] or [query.replace(" ", "")]
    q_compact = query.replace(" ", "")
    return max(
        fuzz.ratio(q_compact, cand.replace(" ", "")),
        max(fuzz.ratio(q_compact, w) for w in words),
        # Marcă din mai multe cuvinte: un cuvânt identic/apropiat de unul din cuvintele căutate.
        0.95 * max(fuzz.ratio(qw, w) for qw in q_words for w in words),
        0.85 * fuzz.partial_ratio(q_compact, cand),
    )


async def _search_term(session, term, nice_classes, offices, territories, criteria, seen, max_pages=MAX_PAGES_PER_TERM):
    rank = _rank_ctx.get()
    if rank:
        # O singură pagină (30) per variantă, păstrăm cele mai relevante N — mai puține cereri
        # spre TMview (mai puține blocaje Imperva) și o listă mai puțin aglomerată.
        query, top_n = rank
        marks, _ = await _search_page(session, term, nice_classes, offices, territories, criteria, 1)
        fresh = [m for m in marks if m.get("ST13") and m["ST13"] not in seen]
        fresh.sort(key=lambda m: _relevance(query, m), reverse=True)
        kept = fresh[:top_n]
        seen.update(m["ST13"] for m in kept)
        return kept
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


TOP_PER_VARIANT = 7   # mărci păstrate per variantă de căutare (cele mai relevante)
# Verificat live 2026-10-13: TMview acceptă toate cele 26 de teritorii EU_FULL într-o cerere
# (200, JSON) — 1 cerere per variantă în loc de 4 loturi de câte 7.
TERRITORY_REQUEST_BATCH = 30
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
    was_open = _cb_open
    _cb_failures += 1
    if _cb_failures >= _CB_THRESHOLD:
        _cb_open = True
        print(f"[CIRCUIT BREAKER] DESCHIS — TMview indisponibil, revin la demo marks")
        if not was_open:
            _trigger_region_rotation("circuit breaker deschis (TMview blochează căutarea)")


def _trigger_region_rotation(reason: str) -> None:
    """Pornește rotirea automată a regiunii Railway, în fundal — nu blochează căutarea curentă."""
    try:
        import asyncio
        from region_rotate import rotate_region
        asyncio.get_event_loop().run_in_executor(None, rotate_region, reason)
    except Exception as e:
        print(f"[REGION-ROTATE] Nu am putut porni rotirea: {e}")

def _cb_is_open() -> bool:
    return _cb_open

def _cb_reset():
    """Reseteaza manual circuit breaker daca vrei sa reincerci TMview."""
    global _cb_failures, _cb_open
    _cb_failures = 0
    _cb_open = False
    print("[CIRCUIT BREAKER] Manual reset — gata pentru reincercare")
# ─────────────────────────────────────────────────────────────────────────────

async def _search_batched(session, term, nice_classes, offices, territories, crit, seen, max_pages=5, batch_delay=0.2):
    """Caută cu împărțire automată în loturi dacă sunt multe teritorii."""
    collected = []
    if territories and len(territories) > TERRITORY_REQUEST_BATCH:
        batches = [territories[i:i+TERRITORY_REQUEST_BATCH]
                   for i in range(0, len(territories), TERRITORY_REQUEST_BATCH)]
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
            await asyncio.sleep(batch_delay)
    rank = _rank_ctx.get()
    if rank and len(batches) > 1 and len(collected) > rank[1]:
        # Și pe mai multe loturi de teritorii: tot max N per variantă, cele mai relevante.
        collected.sort(key=lambda m: _relevance(rank[0], m), reverse=True)
        seen.difference_update(m["ST13"] for m in collected[rank[1]:])  # le pot găsi alte variante
        collected = collected[:rank[1]]
    return collected


WO_MAX_CANDIDATES = 60  # mărci WIPO verificate prin detail fetch pentru desemnarea teritoriului


async def _fetch_tmview(name: str, nice_classes: List[str], user_offices: List[str], proxy_url: str = _PROXY_URL, include_expired: bool = True, extra_terms: Optional[List[str]] = None, wildcard_patterns: Optional[List[str]] = None) -> List[Dict]:
    if _cb_is_open():
        print("[CIRCUIT BREAKER] Circuit deschis — TMview requests oprite")
        return None  # Return None to signal unavailability (not empty list)

    offices, territories = build_offices_and_territories(user_offices)

    upper = name.upper().strip()
    _rank_ctx.set((" ".join(re.split(r"[^A-Z0-9]+", upper)).strip(), TOP_PER_VARIANT))
    _deadline_ctx.set(time.monotonic() + (TMVIEW_BUDGET_SECONDS_EU if territories and len(territories) > TERRITORY_BATCH
                                          else TMVIEW_BUDGET_SECONDS))

    many_territories = territories and len(territories) > TERRITORY_BATCH

    use_proxy = bool(proxy_url)
    proxies   = _make_proxies(proxy_url) if use_proxy else None

    # Pentru many_territories nu trimitem offices — batchurile de teritorii sunt suficiente
    # și trimiterea a 20+ coduri de offices provoacă 400 PARAMETER_INCORRECT_FORMAT în TMview.
    effective_offices = [] if many_territories else offices

    # Variante fonetice plain (fără wildcard) pentru includere în main_searches cu criteriu E.
    # Asigură că KARTEZIAN → CARTEZIAN (K↔C) se caută la același nivel cu termenul original.
    _phon_plain = [t for t in build_phonetic_variants(name) if not t.startswith("*") and len(t) >= 3]

    if many_territories:
        # EU_FULL: E + F + C + prefix + fonetic (2 variante) — acoperire comparabilă cu normal
        main_searches = [("E", upper), ("F", upper), ("C", f"*{upper}*"), ("C", f"{upper}*")]
        for _pt in _phon_plain[:2]:
            main_searches.append(("F", _pt))
            main_searches.append(("C", f"*{_pt}*"))
    elif use_proxy:
        main_searches = [("E", upper), ("C", f"*{upper}*")]
        for _pt in _phon_plain[:1]:
            # E + F + C pentru varianta fonetică — F prinde și prefixe/parțiale (ex. CARTESIA din CARTEZIAN)
            main_searches.append(("E", _pt))
            main_searches.append(("F", _pt))
            main_searches.append(("C", f"*{_pt}*"))
    else:
        main_searches = [
            ("E", upper), ("F", upper),
            ("C", f"*{upper}*"), ("C", f"{upper}*"),
        ]
        for _pt in _phon_plain[:2]:
            main_searches.append(("E", _pt))
            main_searches.append(("F", _pt))
            main_searches.append(("C", f"*{_pt}*"))

    # Căutare fuzzy (F) pe bucăți din nume — F pe cuvântul întreg (UNIQORUM) nu găsește aproape
    # nimic la nume lungi, dar F pe UNIQO / QORUM / NIQORU aduce mărci apropiate pe care
    # fragmentele C (*QORUM*) nu le prind: MYQORZO, ACQUOR, EQUORA, CORUS, CUNIQUE, ANNIQUE.
    # Cuvintele mărcii (≥3 caractere). La mărci din mai multe cuvinte căutăm FIECARE cuvânt separat —
    # o marcă ce conține unul dintre cuvinte (ex. OCEAN din BLUE OCEAN PHARMA) trebuie găsită și
    # afișată. Înainte, fragmentele se tăiau peste cuvinte (*IA B*, *BLUE OCEAN P*) și cuvântul
    # singur (*OCEAN*) nu se căuta deloc.
    _words = [w for w in re.split(r"[^A-Z0-9]+", upper) if len(w) >= 3]
    _multiword = len(_words) >= 2
    _core_word = "".join(ch for ch in upper if ch.isalnum())

    def _fuzzy_parts_of(word: str) -> List[str]:
        parts: List[str] = []
        if len(word) >= 7:
            k = 5 if len(word) <= 9 else 6
            for part in (word[:k], word[-k:], word[1:-1][:6]):
                if part not in parts and part != word:
                    parts.append(part)
        return parts

    if _multiword:
        # Cuvintele lungi (mai distinctive) primele.
        for _w in sorted(_words, key=len, reverse=True):
            main_searches += [("E", _w), ("C", f"*{_w}*")]
            if len(_w) >= 5:
                main_searches.append(("F", _w))
                _wp = [t for t in build_phonetic_variants(_w) if not t.startswith("*")][:1]
                main_searches += [("C", f"*{t}*") for t in _wp]
            main_searches += [("F", p) for p in _fuzzy_parts_of(_w)[:2]]
    else:
        _fp = _fuzzy_parts_of(_core_word)
        # Căutare fuzzy (F) pe bucăți din nume — F pe cuvântul întreg (UNIQORUM) nu găsește aproape
        # nimic la nume lungi, dar F pe UNIQO / QORUM / NIQORU aduce mărci apropiate pe care
        # fragmentele C (*QORUM*) nu le prind: MYQORZO, ACQUOR, EQUORA, CORUS, CUNIQUE, ANNIQUE.
        main_searches += [("F", p) for p in (_fp[:2] if many_territories else _fp)]
    _seen_ms: set = set()
    main_searches = [x for x in main_searches if not (x in _seen_ms or _seen_ms.add(x))]

    all_phonetic = list(set(
        build_phonetic_variants(name) + build_vowel_variants(name) +
        build_plural_stem_variants(name)[:4]
    ))
    phonetic_terms = all_phonetic[:2] if (use_proxy or many_territories) else all_phonetic[:4]
    req_timeout = 55 if use_proxy else 6
    # Termeni extra (fragmente *UNIQO*, *QORUM*, *NIQOR*… și pattern-uri cu ? pentru o literă
    # diferită). Înainte se luau doar primii 4 — 3 dintre ei dublau căutările principale, deci
    # fragmentele de la mijloc/coadă nu se căutau niciodată (UNIQORUM: 128/128 rezultate doar
    # din *UNIQ*, zero din *QORUM*). Acum: fără duplicate, fragmente întâi, apoi pattern-urile ?.
    _main_terms = {t for _, t in main_searches}
    _pool = [t for t in _unique_terms(extra_terms) if t.upper() != upper and t not in _main_terms]
    if _multiword:
        # Fragmentele care trec peste granița dintre cuvinte (*IA B*, *E OCEAN PHARMA*) nu au sens —
        # păstrăm doar fragmentele dintr-un singur cuvânt.
        _pool = [t for t in _pool if " " not in t.strip("*?")]
    _fragments = [t for t in _pool if "?" not in t]
    _single_char = [t for t in _pool if "?" in t]
    # Toate ferestrele de 4 litere din nume și din variantele fonetice principale — prind mărci
    # ca CORUS/ACORUS (*CORU* din UNICORUM), KORU (*KORU*), ACQUOR/EQUORA (*QUOR* din UNIQUORUM),
    # pe care fragmentele de început/sfârșit nu le acoperă.
    _ngrams: List[str] = []
    _ngram_src = list(_words) if _multiword else [_core_word] + [p for p in _phon_plain[:3]]
    for _w in _ngram_src:
        _w = "".join(ch for ch in _w if ch.isalnum())
        for _i in range(len(_w) - 3) if len(_w) >= 6 else []:
            _g = f"*{_w[_i:_i + 4]}*"
            if _g not in _fragments and _g not in _ngrams and _g not in _main_terms:
                _ngrams.append(_g)
    # Fără pattern-urile ? (o literă diferită) — ~16 cereri care aduc foarte puțin și lungeau
    # căutarea; diferențele de o literă sunt prinse oricum de F (fuzzy) și de fragmente.
    extra_searches = [("C", t) for t in (_fragments + _ngrams)[:45]]

    async with AsyncSession(impersonate="chrome120", proxies=proxies, verify=not use_proxy) as session:
        if not has_browser_session():
            try:
                r = await session.get(TMVIEW_HOME, timeout=req_timeout)
                print(f"[TMVIEW] warmup GET status={r.status_code} proxy={'yes' if use_proxy else 'no'}")
            except Exception as e:
                print(f"[TMVIEW] warmup GET error: {type(e).__name__}: {e}")
            await asyncio.sleep(0.5)

        MAX_TOTAL = 800  # nume lungi + fragmente fuzzy: UNIQO singur atinge 400; scorul filtrează după
        seen: set = set()
        all_marks: List[Dict] = []

        base_delay = 1.5 if use_proxy else 0.15
        # EU_FULL: 0.5s batch delay (Imperva e cauzat de prefetch concurrent, nu de search sec.)
        # Cererile durează ~0.2s; pauzele mari de înainte (0.5-1.8s) consumau ~70% din timp.
        inter_batch_delay  = 0.2
        inter_search_delay = (0.15, 0.4)

        for crit, term in main_searches:
            if len(all_marks) >= MAX_TOTAL or _cb_is_open():
                break
            marks = await _search_batched(session, term, nice_classes, effective_offices, territories, crit, seen,
                                               max_pages=(1 if many_territories else MAX_PAGES_PER_TERM),
                                               batch_delay=inter_batch_delay)
            all_marks.extend(marks)
            await asyncio.sleep(random.uniform(*inter_search_delay))

        for crit, term in extra_searches:
            if len(all_marks) >= MAX_TOTAL or _cb_is_open():
                break
            # Fragmente scurte (*UNIQ*, *ORUM*) aduc sute de mărci puțin relevante — max 2 pagini,
            # ca să nu consume tot plafonul înaintea fragmentelor mai specifice.
            _core = term.replace("*", "").replace("?", "")
            _pages = 1 if many_territories else (2 if len(_core) <= 4 else MAX_PAGES_PER_TERM)
            marks = await _search_batched(session, term, nice_classes, effective_offices, territories, crit, seen,
                                               max_pages=_pages,
                                               batch_delay=inter_batch_delay)
            all_marks.extend(marks)
            await asyncio.sleep(random.uniform(*inter_search_delay))

        all_marks = all_marks[:MAX_TOTAL]

        # Mărci WIPO/Madrid cu teritoriu de destinație UE (EM) — căutare separată
        # necesară pentru many_territories deoarece effective_offices=[] acolo.
        if many_territories and "EM" in territories and not _cb_is_open():
            for crit, term in [("E", upper), ("C", f"*{upper}*")]:
                if len(all_marks) >= MAX_TOTAL or _cb_is_open():
                    break
                await asyncio.sleep(random.uniform(*inter_search_delay))
                marks = await _search_term(session, term, nice_classes, ["WO"], ["EM"], crit, seen, max_pages=2)
                all_marks.extend(marks)
            all_marks = all_marks[:MAX_TOTAL]

        # Mărci WIPO/Madrid pentru oficii non-UE (VN, CN, US etc.) — territories=[cod]
        # nu e valid în TMview pentru aceste țări (doar offices=[cod] funcționează,
        # vezi build_offices_and_territories), deci nu putem filtra direct ca la EM
        # mai sus. Căutăm global pe WO, apoi verificăm prin detail fetch care mărci
        # chiar desemnează țara cerută — altfel am afișa conflicte Madrid nelegate de ea.
        _non_eu_codes = [o for o in offices if o in NON_EU_OFFICES]
        if _non_eu_codes and not many_territories and not _cb_is_open():
            wo_seen: set = set()
            wo_candidates: List[Dict] = []
            # Aceleași variante ca la căutarea națională (exact, prefix, fonetice, wildcard,
            # termeni extra), altfel mărci Madrid apropiate (ex. ADEFAS, ADEFAZT) ar fi ratate.
            _wo_searches: List[tuple] = [("E", upper, None), ("C", f"*{upper}*", None), ("C", f"{upper}*", None)]
            for _pt in _phon_plain[:2]:
                _wo_searches.append(("E", _pt, "_phonetic"))
                _wo_searches.append(("C", f"*{_pt}*", "_phonetic"))
            _wo_searches += [("C", t, "_phonetic") for t in phonetic_terms]
            _wo_searches += [("C", t, "_risk_high") for t in (wildcard_patterns or [])]
            _wo_searches += [(c, t, None) for c, t in extra_searches]
            _wo_done: set = set()
            for crit, term, flag in _wo_searches:
                if (crit, term) in _wo_done:
                    continue
                _wo_done.add((crit, term))
                if len(wo_candidates) >= WO_MAX_CANDIDATES or _cb_is_open():
                    break
                marks = await _search_term(session, term, nice_classes, ["WO"], [], crit, wo_seen, max_pages=1)
                if flag:
                    for m in marks:
                        m[flag] = True
                wo_candidates.extend(marks)
                await asyncio.sleep(random.uniform(*inter_search_delay))
            wo_candidates = wo_candidates[:WO_MAX_CANDIDATES]
            if wo_candidates:
                _sem = asyncio.Semaphore(5)

                async def _check_designation(mark):
                    async with _sem:
                        detail = await _fetch_detail(session, mark.get("ST13", ""))
                    designated = {str(c).upper() for c in (detail.get("designatedCountries") or [])}
                    return mark if any(code in designated for code in _non_eu_codes) else None

                checked = await asyncio.gather(*[_check_designation(m) for m in wo_candidates])
                wo_marks = [m for m in checked if m]
                print(f"[TMVIEW] WIPO/Madrid suplimentar ({_non_eu_codes}): "
                      f"{len(wo_marks)}/{len(wo_candidates)} mărci desemnează teritoriul căutat")
                all_marks.extend(wo_marks)
                all_marks = all_marks[:MAX_TOTAL]

        # Variante fonetice
        if many_territories:
            # EU_FULL: fonetic pe teritoriile cheie (EM + marile naționale) — 1 batch, 2 termeni
            key_ter = [t for t in territories if t in {"EM", "BX", "DE", "FR", "IT", "ES", "PL"}][:TERRITORY_BATCH]
            for term in phonetic_terms[:2]:
                if len(all_marks) >= MAX_TOTAL or _cb_is_open():
                    break
                marks = await _search_term(session, term, nice_classes, [], key_ter, "C", seen, max_pages=2)
                for m in marks:
                    m["_phonetic"] = True
                all_marks.extend(marks)
                await asyncio.sleep(random.uniform(*inter_search_delay))
            all_marks = all_marks[:MAX_TOTAL]
        else:
            phon_ter = territories if not use_proxy else territories[:TERRITORY_BATCH]
            for term in phonetic_terms:
                if len(all_marks) >= MAX_TOTAL or _cb_is_open():
                    break
                marks = await _search_term(session, term, nice_classes, effective_offices, phon_ter, "C", seen,
                                           max_pages=2)
                for m in marks:
                    m["_phonetic"] = True
                all_marks.extend(marks)
                await asyncio.sleep(random.uniform(base_delay, base_delay + 0.25))
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
                await asyncio.sleep(random.uniform(base_delay, base_delay + 0.25))
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
                     include_expired: bool = True,
                     max_tmview_attempts: int = 3,
                     quality: Optional[Dict] = None) -> Tuple[List[Dict], str]:
        """`quality` (dict dat de apelant) se completează cu {ok, failed, skipped, partial, attempts}
        pentru căutarea TMview — partial=True înseamnă că o parte din cereri au eșuat sau au fost
        sărite, deci pot lipsi mărci din rezultat."""
        if quality is None:
            quality = {}
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

        # \u00cencearc\u0103 EUIPO API primul (rapid, f\u0103r\u0103 blocaje Imperva) \u2014 DOAR c\u00e2nd teritoriul
        # cerut e exclusiv EM. search_euipo() nu filtreaz\u0103 deloc dup\u0103 office/territory
        # (verificat: offices=["WO"] \u0219i offices=["EM"] dau exact acelea\u0219i rezultate,
        # etichetate mereu "EM") \u2014 deci nu poate servi un "WO" (Madrid) real, doar EM.
        # Pentru orice alt teritoriu (state individuale, EU_FULL, WO singur), TMview.
        _euipo_only = {o.upper() for o in offices} == {"EM"}
        if euipo_available() and _euipo_only:
            try:
                loop = asyncio.get_event_loop()
                euipo_marks = await asyncio.wait_for(
                    loop.run_in_executor(None, search_euipo, name, nice_classes),
                    timeout=35.0
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
        # Conectivitatea Railway→TMview e intermitentă — verificat live: aceeași
        # căutare poate eșua complet (timeout de conexiune) minute întregi și
        # reuși perfect la puțin timp distanță, fără nicio schimbare de cod.
        # Reîncercăm necondiționat (nu doar la eșecuri "rapide" — verificat live
        # că eșecurile reale pot dura 24-80+ secunde) până la max_tmview_attempts,
        # cu pauză progresivă între încercări (4s, 8s, 16s, 30s, apoi plafonat).
        _, _ter_preview = build_offices_and_territories(offices)
        # Plasă de siguranță peste bugetul intern (_deadline_ctx) — _fetch_tmview se oprește singur
        # la buget și întoarce ce a găsit; timeout-ul de aici ar arunca tot.
        _tmview_timeout = (TMVIEW_BUDGET_SECONDS_EU if len(_ter_preview) > TERRITORY_BATCH else TMVIEW_BUDGET_SECONDS) + 40.0
        _MAX_ATTEMPTS = max(1, max_tmview_attempts)
        _backoff_schedule = [4.0, 8.0, 16.0, 30.0]

        _best: List[Dict] = []          # rezultatele adunate din încercările parțiale
        _best_stats: Optional[Dict] = None

        def _partial(st: Dict) -> bool:
            lost = st.get("failed", 0) + st.get("skipped", 0)
            total = lost + st.get("ok", 0)
            return total > 0 and lost / total >= 0.15

        def _finish(marks: List[Dict], st: Dict, attempt: int) -> Tuple[List[Dict], str]:
            quality.update(ok=st.get("ok", 0), failed=st.get("failed", 0), skipped=st.get("skipped", 0),
                           partial=_partial(st), attempts=attempt)
            if not include_expired:
                marks = [m for m in marks if not _is_expired_mark(m)]
            return marks, "live:tmview"

        for _attempt in range(1, _MAX_ATTEMPTS + 1):
            _cb_reset()
            _stats = {"ok": 0, "failed": 0, "skipped": 0}
            _req_stats_ctx.set(_stats)
            try:
                marks = await asyncio.wait_for(
                    _fetch_tmview(name, nice_classes, offices, proxy_url="",
                                  include_expired=include_expired,
                                  extra_terms=extra_terms,
                                  wildcard_patterns=wildcard_patterns),
                    timeout=_tmview_timeout
                )
                if marks:
                    # Preferăm orice rezultate REALE deja adunate (ex. batch-uri reușite
                    # dintr-o căutare EU_FULL pe mai multe teritorii) unor date 100% demo —
                    # chiar dacă circuit breaker-ul s-a deschis între timp (blocând doar
                    # cererile ULTERIOARE, nu invalidând ce am găsit deja).
                    print(f"[TMVIEW] direct success (attempt {_attempt}/{_MAX_ATTEMPTS}): {len(marks)} marks, "
                          f"cereri {_stats}" + (" (circuit breaker deschis pe parcurs)" if _cb_is_open() else ""))
                    _best = _merge_marks(_best, marks)
                    if _best_stats is None or _stats.get("ok", 0) > _best_stats.get("ok", 0):
                        _best_stats = _stats
                    # Căutare parțială (TMview a căzut pe parcurs) — o reluăm, dacă mai avem încercări,
                    # și păstrăm reuniunea rezultatelor; altfel ar lipsi mărci fără ca nimeni să afle.
                    if not _partial(_stats) or _attempt == _MAX_ATTEMPTS:
                        return _finish(_best, _best_stats, _attempt)
                    print(f"[TMVIEW] Căutare parțială ({_stats}) — reiau")
            except asyncio.TimeoutError:
                print(f"[TMVIEW] direct timeout (attempt {_attempt}/{_MAX_ATTEMPTS})")
            except Exception as e:
                print(f"[TMVIEW] direct error (attempt {_attempt}/{_MAX_ATTEMPTS}): {type(e).__name__}: {e}")

            if _attempt < _MAX_ATTEMPTS:
                _delay = _backoff_schedule[min(_attempt - 1, len(_backoff_schedule) - 1)]
                print(f"[TMVIEW] Reîncerc peste {_delay:.0f}s ({_attempt + 1}/{_MAX_ATTEMPTS})")
                await asyncio.sleep(_delay)
                continue
            break

        if _best:
            return _finish(_best, _best_stats, _MAX_ATTEMPTS)
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
