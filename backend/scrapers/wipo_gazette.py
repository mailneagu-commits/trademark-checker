"""
Gazeta WIPO a mărcilor internaționale (sistemul Madrid) — publicată săptămânal, joia.

Pentru monitorizare luăm, din gazeta săptămânii, mărcile care desemnează România (RO) sau Uniunea
Europeană (EM): înregistrările internaționale noi (tranzacția ENN) și desemnările ulterioare (EXN).
Lista vine din endpoint-ul public al Madrid Monitor (gaz/query.jsp); clasele și produsele /
serviciile, adresa titularului și reprezentantul — din TMview (ST13 „WO50000000” + nr. IR); pentru
mărcile pe care TMview nu le are (sau nu răspunde), clasele și produsele se iau din fișa Madrid Monitor.
"""
from __future__ import annotations

import asyncio
import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

GAZ_URL = "https://www3.wipo.int/madrid/monitor/gaz/query.jsp"
IMAGE_URL = "https://www.wipo.int/madridwebgazette/images/{path}"
MONITOR_URL = "https://www3.wipo.int/madrid/monitor/en/showData.jsp?ID=ROM.{irn}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www3.wipo.int/madrid/monitor/en/"}
COUNTRIES = ("RO", "EM")
TRANSACTIONS = {"ENN": "Înregistrare internațională", "EXN": "Desemnare ulterioară"}


def gazette_day(td: date) -> date:
    """Ultima joi ≤ td — ziua în care apare gazeta săptămânii."""
    return td - timedelta(days=(td.weekday() - 3) % 7)


def st13(irn: str) -> str:
    return f"WO50000000{str(irn).zfill(7)}"


def gazette_issues(year: int) -> List[Dict]:
    r = requests.get(GAZ_URL, params={"action": "issues", "year": year, "lang": "en"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    out = []
    for it in r.json():
        try:
            out.append({"issue": int(it["week"]), "date": datetime.strptime(it["pub"], "%d/%m/%Y").date()})
        except (KeyError, ValueError):
            continue
    return out


def _query(year: int, issue: int, country: str, ttype: str) -> List[Dict]:
    docs, start = [], 0
    while True:
        r = requests.post(GAZ_URL, headers=HEADERS, timeout=60, data={
            "action": "query", "lang": "en", "year": year, "issue": issue, "chapter": 1, "country": country,
            "designated": "on", "irn": "", "holder": "", "brand": "", "type": ttype,
            "start": start, "rows": 100, "context": "browse"})
        r.raise_for_status()
        d = r.json()
        batch = d.get("docs") or []
        docs += batch
        start += len(batch)
        if not batch or start >= int(d.get("hits") or 0):
            return docs


async def _details(st13s: List[str]) -> Dict[str, Dict]:
    """Detaliile TMview (clase, produse, titular cu adresă, reprezentant) — din cache, apoi live."""
    import export_cache
    from agents.search_agent import (AsyncSession, HAS_CURL_CFFI, TMVIEW_HOME, _PROXIES, _build_headers,
                                     fetch_detail_once)
    loop = asyncio.get_event_loop()
    found: Dict[str, Dict] = await loop.run_in_executor(None, export_cache.get_details, st13s)
    todo = [s for s in st13s if not found.get(s)]
    if not todo or not HAS_CURL_CFFI:
        return found
    sem = asyncio.Semaphore(5)
    fresh: Dict[str, Dict] = {}
    try:
        async with AsyncSession(impersonate="chrome120", proxies=_PROXIES, verify=not bool(_PROXIES)) as session:
            await session.get(TMVIEW_HOME, timeout=15, headers=_build_headers())

            async def one(s: str):
                async with sem:
                    try:
                        d = await asyncio.wait_for(fetch_detail_once(session, s), timeout=40)
                        if d:
                            fresh[s] = d
                    except Exception as e:
                        print(f"[WIPO] Detaliu TMview {s}: {type(e).__name__}: {e}")
            await asyncio.gather(*(one(s) for s in todo))
    except Exception as e:
        # TMview indisponibil — clasele lipsă vin apoi din Madrid Monitor.
        print(f"[WIPO] Sesiune TMview: {type(e).__name__}: {e}")
    if fresh:
        await loop.run_in_executor(None, export_cache.save_details, fresh)
    found.update(fresh)
    return found


_RE_NICE_HEAD = re.compile(r'<td class="nice">(.*?)</td>', re.S)
_RE_GS = re.compile(r'<dd class="GSGR BASICGS" nice="(\d+)">(.*?)</dd>', re.S)
_RE_GS_EN = re.compile(r'<p class="gsterm[^"]*"[^>]*lang="EN">(.*?)</p>', re.S)


def _monitor_goods(irn: str) -> List[Dict]:
    """Clasele Nisa și produsele (în engleză) din fișa Madrid Monitor a înregistrării internaționale."""
    try:
        r = requests.get(MONITOR_URL.format(irn=irn.lstrip("0")), headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[WIPO] Fișa Madrid Monitor {irn}: {type(e).__name__}: {e}")
        return []
    text = r.text
    goods = {}
    for cls, body in _RE_GS.findall(text):
        en = _RE_GS_EN.search(body)
        term = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", en.group(1) if en else body))).strip()
        goods.setdefault(str(int(cls)), term)
    head = _RE_NICE_HEAD.search(text)
    for cls in re.findall(r"\b\d{1,2}\b", re.sub(r"<[^>]+>", " ", head.group(1)) if head else ""):
        goods.setdefault(str(int(cls)), "")
    return [{"niceClass": c, "goodsAndServices": t} for c, t in sorted(goods.items(), key=lambda kv: int(kv[0]))]


def _fill_from_monitor(irns: List[str], details: Dict[str, Dict]) -> int:
    """Completează clasele lipsă din Madrid Monitor; întoarce câte mărci au fost completate."""
    todo = [i for i in irns if not any(g.get("niceClass") for g in (details.get(st13(i)) or {}).get("goodAndServices") or [])]
    if not todo:
        return 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = dict(zip(todo, ex.map(_monitor_goods, todo)))
    filled = 0
    for irn, goods in res.items():
        if goods:
            details.setdefault(st13(irn), {})["goodAndServices"] = goods
            filled += 1
    return filled


def _to_mark(doc: Dict, designations: List[str], day: date, detail: Dict) -> Dict:
    irn = str(doc.get("irn") or "")
    goods = [g for g in (detail.get("goodAndServices") or []) if g.get("niceClass")]
    apps = detail.get("applicants_detail") or []
    holder = doc.get("holder") or (apps[0].get("name") if apps else "")
    address = ""
    if apps and apps[0].get("address"):
        a = apps[0]
        address = ", ".join(p for p in [a.get("name") or holder, a["address"].replace("\n", ", "), a.get("country") or ""] if p)
    reps = detail.get("representatives") or []
    return {
        "ST13": st13(irn),
        "tmName": doc.get("brand") or "",
        "tmOffice": "WO",
        "applicationNumber": irn,
        "applicantName": [holder] if holder else [],
        "applicantAddress": address,
        "representatives": reps if isinstance(reps, list) else [],
        "niceClass": sorted({int(g["niceClass"]) for g in goods if str(g["niceClass"]).isdigit()}),
        "goodAndServices": goods,
        "markImageURI": IMAGE_URL.format(path=doc["image"]) if doc.get("image") else "",
        "applicationDate": (detail.get("registrationDate") or "")[:10],
        "publicationDate": day.isoformat(),
        "markFeature": detail.get("markFeature") or "",
        "designations": designations,
        # Toate țările desemnate în înregistrare / desemnarea ulterioară (EM = Uniunea Europeană).
        "designatedCountries": sorted({c.strip() for c in (doc.get("dcpcd") or "").split("~") if c.strip()}
                                      | set(designations)),
        "transaction": TRANSACTIONS.get(doc.get("type"), doc.get("type") or ""),
        "originOffice": doc.get("oocd") or "",
        "officeUrl": MONITOR_URL.format(irn=irn),
        "_source": "wipo_gazette",
        "_detail_enriched": bool(goods),
    }


def fetch_wipo_for_date(td: date) -> Tuple[List[Dict], Dict]:
    """Mărcile din gazeta WIPO a săptămânii lui `td` care desemnează RO sau UE."""
    day = gazette_day(td)
    issue = next((i["issue"] for i in gazette_issues(day.year) if i["date"] == day), None)
    if issue is None:
        return [], {"error": f"Gazeta WIPO din {day:%d.%m.%Y} nu a fost publicată (încă) sau nu există "
                             f"(gazeta apare săptămânal, joia).", "day": day.isoformat()}
    by_irn: Dict[str, Dict] = {}
    desig: Dict[str, set] = {}
    for country in COUNTRIES:
        for ttype in TRANSACTIONS:
            for doc in _query(day.year, issue, country, ttype):
                irn = str(doc.get("irn") or "")
                if not irn:
                    continue
                by_irn.setdefault(irn, doc)
                desig.setdefault(irn, set()).add(country)
    details = asyncio.run(_details([st13(i) for i in by_irn]))
    from_monitor = _fill_from_monitor(list(by_irn), details)
    marks = [_to_mark(doc, sorted(desig[irn]), day, details.get(st13(irn)) or {})
             for irn, doc in sorted(by_irn.items())]
    return marks, {"issue": issue, "day": day.isoformat(), "marks": len(marks),
                   "with_classes": sum(1 for m in marks if m["niceClass"]), "classes_from_monitor": from_monitor}
