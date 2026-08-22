import os
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List
import io

from agents.search_agent import SearchAgent, set_browser_session, has_browser_session
from agents.similarity_agent import SimilarityAgent
from agents.variant_agent import generate_all_variants
from export import build_excel, build_pdf, build_word
from db import init_db
from scheduler import start_scheduler, stop_scheduler
from routes.monitor import router as monitor_router


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
    try:
        async with AsyncSession(impersonate="chrome120", proxies=_PROXIES, verify=not bool(_PROXIES)) as session:
            await session.get(TMVIEW_HOME, timeout=10, headers=_build_headers())
            return await _fetch_detail(session, st13)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@app.get("/api/session-status")
async def session_status():
    return {"active": has_browser_session()}


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


@app.post("/api/check")
async def check_trademark(request: SearchRequest):
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
    for _phon in _phon_extras[:1]:
        for _t in _bil(_phon):
            if _t not in extra_terms:
                extra_terms.append(_t)

    trademarks, source = await search_agent.search(
        name,
        request.nice_classes,
        request.offices,
        extra_terms=extra_terms,
        wildcard_patterns=variants.get("wildcard_patterns", []),
        include_expired=request.include_expired,
    )
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
        "variants":          variants,
    }


@app.post("/api/export")
async def export_report(request: ExportRequest):
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

    # Enrich all marks with TMview detail (G&S text, representatives, exact dates, etc.)
    import agents.search_agent as _sa
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

    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
