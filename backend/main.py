import asyncio
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
import io

from agents.search_agent import SearchAgent, set_browser_session, has_browser_session
from agents.similarity_agent import SimilarityAgent
from agents.variant_agent import generate_all_variants
from export import build_excel, build_pdf, build_word
from db import init_db, SessionLocal
from monitor_models import CheckJob
from scheduler import start_scheduler, stop_scheduler
from routes.monitor import router as monitor_router
from routes.checks import router as checks_router, save_check


def _ensure_data_dirs():
    base = os.path.join(os.path.dirname(__file__), "..", "data", "bulletins")
    os.makedirs(os.path.join(base, "osim"),  exist_ok=True)
    os.makedirs(os.path.join(base, "euipo"), exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_data_dirs()
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Trademark Checker", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(monitor_router)
app.include_router(checks_router)

search_agent    = SearchAgent()
similarity_agent = SimilarityAgent(threshold_very_high=90.0, threshold_high=75.0, threshold_medium=60.0, threshold_small=35.0)


class SearchRequest(BaseModel):
    trademark_name: str
    nice_classes: List[str]
    offices: List[str]
    include_expired: bool = True


class CurlRequest(BaseModel):
    curl: str


class ExportRequest(BaseModel):
    trademark_name: str
    nice_classes: List[str]
    offices: List[str]
    results: List[dict]
    similar: List[dict] = []
    expired_conflicts: List[dict] = []
    expired_similar: List[dict] = []
    ended_marks: List[dict] = []
    terminated_marks: List[dict] = []
    format: str  # "excel", "pdf" or "word"
    include_expired: bool = True  # dacă False, exclude mărci expirate din exportul Word


@app.post("/api/set-curl")
async def set_curl(request: CurlRequest):
    ok = set_browser_session(request.curl)
    if not ok:
        raise HTTPException(status_code=400, detail="Nu am găsit cookie-uri în cURL. Verifică că ai copiat request-ul corect.")
    return {"status": "ok", "message": "Sesiune TMview activată. Căutările vor folosi acum sesiunea ta de browser."}


@app.get("/api/region-status")
async def region_status():
    """Regiunea curentă, dacă rotirea automată e configurată, și ultima rotire încercată."""
    from region_rotate import status
    return status()


@app.get("/api/tmview-eta")
async def tmview_eta():
    """Pentru cronometrul din frontend: etapa deblocării TMview și când merită reîncercată căutarea.
    Fără apeluri la API-ul Railway (spre deosebire de /api/region-status) — se poate interoga des."""
    from region_rotate import retry_eta
    return retry_eta()


@app.post("/api/region-rotate")
async def region_rotate_now(x_backup_token: Optional[str] = Header(None)):
    """Forțează o rotire de regiune acum (testare / urgență) — protejat de BACKUP_TOKEN,
    ca și backup-ul bazei de date."""
    import hmac
    expected = os.environ.get("BACKUP_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Setează BACKUP_TOKEN în Railway ca să poți declanșa rotirea manual de aici.")
    if not x_backup_token or not hmac.compare_digest(x_backup_token, expected):
        raise HTTPException(403, "Token greșit.")
    import asyncio
    import region_rotate as _rr
    # Forțat: ignorăm cooldown-ul (apelat manual, nu de bucla automată)
    old_check = _rr._can_rotate_now
    _rr._can_rotate_now = lambda: None if _rr._token() else "RAILWAY_API_TOKEN nesetat"
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _rr.rotate_region, "declanșat manual din /api/region-rotate")
    finally:
        _rr._can_rotate_now = old_check
    return result


@app.get("/api/debug-euipo-raw")
async def debug_euipo_raw(query: str = "", size: int = 3, page: int = 0):
    """Rulează un query RSQL brut pe EUIPO Search API și arată câmpurile întoarse
    (folosit ca să aflăm după ce câmpuri se poate filtra, ex. data publicării)."""
    from agents.euipo_agent import EUIPO_SEARCH_URL, EUIPO_CLIENT_ID, euipo_available, _get_access_token
    if not euipo_available():
        return {"error": "EUIPO not configured"}
    import requests as _rq
    hdrs = {"Authorization": f"Bearer {_get_access_token()}", "X-IBM-Client-Id": EUIPO_CLIENT_ID, "Accept": "application/json"}
    params = {"size": size, "page": page}
    if query:
        params["query"] = query
    r = _rq.get(EUIPO_SEARCH_URL, headers=hdrs, params=params, timeout=30)
    out = {"status": r.status_code, "query": query}
    try:
        d = r.json()
    except Exception:
        out["body"] = r.text[:500]
        return out
    tms = d.get("trademarks") or []
    out["count"] = len(tms)
    out["top_level_keys"] = [k for k in d.keys() if k != "trademarks"]
    out["meta"] = {k: d[k] for k in d.keys() if k != "trademarks"}
    out["first"] = tms[0] if tms else d if r.status_code != 200 else None
    return out


@app.get("/api/debug-euipo-detail")
async def debug_euipo_detail(app_num: str, sub: str = ""):
    """Răspunsul brut al API-ului EUIPO pentru o singură marcă (detaliu complet)."""
    from agents.euipo_agent import EUIPO_SEARCH_URL, EUIPO_CLIENT_ID, euipo_available, _get_access_token
    if not euipo_available():
        return {"error": "EUIPO not configured"}
    import requests as _rq
    hdrs = {"Authorization": f"Bearer {_get_access_token()}", "X-IBM-Client-Id": EUIPO_CLIENT_ID, "Accept": "application/json"}
    url = f"{EUIPO_SEARCH_URL}/{app_num}" if not sub else f"{EUIPO_SEARCH_URL.rsplit('/trademarks', 1)[0]}/{sub}"
    r = _rq.get(url, headers=hdrs, timeout=30)
    try:
        return {"status": r.status_code, "body": r.json()}
    except Exception:
        return {"status": r.status_code, "text": r.text[:800]}


@app.get("/api/debug-euipo-query")
async def debug_euipo_query(name: str, nc: str = ""):
    """Testează mai multe variante de formatare RSQL pentru un nume cu spații —
    query-ul EUIPO actual (wordMarkSpecification.verbalElement==NUME CU SPATII,
    fără ghilimele) poate fi interpretat greșit de parserul RSQL, ratând mărci
    care există cu adevărat (verificat: "PROTECT YOUR BUSINESS" clasa 45 nu
    apărea deloc, deși e înregistrată)."""
    from agents.euipo_agent import EUIPO_SEARCH_URL, EUIPO_CLIENT_ID, euipo_available, _get_access_token
    if not euipo_available():
        return {"error": "EUIPO not configured"}
    import requests as _plain_requests

    upper = name.upper()
    nc_ints = [c.strip() for c in nc.split(",") if c.strip().isdigit()]
    nc_filter = f";niceClasses=in=({','.join(nc_ints)})" if nc_ints else ""

    variants = {
        "unquoted":        f"wordMarkSpecification.verbalElement=={upper}",
        "unquoted_wild":   f"wordMarkSpecification.verbalElement==*{upper}*",
        "double_quoted":   f'wordMarkSpecification.verbalElement=="{upper}"',
        "double_quoted_wild": f'wordMarkSpecification.verbalElement=="*{upper}*"',
        "single_quoted":   f"wordMarkSpecification.verbalElement=='{upper}'",
        "url_encoded_plus": f"wordMarkSpecification.verbalElement=={upper.replace(' ', '+')}",
    }

    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}", "X-IBM-Client-Id": EUIPO_CLIENT_ID, "Accept": "application/json"}
    results = {}
    for label, q in variants.items():
        try:
            resp = _plain_requests.get(EUIPO_SEARCH_URL, headers=headers,
                                        params={"query": q + nc_filter, "size": 10, "page": 0}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("trademarks") or data.get("items") or data.get("results") or data.get("data") or []
                results[label] = {"query_sent": q + nc_filter, "status": 200, "count": len(items),
                                   "names": [it.get("wordMarkSpecification", {}).get("verbalElement") for it in items[:5]]}
            else:
                results[label] = {"query_sent": q + nc_filter, "status": resp.status_code, "body": resp.text[:200]}
        except Exception as e:
            results[label] = {"query_sent": q + nc_filter, "error": f"{type(e).__name__}: {e}"}
    return results


@app.get("/api/debug-network")
async def debug_network():
    """Izolează cauza blocajului: IP-ul serverului (orice client ar da timeout)
    vs. amprenta TLS a curl_cffi (impersonate=chrome120 — un WAF poate "arunca"
    tăcut pachetele unui fingerprint suspect, ceea ce arată identic cu un timeout
    de rețea). Testează același host (tmdn.org) cu 3 clienți diferiți + un site
    de control (fără WAF anti-scraping) ca reper."""
    import time
    import requests as _plain_requests
    results = {}

    def _try(label, fn):
        t0 = time.time()
        try:
            r = fn()
            results[label] = {"ok": True, "status": r.status_code, "time": round(time.time() - t0, 2)}
        except Exception as e:
            results[label] = {"ok": False, "error": f"{type(e).__name__}: {e}", "time": round(time.time() - t0, 2)}

    # 1. plain `requests` (stack TLS standard Python, fără impersonare de browser)
    _try("tmdn_plain_requests", lambda: _plain_requests.get(
        "https://www.tmdn.org/tmview/", timeout=10,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}))

    # 2. curl_cffi FĂRĂ impersonare de browser (TLS implicit, nu Chrome-spoofed)
    try:
        from curl_cffi.requests import Session as _CurlSession
        def _cc_noimp():
            with _CurlSession() as s:
                return s.get("https://www.tmdn.org/tmview/", timeout=10)
        _try("tmdn_curlcffi_no_impersonate", _cc_noimp)
    except Exception as e:
        results["tmdn_curlcffi_no_impersonate"] = {"ok": False, "error": f"setup: {e}"}

    # 3. curl_cffi CU impersonare Chrome (exact ce foloseşte deja căutarea normală)
    try:
        from curl_cffi.requests import Session as _CurlSession
        def _cc_imp():
            with _CurlSession(impersonate="chrome120") as s:
                return s.get("https://www.tmdn.org/tmview/", timeout=10)
        _try("tmdn_curlcffi_chrome120", _cc_imp)
    except Exception as e:
        results["tmdn_curlcffi_chrome120"] = {"ok": False, "error": f"setup: {e}"}

    # 4. Control — un site oarecare, fără protecție anti-scraping cunoscută,
    # ca să confirmăm că egress-ul general al serverului funcţionează normal.
    _try("control_example_com", lambda: _plain_requests.get("https://example.com", timeout=10))

    return results


@app.get("/api/debug-tmview")
async def debug_tmview():
    """Test TMview direct (fara proxy) si cu proxy."""
    from agents.search_agent import AsyncSession, TMVIEW_URL, TMVIEW_HOME, _build_headers, HAS_CURL_CFFI
    if not HAS_CURL_CFFI:
        return {"error": "curl_cffi not available"}
    results = {}

    def mk(territories=None, offices=None, term="TEST", crit="F", size=3):
        p = {"page": "1", "pageSize": str(size), "criteria": crit,
             "basicSearch": term, "newPage": True, "fields": ["ST13", "tmName", "tmOffice"]}
        if territories: p["territories"] = territories
        if offices:     p["offices"]     = offices
        return p

    # Test CRITERIILE: care coduri accepta TMview? (toate cu territories=["RO"])
    # Test TERITORIILE: care coduri de teritoriu sunt valide? (toate cu criteria="F")
    tests = {
        # Criterii — doar RO ca sa izolam variabila
        "crit_F_ro":   mk(territories=["RO"], crit="F"),   # stiam ca merge
        "crit_C_ro":   mk(territories=["RO"], crit="C"),
        "crit_Z_ro":   mk(territories=["RO"], crit="Z"),   # suspect 400
        "crit_E_ro":   mk(territories=["RO"], crit="E"),
        "crit_S_ro":   mk(territories=["RO"], crit="S"),
        # Teritorii — doar criteria="F" care stiam ca merge
        "ter_RO":      mk(territories=["RO"],  crit="F"),
        "ter_DE":      mk(territories=["DE"],  crit="F"),
        "ter_BE":      mk(territories=["BE"],  crit="F"),
        "ter_BX":      mk(territories=["BX"],  crit="F"),
        "ter_LU":      mk(territories=["LU"],  crit="F"),
        "ter_NL":      mk(territories=["NL"],  crit="F"),
        "ter_EM":      mk(territories=["EM"],  crit="F"),
        "ter_WO":      mk(territories=["WO"],  crit="F"),
        # Batch-uri cu criteria="F"
        "ter_7_nobenelux": mk(territories=["AT","DE","FR","IT","ES","PL","RO"], crit="F"),
        "ter_7_withBX":    mk(territories=["BX","AT","DE","FR","IT","ES","PL"], crit="F"),
        "ter_7_withBE":    mk(territories=["BE","AT","DE","FR","IT","ES","PL"], crit="F"),
    }

    try:
        async with AsyncSession(impersonate="chrome120", proxies=None, verify=True) as session:
            r = await session.get(TMVIEW_HOME, timeout=10)
            results["home_status"] = r.status_code
            import asyncio as _aio
            await _aio.sleep(1.0)
            for name, payload in tests.items():
                try:
                    rx = await session.post(TMVIEW_URL, json=payload, headers=_build_headers(), timeout=15)
                    ct = rx.headers.get("content-type", "")
                    if rx.status_code == 200 and "json" in ct:
                        d = rx.json()
                        results[name] = {"status": 200, "total": d.get("total"),
                                         "offices": [m.get("tmOffice") for m in d.get("tradeMarks", [])[:3]]}
                    else:
                        results[name] = {"status": rx.status_code, "body": rx.text[:120]}
                    await _aio.sleep(1.0)
                except Exception as ex:
                    results[name] = {"error": str(ex)[:100]}
    except Exception as e:
        results["error"] = f"{type(e).__name__}: {e}"
    return results


@app.get("/api/debug-tm-detail")
async def debug_tm_detail(st13: str):
    """Returnează raw JSON de la TMview pentru un ST13 dat — pentru diagnosticare designatedCountries."""
    from agents.search_agent import AsyncSession, TMVIEW_DETAIL, TMVIEW_HOME, _PROXIES, _build_headers, HAS_CURL_CFFI
    if not HAS_CURL_CFFI:
        return {"error": "curl_cffi not available"}
    try:
        async with AsyncSession(impersonate="chrome120", proxies=_PROXIES, verify=not bool(_PROXIES)) as session:
            await session.get(TMVIEW_HOME, timeout=20)
            r = await session.get(TMVIEW_DETAIL.format(st13=st13), headers=_build_headers(), timeout=20)
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}", "body": r.text[:500]}
            data = r.json()
            tm = data.get("tradeMark", {})
            return {
                "tm_keys": list(tm.keys()),
                "root_keys": list(data.keys()),
                "designatedCountries": tm.get("designatedCountries"),
                "designatedOffices": tm.get("designatedOffices"),
                "territories": tm.get("territories"),
                "designationUnderMadridProtocol": tm.get("designationUnderMadridProtocol"),
                "office": tm.get("office"),
                "tmOffice": tm.get("tmOffice"),
                "publication": data.get("publication", [])[:3],
                "oppositions": data.get("oppositions", [])[:3],
                "oppositionStartDate": tm.get("oppositionStartDate"),
                "oppositionEndDate": tm.get("oppositionEndDate"),
                "markCurrentStatusDate": tm.get("markCurrentStatusDate"),
            }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@app.get("/api/tm-detail")
async def tm_detail(st13: str):
    """Returnează detalii complete (reprezentant, mărfuri, date) pentru o marcă după ST13."""
    from agents.search_agent import _fetch_detail, AsyncSession, TMVIEW_HOME, _PROXIES, _build_headers, HAS_CURL_CFFI
    if not HAS_CURL_CFFI:
        return {"error": "curl_cffi not available"}
    if not st13 or st13.startswith("DEMO"):
        return {}
    import export_cache
    from agents.search_agent import fetch_detail_once
    loop = asyncio.get_event_loop()
    cached = (await loop.run_in_executor(None, export_cache.get_details, [st13])).get(st13)
    if cached:
        return cached
    try:
        async with AsyncSession(impersonate="chrome120", proxies=_PROXIES, verify=not bool(_PROXIES)) as session:
            await session.get(TMVIEW_HOME, timeout=10, headers=_build_headers())
            detail = await fetch_detail_once(session, st13)
        if detail:   # salvat și pentru export (nu-l mai cerem încă o dată la TMview)
            await loop.run_in_executor(None, export_cache.save_details, {st13: detail})
        return detail
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


class _DetailBench(BaseModel):
    st13s: List[str]
    concurrency: int = 5


@app.post("/api/debug/detail-bench")
async def detail_bench(body: _DetailBench):
    """Măsoară cererile de detaliu TMview (fără cache) la o anumită concurență — pentru a alege
    concurența exportului pe date reale. Max 30 de mărci per test."""
    import time as _t
    import agents.search_agent as _sa
    _sa.DETAIL_DIAG["fail"].clear(); _sa.DETAIL_DIAG["ok_s"].clear()
    sem = asyncio.Semaphore(max(1, min(body.concurrency, 15)))
    t0 = _t.monotonic()
    async with _sa.AsyncSession(impersonate="chrome120", proxies=_sa._PROXIES, verify=not bool(_sa._PROXIES)) as session:
        await session.get(_sa.TMVIEW_HOME, timeout=10, headers=_sa._build_headers())

        async def one(st13):
            async with sem:
                return bool(await _sa._fetch_detail(session, st13))
        res = await asyncio.gather(*(one(s) for s in body.st13s[:30]))
    ok = sorted(_sa.DETAIL_DIAG["ok_s"])
    return {"n": len(res), "ok": sum(res), "elapsed_s": round(_t.monotonic() - t0, 1),
            "fail": dict(_sa.DETAIL_DIAG["fail"]), "median_s": ok[len(ok) // 2] if ok else None}


@app.get("/api/session-status")
async def session_status():
    return {"active": has_browser_session()}


@app.get("/api/debug-session")
async def debug_session():
    """Arată ce chei de cookie/header s-au extras din cURL-ul lipit — fără valori
    (nu expunem cookie-urile efective), doar numele, ca să diagnosticăm dacă
    cookie-urile Imperva (incap_ses_*, visid_incap_*, nlbi_*, ___utmvc) au fost
    prinse la parsare sau lipsesc din cURL-ul copiat."""
    from agents.search_agent import _browser_session
    cookies = _browser_session.get("cookies", {}) or {}
    headers = _browser_session.get("headers", {}) or {}
    return {
        "active": has_browser_session(),
        "cookie_keys": sorted(cookies.keys()),
        "cookie_count": len(cookies),
        "header_keys": sorted(headers.keys()),
        "has_imperva_cookie": any(
            k.lower().startswith(("incap_ses", "visid_incap", "nlbi", "___utmvc", "reese84"))
            for k in cookies
        ),
    }


@app.get("/api/debug-search")
async def debug_search(name: str = "VISUAL", offices: str = "EM", nc: str = "9", full: str = "0"):
    """Rulează căutarea și returnează source + log intern.
    full=1 → folosește search_agent.search() complet (cu EUIPO); full=0 → _fetch_tmview direct.
    """
    import asyncio, io, sys
    from agents.search_agent import _fetch_tmview, _cb_reset, HAS_CURL_CFFI
    from agents.variant_agent import build_offices_and_territories

    office_list = [o.strip().upper() for o in offices.split(",") if o.strip()]
    nc_list = [c.strip() for c in nc.split(",") if c.strip()]
    built_offices, territories = build_offices_and_territories(office_list)
    many = len(territories) > 7

    if not HAS_CURL_CFFI:
        return {"error": "curl_cffi not available"}

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    marks = None
    source = "?"
    try:
        if full == "1":
            marks, source = await search_agent.search(name, nc_list, office_list)
        else:
            _cb_reset()
            marks = await asyncio.wait_for(
                _fetch_tmview(name, nc_list, office_list, proxy_url=""),
                timeout=60.0
            )
            source = "live:tmview" if (marks and len(marks) > 0) else "demo (0 marks)"
    except asyncio.TimeoutError:
        source = "TIMEOUT (>60s)"
    except Exception as e:
        source = f"ERROR: {type(e).__name__}: {e}"
    finally:
        sys.stdout = old_stdout

    log_lines = buf.getvalue().splitlines()
    return {
        "mode": "full search_agent.search()" if full == "1" else "_fetch_tmview direct",
        "name": name, "offices_input": office_list, "nc": nc_list,
        "built_offices": built_offices, "territories": territories,
        "many_territories": many, "source": source,
        "marks_count": len(marks) if marks else 0,
        "first_mark": (marks[0] if marks else None),
        "log": log_lines[-50:],
    }


@app.post("/api/reset-circuit-breaker")
async def reset_circuit_breaker():
    from agents.search_agent import _cb_reset
    _cb_reset()
    return {"status": "ok", "message": "Circuit breaker resetat. TMview va fi reîncercat."}


@app.get("/api/debug-euipo")
async def debug_euipo():
    """Testează diferite query-uri EUIPO API."""
    import asyncio, requests as _req
    from agents.euipo_agent import (EUIPO_CLIENT_ID, EUIPO_SEARCH_URL, EUIPO_TOKEN_URL,
                                     euipo_available, _get_access_token)
    result = {
        "configured": euipo_available(),
        "client_id_prefix": EUIPO_CLIENT_ID[:8] if EUIPO_CLIENT_ID else None,
    }
    if not euipo_available():
        return result

    try:
        token = _get_access_token(force_refresh=True)
        result["oauth_token"] = "obtained (" + token[:10] + "...)"
    except Exception as e:
        result["oauth_token_error"] = f"{EUIPO_TOKEN_URL} -> {str(e)[:300]}"
        return result

    hdrs = {
        "Authorization":   f"Bearer {token}",
        "X-IBM-Client-Id": EUIPO_CLIENT_ID,
        "Accept": "application/json",
    }

    def _try(label, params):
        try:
            r = _req.get(EUIPO_SEARCH_URL, headers=hdrs, params=params, timeout=10)
            return {"label": label, "status": r.status_code, "body": r.text[:400]}
        except Exception as e:
            return {"label": label, "error": str(e)[:100]}

    queries = [
        ("eq APPLE",        {"query": "wordMarkSpecification.verbalElement==APPLE", "size": 10}),
        ("like %APPLE%",    {"query": "wordMarkSpecification.verbalElement=like=%APPLE%", "size": 10}),
        ("ilike %APPLE%",   {"query": "wordMarkSpecification.verbalElement=ilike=%APPLE%", "size": 10}),
        ("no query",        {"size": 10}),
        ("name field",      {"query": "tradeMarkName==APPLE", "size": 10}),
        ("date range",      {"query": "applicationDate>=2026-08-19;applicationDate<=2026-08-21", "size": 10}),
        ("date range prod", {"query": "applicationDate>=2026-08-20;applicationDate<=2026-08-22", "size": 100, "page": 0, "sort": "applicationDate:desc"}),
    ]

    loop = asyncio.get_event_loop()
    results = []
    for label, params in queries:
        try:
            r = await asyncio.wait_for(
                loop.run_in_executor(None, _try, label, params),
                timeout=12.0
            )
            results.append(r)
        except asyncio.TimeoutError:
            results.append({"label": label, "error": "timeout"})
    result["queries"] = results
    return result


class TestSmtpRequest(BaseModel):
    email: str


@app.post("/api/settings/test-smtp")
async def test_smtp(request: TestSmtpRequest):
    """Trimite un email de test la adresa specificată."""
    from monitor_service import _email_available, _send_email
    if not _email_available():
        raise HTTPException(400, "SMTP neconfigurate. Setați SMTP_USER și SMTP_PASSWORD.")
    if "@" not in request.email:
        raise HTTPException(400, "Adresă email invalidă.")
    try:
        _send_email(
            request.email,
            "[Trademark Monitor] Test SMTP ✓",
            "<p>Dacă primiți acest email, configurarea SMTP funcționează corect.</p>",
        )
        return {"status": "ok", "message": f"Email test trimis la {request.email}"}
    except Exception as e:
        raise HTTPException(500, f"Eroare SMTP: {e}")


@app.get("/api/settings")
async def get_settings():
    """Returnează starea configurației (fără parole/chei)."""
    from monitor_service import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_FROM, _email_available
    from agents.euipo_agent import euipo_available, EUIPO_CLIENT_ID

    smtp_ok = _email_available()
    euipo_ok = euipo_available()

    return {
        "smtp": {
            "configured": smtp_ok,
            "host":       SMTP_HOST if smtp_ok else None,
            "port":       SMTP_PORT if smtp_ok else None,
            "user":       SMTP_USER if smtp_ok else None,
            "from_addr":  SMTP_FROM if smtp_ok else None,
        },
        "euipo_api": {
            "configured": euipo_ok,
            "client_id":  (EUIPO_CLIENT_ID[:8] + "…") if (euipo_ok and EUIPO_CLIENT_ID) else None,
        },
        "tmview": {
            "configured": True,
            "note":       "Disponibil fără autentificare",
        },
        "scraperapi": {
            "configured": bool(os.environ.get("SCRAPERAPI_KEY")),
        },
        "database": {
            "type": "postgresql" if os.environ.get("DATABASE_URL") else "sqlite",
        },
    }


async def _run_check(request: "SearchRequest", max_tmview_attempts: int = 3) -> dict:
    if not request.trademark_name.strip():
        raise HTTPException(status_code=400, detail="Denumirea mărcii este obligatorie.")
    if not request.nice_classes:
        raise HTTPException(status_code=400, detail="Selectați cel puțin o clasă NICE.")
    if not request.offices:
        raise HTTPException(status_code=400, detail="Selectați cel puțin un teritoriu.")

    name = request.trademark_name.strip()

    variants = generate_all_variants(name)
    extra_terms = list(variants.get("search_terms", []))

    # Adaugă și termenele de căutare ale variantei fonetice principale (ex. CARTEZIAN pt KARTEZIAN).
    # Astfel trunchierile *CART*, *CARTE* din CARTEZIAN sunt căutate și când se caută KARTEZIAN,
    # găsind mărci ca CARTESIA care altfel ar fi ratate.
    from agents.variant_agent import build_phonetic_variants, build_input_list as _bil
    _phon_extras = [t for t in build_phonetic_variants(name) if not t.startswith("*") and len(t) >= 3]
    for _phon in _phon_extras[:3]:  # ex. UNIQORUM → UNIKORUM, UNICORUM, UNIQUORUM
        for _t in _bil(_phon):
            if "?" not in _t and _t not in extra_terms:
                extra_terms.append(_t)

    quality: dict = {}
    trademarks, source = await search_agent.search(
        name,
        request.nice_classes,
        request.offices,
        extra_terms=extra_terms,
        wildcard_patterns=variants.get("wildcard_patterns", []),
        include_expired=request.include_expired,
        max_tmview_attempts=max_tmview_attempts,
        quality=quality,
    )
    if source.startswith("demo (TMview"):
        # Nu afișăm mărci inventate ca rezultat — utilizatorul le-ar putea lua drept reale.
        raise HTTPException(
            status_code=503,
            detail="TMview blochează momentan serverul (protecție Imperva). Serverul își schimbă "
                   "automat regiunea/IP-ul — reîncearcă căutarea peste 2-3 minute.")
    analysis = similarity_agent.analyze(name, trademarks, request.nice_classes, user_offices=request.offices)

    return {
        "query":             name,
        "nice_classes":      request.nice_classes,
        "offices":           request.offices,
        "total_found":       len(trademarks),
        "risky_marks":       len(analysis["conflicts"]),
        "similar_marks":     len(analysis["similar"]),
        "results":           analysis["conflicts"],
        "similar":           analysis["similar"],
        "ended_marks":       analysis["ended_marks"],
        "terminated_marks":  analysis["terminated_marks"],
        "expired_conflicts": analysis["expired_conflicts"],
        "expired_similar":   analysis["expired_similar"],
        "source":            source,
        "search_quality":    quality,
        "variants":          variants,
    }


@app.post("/api/check")
async def check_trademark(request: SearchRequest):
    result = await _run_check(request)
    result["check_id"] = save_check(request, result)     # salvat în baza de date (vezi „Verificări salvate”)
    asyncio.ensure_future(_prefetch_export_assets(result))
    return result


# ── Căutare ca job de fundal ───────────────────────────────────────────────
# Conectivitatea spre TMview poate pica minute întregi (verificat live) — o
# singură cerere sincronă nu poate reîncerca insistent fără să blocheze
# nerezonabil de mult pagina. Aici pornim căutarea în fundal, cu un buget de
# reîncercări mult mai generos (până la ~7 minute), și frontend-ul verifică
# periodic dacă s-a terminat, în loc să aștepte pe un singur request.
#
# Job-ul e salvat în DB, nu într-un dict în memorie — un redeploy la mijlocul
# căutării (ex. rotirea automată de regiune, declanșată chiar de blocajul pe
# care-l întâlnește căutarea asta) altfel ar șterge job-ul din memoria
# procesului vechi și frontend-ul ar primi "Job negăsit" la următoarea verificare.

_PROCESS_START = datetime.utcnow()


def _set_job(job_id: str, status: str, result: Optional[dict] = None, error: Optional[str] = None) -> None:
    db = SessionLocal()
    try:
        row = db.get(CheckJob, job_id)
        if row is None:
            row = CheckJob(id=job_id)
            db.add(row)
        row.status     = status
        row.result     = result
        row.error      = error
        row.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


_EXPORT_LISTS = ("results", "similar", "ended_marks", "terminated_marks", "expired_conflicts", "expired_similar")


async def _prefetch_export_assets(result: dict) -> None:
    """Imediat după o căutare: aduce în fundal detaliile TMview și imaginile mărcilor găsite (în
    cache-ul din DB), ca exportul să fie gata în câteva secunde când utilizatorul apasă Export.
    Traducerile NU — costă bani la Google și se fac doar dacă chiar se exportă. Concurență redusă
    (4), ca să nu provocăm blocaje Imperva cât utilizatorul mai caută ceva."""
    import agents.search_agent as _sa
    import export as _export_mod
    marks = [m for k in _EXPORT_LISTS for m in (result.get(k) or [])]
    if not marks or not str(result.get("source", "")).startswith("live"):
        return
    try:
        await asyncio.sleep(3)
        t0 = datetime.utcnow()
        enriched = await _sa.enrich_marks_with_detail(marks, concurrency=4)
        await asyncio.get_event_loop().run_in_executor(None, _export_mod.prefetch_images, enriched)
        print(f"[EXPORT-PREFETCH] {len(marks)} mărci pregătite în {(datetime.utcnow() - t0).total_seconds():.0f}s")
    except Exception as e:
        print(f"[EXPORT-PREFETCH] {type(e).__name__}: {e}")


@app.post("/api/check/start")
async def check_trademark_start(request: SearchRequest):
    import uuid
    # Validăm întâi sincron, ca erorile (câmpuri lipsă) să ajungă imediat,
    # nu după ce utilizatorul așteaptă un job care oricum ar eșua instant.
    if not request.trademark_name.strip():
        raise HTTPException(status_code=400, detail="Denumirea mărcii este obligatorie.")
    if not request.nice_classes:
        raise HTTPException(status_code=400, detail="Selectați cel puțin o clasă NICE.")
    if not request.offices:
        raise HTTPException(status_code=400, detail="Selectați cel puțin un teritoriu.")

    job_id = uuid.uuid4().hex
    _set_job(job_id, "running")

    async def _bg():
        try:
            result = await _run_check(request, max_tmview_attempts=3)
            result["check_id"] = save_check(request, result)     # salvat în baza de date
            _set_job(job_id, "done", result=result)
            asyncio.ensure_future(_prefetch_export_assets(result))
        except HTTPException as e:
            _set_job(job_id, "error", error=e.detail)
        except Exception as e:
            _set_job(job_id, "error", error=str(e))

    asyncio.ensure_future(_bg())
    return {"job_id": job_id, "status": "started"}


@app.get("/api/check/status")
async def check_trademark_status(job_id: str):
    db = SessionLocal()
    try:
        row = db.get(CheckJob, job_id)
    finally:
        db.close()
    if not row:
        raise HTTPException(404, "Job negăsit — a expirat sau ID invalid.")
    if row.status == "running" and row.updated_at and row.updated_at < _PROCESS_START:
        # Job pornit de procesul anterior — a murit la repornire (de obicei rotirea automată de
        # regiune, declanșată chiar de blocajul TMview). Altfel pagina ar aștepta la nesfârșit.
        return {"status": "error", "result": None,
                "error": "Serverul a repornit pe altă regiune (TMview blocase IP-ul) — "
                         "căutarea s-a întrerupt. Apasă din nou „Caută” peste ~1 minut."}
    return {"status": row.status, "result": row.result, "error": row.error}


async def _run_export(request: ExportRequest):
    """Construiește fișierul de export. Poate dura zeci de secunde — vezi
    enrich_marks_with_detail (un round-trip TMview + Translate per marcă unică)."""
    name    = request.trademark_name
    classes = request.nice_classes
    offices = request.offices
    results = request.results
    fmt     = request.format.lower()

    similar = request.similar
    expired_conflicts = request.expired_conflicts
    expired_similar = request.expired_similar
    ended_marks = request.ended_marks
    terminated_marks = request.terminated_marks
    include_expired = request.include_expired

    # Cronometre pe etape (raportate în /api/export/status) — vezi export.EXPORT_TIMINGS.
    import export as _export_mod
    import time as _time
    _t0 = _time.monotonic()
    # Enrich all marks with TMview detail (G&S text, representatives, exact dates, etc.)
    import agents.search_agent as _sa
    _sa.DETAIL_DIAG["fail"].clear(); _sa.DETAIL_DIAG["ok_s"].clear()
    all_export_marks = results + similar + ended_marks + terminated_marks + expired_conflicts + expired_similar
    if all_export_marks:
        enriched_map = {}
        try:
            enriched_list = await _sa.enrich_marks_with_detail(all_export_marks)
            for orig, enriched in zip(all_export_marks, enriched_list):
                key = orig.get("ST13") or orig.get("applicationNumber") or id(orig)
                enriched_map[key] = enriched
            def _enrich(lst):
                out = []
                for m in lst:
                    key = m.get("ST13") or m.get("applicationNumber") or id(m)
                    out.append(enriched_map.get(key, m))
                return out
            results          = _enrich(results)
            similar          = _enrich(similar)
            ended_marks      = _enrich(ended_marks)
            terminated_marks = _enrich(terminated_marks)
            expired_conflicts = _enrich(expired_conflicts)
            expired_similar  = _enrich(expired_similar)
        except Exception as _e:
            print(f"[EXPORT] Detail enrichment failed: {_e} — exporting with available data")
    _export_mod.EXPORT_TIMINGS["detalii_s"] = round(_time.monotonic() - _t0, 1)
    _ok = sorted(_sa.DETAIL_DIAG["ok_s"])
    _export_mod.EXPORT_TIMINGS["detalii_diag"] = {
        "mărci": len({m.get("ST13") for m in all_export_marks}), "reușite_acum": len(_ok),
        "eșecuri": dict(_sa.DETAIL_DIAG["fail"]),
        "durata_median_s": _ok[len(_ok) // 2] if _ok else None, "durata_max_s": _ok[-1] if _ok else None}

    _t1 = _time.monotonic()
    _all_for_img = results + similar + ended_marks + terminated_marks + expired_conflicts + expired_similar
    await asyncio.get_event_loop().run_in_executor(None, _export_mod.prefetch_images, _all_for_img)
    _export_mod.EXPORT_TIMINGS["imagini_s"] = round(_time.monotonic() - _t1, 1)

    _t2 = _time.monotonic()
    try:
        if fmt == "excel":
            data = build_excel(name, classes, offices, results, similar, expired_conflicts, expired_similar, include_expired=include_expired, ended_marks=ended_marks, terminated_marks=terminated_marks)
            filename = f"raport_marca_{name.replace(' ', '_')}.xlsx"
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif fmt == "pdf":
            data = build_pdf(name, classes, offices, results, similar, expired_conflicts, expired_similar, include_expired=include_expired, ended_marks=ended_marks, terminated_marks=terminated_marks)
            filename = f"raport_marca_{name.replace(' ', '_')}.pdf"
            media_type = "application/pdf"
        elif fmt == "word":
            data = build_word(name, classes, offices, results, similar, expired_conflicts, expired_similar, include_expired=include_expired, ended_marks=ended_marks, terminated_marks=terminated_marks)
            filename = f"raport_marca_{name.replace(' ', '_')}.docx"
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            raise HTTPException(status_code=400, detail="Format invalid. Folosiți 'excel', 'pdf' sau 'word'.")
    except HTTPException:
        raise
    except Exception as e:
        err = traceback.format_exc()
        print(f"[EXPORT ERROR] {fmt.upper()}:\n{err}")
        raise HTTPException(status_code=500, detail=f"Eroare generare {fmt.upper()}: {type(e).__name__}: {e}")
    _export_mod.EXPORT_TIMINGS["document_s"] = round(_time.monotonic() - _t2, 1)
    _export_mod.EXPORT_TIMINGS["total_s"] = round(_time.monotonic() - _t0, 1)

    return data, filename, media_type


@app.post("/api/export")
async def export_report(request: ExportRequest):
    data, filename, media_type = await _run_export(request)
    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Export asincron ──────────────────────────────────────────────────────────
# Exporturile mari (multe mărci → multe round-trip-uri de detail-fetch TMview
# + traducere per marcă) pot dura peste un minut — peste timeout-ul de gateway
# al Railway-ului, care întrerupe cererea sincronă și o arată ca "Export eșuat"
# în frontend chiar dacă backend-ul ar fi terminat cu succes. La fel ca la
# căutare, pornim generarea în fundal și frontend-ul verifică periodic status-ul.
_export_jobs: dict = {}


@app.post("/api/export/start")
async def export_start(request: ExportRequest):
    import uuid
    job_id = uuid.uuid4().hex
    _export_jobs[job_id] = {"status": "running", "error": None}

    async def _bg():
        try:
            import export as _export_mod
            _export_mod.IMG_FAILURES.clear()
            _export_mod.EXPORT_TIMINGS.clear()
            data, filename, media_type = await _run_export(request)
            _export_jobs[job_id] = {
                "status": "done", "error": None,
                "image_failures": list(_export_mod.IMG_FAILURES),
                "timings": dict(_export_mod.EXPORT_TIMINGS),
                "data": data, "filename": filename, "media_type": media_type,
            }
        except HTTPException as e:
            _export_jobs[job_id] = {"status": "error", "error": e.detail}
        except Exception as e:
            _export_jobs[job_id] = {"status": "error", "error": str(e)}

    asyncio.ensure_future(_bg())
    return {"job_id": job_id, "status": "started"}


@app.get("/api/export/status")
async def export_status(job_id: str):
    job = _export_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job de export negăsit — a expirat sau ID invalid.")
    return {"status": job["status"], "error": job.get("error"), "image_failures": job.get("image_failures"),
            "timings": job.get("timings")}


@app.get("/api/export/download")
async def export_download(job_id: str):
    job = _export_jobs.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(404, "Fișierul de export nu este (încă) disponibil.")
    data = _export_jobs.pop(job_id)  # eliberăm memoria imediat după download
    return StreamingResponse(
        io.BytesIO(data["data"]),
        media_type=data["media_type"],
        headers={"Content-Disposition": f'attachment; filename="{data["filename"]}"'},
    )



frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
