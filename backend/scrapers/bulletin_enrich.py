"""
Îmbogățește mărcile din buletinele OSIM/EUIPO cu aceleași detalii pe care le aduce
aplicația de verificare (agents.search_agent._fetch_detail): produse/servicii pe clase
NISA, date de publicare/status/opoziție, solicitanți și reprezentanți structurați,
coduri Viena, tip marcă, imagine.

Buletinul (PDF) rămâne sursa autoritară pentru datele publicate; detaliul TMview doar
completează ce lipsește. Rezultatele se salvează pe disc, per marcă (ST13 TMview), ca
o marcă deja îmbogățită să nu fie cerută a doua oară, iar cele pe care TMview încă nu le
are (publicate azi) să fie reîncercate mai târziu.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Dict, List, Optional

from paths import DATA_DIR

CACHE_DIR = (
    os.path.join(DATA_DIR, "bulletins", "detail") if DATA_DIR
    else os.path.join(os.path.dirname(__file__), "..", "..", "data", "bulletins", "detail")
)
os.makedirs(CACHE_DIR, exist_ok=True)

RETRY_MISSING_AFTER = 6 * 3600     # o marcă negăsită în TMview se reîncearcă după 6h
CONCURRENCY = 8
CHUNK = 40                         # salvăm cache-ul după fiecare grup, nu doar la final

# Câmpuri luate din detaliul TMview. Restul (nume, clase, solicitant, adresă, imagine din
# buletin) rămân cele din buletin.
_DETAIL_FIELDS = (
    "goodAndServices", "registrationDate", "expiryDate", "publicationDate",
    "markCurrentStatusCode", "markCurrentStatusDate", "markFeature", "kindMark",
    "oppositionStartDate", "oppositionEndDate", "viennaCodes", "designatedCountries",
    "applicants_detail", "representatives", "officeUrl",
)


def tmview_st13(source: str, mark: Dict) -> Optional[str]:
    """ST13 din TMview pentru o marcă din buletin. Buletinele folosesc alt format
    (ROM202608118 / EM019164848) decât TMview (RO50000M202608118 / EM500000019164848)."""
    app = str(mark.get("applicationNumber") or "").replace(" ", "").strip()
    if not app:
        return None
    if source == "osim":
        return f"RO50000{app}"
    if source == "euipo":
        return f"EM500000{app}"
    return None


def _cache_path(source: str) -> str:
    return os.path.join(CACHE_DIR, f"{source}.json")


def _load_cache(source: str) -> Dict[str, Dict]:
    try:
        with open(_cache_path(source)) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(source: str, cache: Dict[str, Dict]) -> None:
    tmp = _cache_path(source) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, _cache_path(source))


def _merge(mark: Dict, detail: Dict) -> Dict:
    """Aceeași regulă ca enrich_marks_with_detail din aplicația de verificare, dar
    cu buletinul ca sursă autoritară: valorile lui nu sunt suprascrise cu goluri."""
    merged = dict(mark)
    for k in _DETAIL_FIELDS:
        v = detail.get(k)
        if v in (None, "", [], {}):
            continue
        if merged.get(k) in (None, "", [], {}):
            merged[k] = v
    if detail.get("applicants_detail"):
        merged["applicants"] = detail["applicants_detail"]
    if not merged.get("markImageURI") and detail.get("markImageURI"):
        merged["markImageURI"] = detail["markImageURI"]
    merged["_detail_enriched"] = True
    return merged


def _describe_classes(mark: Dict) -> Dict:
    """Clasele descrise, ca în cardul din aplicația de verificare: pentru fiecare clasă
    NISA — titlul scurt și descrierea generică în română (nice_classes_ro), plus lista de
    produse/servicii a mărcii când o avem. `niceDetailed` acoperă și clasele fără listă."""
    from nice_classes_ro import get_nice_description, get_nice_short

    goods = []
    for g in mark.get("goodAndServices") or []:
        nc = str(g.get("niceClass", "")).strip()
        n = int(nc) if nc.isdigit() else 0
        goods.append({
            "niceClass":        nc,
            "niceClassInt":     n,
            "niceShort":        get_nice_short(n) if n else "",
            "niceDescription":  get_nice_description(n) if n else "",
            "goodsAndServices": g.get("goodsAndServices", ""),
        })
    goods.sort(key=lambda g: g["niceClassInt"])
    nice = sorted({int(c) for c in (mark.get("niceClass") or []) if str(c).isdigit()})
    mark["goodAndServices"] = goods
    mark["niceDetailed"] = [
        {"class": c, "short": get_nice_short(c), "description": get_nice_description(c)} for c in nice
    ]
    return mark


def apply_cached_detail(source: str, marks: List[Dict], bulletin_date: Optional[str] = None) -> List[Dict]:
    """Combină mărcile din buletin cu detaliile deja aduse (doar din cache, fără rețea) și
    adaugă descrierea claselor. `bulletin_date` (YYYY-MM-DD) devine data publicării când
    buletinul nu o dă pe marcă (OSIM: BOPI-ul e publicat chiar în data lui)."""
    cache = _load_cache(source)
    out = []
    for m in marks:
        st13 = tmview_st13(source, m)
        entry = cache.get(st13) if st13 else None
        detail = entry.get("detail") if entry else None
        merged = _merge(m, detail) if detail else dict(m)
        if bulletin_date and not merged.get("publicationDate"):
            merged["publicationDate"] = bulletin_date
        out.append(_describe_classes(merged))
    return out


def enrichment_stats(source: str, marks: List[Dict]) -> Dict:
    cache = _load_cache(source)
    have = sum(1 for m in marks if (cache.get(tmview_st13(source, m) or "") or {}).get("detail"))
    return {"total": len(marks), "enriched": have}


async def enrich_bulletin_marks(source: str, marks: List[Dict], progress: Optional[Dict] = None) -> Dict:
    """Aduce din TMview detaliul mărcilor care încă nu-l au (sau care au lipsit acum >6h)
    și îl salvează în cache. Întoarce statistici; nu aruncă excepții."""
    from agents.search_agent import (
        AsyncSession, HAS_CURL_CFFI, TMVIEW_HOME, _build_headers, _fetch_detail, _PROXIES,
    )
    progress = progress if progress is not None else {}
    cache = _load_cache(source)
    now = time.time()

    todo = []
    for m in marks:
        st13 = tmview_st13(source, m)
        if not st13:
            continue
        entry = cache.get(st13)
        if entry and entry.get("detail"):
            continue
        if entry and now - entry.get("missing_at", 0) < RETRY_MISSING_AFTER:
            continue
        todo.append(st13)
    todo = list(dict.fromkeys(todo))

    progress.update({"total": len(todo), "done": 0, "found": 0})
    if not todo or not HAS_CURL_CFFI:
        return {**enrichment_stats(source, marks), "fetched": 0, "missing": 0}

    sem = asyncio.Semaphore(CONCURRENCY)
    found = missing = 0
    empty_batches = 0     # grupuri consecutive fără niciun răspuns — TMview ne limitează cererile
    blocked = False

    async def _one(session, st13):
        async with sem:
            return st13, await _fetch_detail(session, st13)

    async with AsyncSession(impersonate="chrome120", proxies=_PROXIES,
                            verify=not bool(_PROXIES)) as session:
        for _try in range(2):
            try:
                await session.get(TMVIEW_HOME, timeout=10, headers=_build_headers())
                break
            except Exception:
                await asyncio.sleep(1.5)

        for i in range(0, len(todo), CHUNK):
            batch = todo[i:i + CHUNK]
            results = await asyncio.gather(*(_one(session, s) for s in batch), return_exceptions=True)
            ok = [r for r in results if not isinstance(r, Exception)]
            # Dacă niciun răspuns din grup n-a adus detaliu, e probabil TMview blocat/instabil,
            # nu mărci absente — nu le marcăm ca „lipsă”, ca să fie reîncercate la următorul tick.
            batch_reachable = any(d for _, d in ok)
            empty_batches = 0 if batch_reachable else empty_batches + 1
            for st13, detail in ok:
                if detail:
                    cache[st13] = {"detail": detail, "at": now}
                    found += 1
                elif batch_reachable:
                    cache[st13] = {"missing_at": time.time()}
                    missing += 1
            _save_cache(source, cache)
            progress.update({"done": min(i + CHUNK, len(todo)), "found": found})
            if empty_batches >= 2 and i + CHUNK < len(todo):
                # Continuarea ar lovi degeaba un TMview care refuză; restul rămân necache-uite
                # și sunt reîncercate la următoarea rulare (programator sau deschidere tabel).
                blocked = True
                progress["blocked"] = True
                print(f"[BULLETIN-ENRICH] {source}: TMview nu răspunde — opresc la {i + CHUNK}/{len(todo)}")
                break

    return {**enrichment_stats(source, marks), "fetched": found, "missing": missing, "blocked": blocked}
