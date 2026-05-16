import os
import traceback
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
from agents.euipo_agent import search_euipo, euipo_available
from export import build_excel, build_pdf, build_word

app = FastAPI(title="Trademark Checker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

search_agent    = SearchAgent()
similarity_agent = SimilarityAgent(threshold_very_high=90.0, threshold_high=75.0, threshold_medium=60.0, threshold_small=35.0)


class SearchRequest(BaseModel):
    trademark_name: str
    nice_classes: List[str]
    offices: List[str]
    include_expired: bool = False


class CurlRequest(BaseModel):
    curl: str


class ExportRequest(BaseModel):
    trademark_name: str
    nice_classes: List[str]
    offices: List[str]
    results: List[dict]
    similar: List[dict] = []
    format: str  # "excel", "pdf" or "word"


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


@app.get("/api/session-status")
async def session_status():
    return {"active": has_browser_session()}


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
    )
    analysis = similarity_agent.analyze(name, trademarks, request.nice_classes)

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

    try:
        if fmt == "excel":
            data = build_excel(name, classes, offices, results, similar)
            filename = f"raport_marca_{name.replace(' ', '_')}.xlsx"
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif fmt == "pdf":
            data = build_pdf(name, classes, offices, results, similar)
            filename = f"raport_marca_{name.replace(' ', '_')}.pdf"
            media_type = "application/pdf"
        elif fmt == "word":
            data = build_word(name, classes, offices, results, similar)
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
