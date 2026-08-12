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
    """Test connectivity to TMview API — for diagnostics only."""
    from agents.search_agent import AsyncSession, TMVIEW_URL, TMVIEW_HOME, _PROXIES, _build_headers, HAS_CURL_CFFI
    if not HAS_CURL_CFFI:
        return {"error": "curl_cffi not available"}
    results = {}
    try:
        async with AsyncSession(impersonate="chrome120", proxies=_PROXIES, verify=not bool(_PROXIES)) as session:
            r = await session.get(TMVIEW_HOME, timeout=60)
            results["home_status"] = r.status_code
            results["home_cookies"] = list(session.cookies.keys())
            results["home_body_preview"] = r.text[:300]
            r2 = await session.post(TMVIEW_URL, json={
                "page": "1", "pageSize": "5", "criteria": "F",
                "basicSearch": "TEST", "newPage": True,
                "fields": ["ST13", "tmName"],
                "territories": ["RO"]
            }, headers=_build_headers(), timeout=60)
            results["api_status"] = r2.status_code
            results["api_body_preview"] = r2.text[:500]
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


@app.get("/api/session-status")
async def session_status():
    return {"active": has_browser_session()}


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

    # Generează variante wildcard (afișate în UI); search_agent le reconstruiește intern
    variants = generate_all_variants(name)

    trademarks, source = await search_agent.search(
        name,
        request.nice_classes,
        request.offices,
        extra_terms=variants.get("search_terms", []),
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
    include_expired = request.include_expired

    try:
        if fmt == "excel":
            data = build_excel(name, classes, offices, results, similar, expired_conflicts, expired_similar, include_expired=include_expired)
            filename = f"raport_marca_{name.replace(' ', '_')}.xlsx"
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif fmt == "pdf":
            data = build_pdf(name, classes, offices, results, similar, expired_conflicts, expired_similar, include_expired=include_expired)
            filename = f"raport_marca_{name.replace(' ', '_')}.pdf"
            media_type = "application/pdf"
        elif fmt == "word":
            data = build_word(name, classes, offices, results, similar, expired_conflicts, expired_similar, include_expired=include_expired)
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
