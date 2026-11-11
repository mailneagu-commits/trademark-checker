"""
Litigiile privind mărcile (anulare, decădere, contrafacere…) din Portalul instanțelor de judecată
(portal.just.ro) — serviciul web public al Ministerului Justiției, fără autentificare.

Tribunalul București judecă în primă instanță cererile de anulare / decădere (Legea 84/1998), Curtea de
Apel București apelurile. Dosarele se leagă de mărcile din registru după nr. mărcii din obiect („anulare
marcă nr. 164556”) sau, cu mențiunea „de verificat”, după numele titularului aflat printre părți.
"""
import re
import threading
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Dict, List

import requests

from db import CourtCase, CourtLink, Mark, SessionLocal, norm

PORTAL = "http://portalquery.just.ro/query.asmx"
INSTITUTIONS = {"TribunalulBUCURESTI": "Tribunalul București", "CurteadeApelBUCURESTI": "Curtea de Apel București"}
FIRST_YEAR = 2008
NS = "{portalquery.just.ro}"
# Forme juridice și cuvinte de legătură care nu deosebesc un titular de altul.
_FORMS = {"sc", "srl", "sa", "s", "r", "l", "a", "snc", "pfa", "ii", "ltd", "llc", "inc", "gmbh", "ag", "bv",
          "spa", "sas", "co", "company", "corporation", "corp", "limited", "si", "and"}

job: Dict = {"running": False, "cases": 0, "links": 0, "last": None, "error": ""}


def party_key(name: str) -> str:
    """Cheia de potrivire a unui titular / unei părți: fără domiciliul ales, fără forma juridică."""
    n = re.split(r"\s+-\s+|\bcu dom|\bprin\b|\bdomiciliu", name or "", flags=re.I)[0]
    words = [w for w in norm(n).split() if w not in _FORMS]
    return " ".join(words)


def _soap(obiect: str, inst: str, start: date, stop: date) -> List[ET.Element]:
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<CautareDosare xmlns="portalquery.just.ro"><obiectDosar>{obiect}</obiectDosar><institutie>{inst}</institutie>
<dataStart>{start.isoformat()}T00:00:00</dataStart><dataStop>{stop.isoformat()}T23:59:59</dataStop></CautareDosare>
</soap:Body></soap:Envelope>"""
    r = requests.post(PORTAL, data=body.encode("utf-8"), timeout=180,
                      headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"portalquery.just.ro/CautareDosare"'})
    r.raise_for_status()
    return ET.fromstring(r.content).iter(NS + "Dosar")


def _t(el, tag: str) -> str:
    x = el.find(NS + tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def _case(el) -> Dict:
    parti = [{"nume": _t(p, "nume"), "calitate": _t(p, "calitateParte")} for p in el.iter(NS + "DosarParte")]
    sedinte = []
    for s in el.iter(NS + "DosarSedinta"):
        sedinte.append({"data": _t(s, "data")[:10], "ora": _t(s, "ora"), "complet": _t(s, "complet"),
                        "solutie": _t(s, "solutie"), "sumar": _t(s, "solutieSumar")[:2000],
                        "document": _t(s, "documentSedinta"), "nr_document": _t(s, "numarDocument")})
    sedinte.sort(key=lambda s: s["data"], reverse=True)
    cai = [{"tip": _t(c, "tipCaleAtac"), "data": _t(c, "dataDeclarare")[:10], "parte": _t(c, "parteDeclaratoare").strip(" ,")}
           for c in el.iter(NS + "DosarCaleAtac")]
    obiect = _t(el, "obiect")
    return {"numar": _t(el, "numar"), "institutie": _t(el, "institutie"), "data": _t(el, "data")[:10],
            "departament": _t(el, "departament"), "stadiu": _t(el, "stadiuProcesualNume") or _t(el, "stadiuProcesual"),
            "categorie": _t(el, "categorieCaz"), "obiect": obiect, "modificat": _t(el, "dataModificare")[:19],
            "parti": parti, "sedinte": sedinte[:40], "cai_atac": cai,
            # „marcă nr. 164556”, „marca nr.164556”, „M 2019 01234”
            "mark_regs": sorted(set(re.findall(r"\bnr\.?\s*(\d{5,6})\b(?![/.]\d)", obiect, flags=re.I))),
            "mark_apps": sorted({("M" + a + b) for a, b in re.findall(r"\bM\s*(\d{4})\s*(\d{5})\b", obiect)})}


def _is_trademark(c: Dict) -> bool:
    o = norm(c["obiect"])
    return bool(re.search(r"\bmarc(a|i|ii|ilor)?\b", o)) or (
        c["categorie"] == "ProprietateIntelectuala" and "contrafacere" in o)


def sync(full: bool = False) -> None:
    """Dosarele cu obiect „marcă” din ultimii 3 ani (săptămânal) sau din 2008 încoace (prima dată)."""
    if job["running"]:
        return
    job.update(running=True, error="")
    try:
        today = date.today()
        first = FIRST_YEAR if full else today.year - 2
        seen: Dict[str, Dict] = {}
        for inst in INSTITUTIONS:
            for y in range(first, today.year + 1):
                for start, stop in ((date(y, 1, 1), date(y, 6, 30)), (date(y, 7, 1), date(y, 12, 31))):
                    if start > today:
                        continue
                    # Căutarea după obiect potrivește cuvinte întregi (fără diacritice): „marca”, „marci”.
                    for term in ("marca", "marci", "contrafacere"):
                        try:
                            for el in _soap(term, inst, start, min(stop, today)):
                                c = _case(el)
                                if c["numar"] and _is_trademark(c):
                                    seen[f"{c['institutie']}|{c['numar']}"] = c
                        except Exception as e:
                            print(f"[COURTS] {inst} {term} {start}: {type(e).__name__}: {e}")
        db = SessionLocal()
        try:
            for key, c in seen.items():
                db.merge(CourtCase(id=key, numar=c["numar"], institutie=c["institutie"], data=c["data"],
                                   departament=c["departament"], stadiu=c["stadiu"], obiect=c["obiect"],
                                   modificat=c["modificat"], parti=c["parti"], sedinte=c["sedinte"],
                                   cai_atac=c["cai_atac"], mark_regs=c["mark_regs"], mark_apps=c["mark_apps"],
                                   updated_at=datetime.utcnow()))
            db.commit()
            job["cases"] = db.query(CourtCase).count()
        finally:
            db.close()
        job["links"] = link_cases()
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
        print(f"[COURTS] {job['error']}")
    finally:
        job.update(running=False, last=datetime.utcnow().isoformat() + "Z")


def link_cases() -> int:
    """Legăturile dosar ↔ marcă: după nr. mărcii / nr. depozitului din obiect (sigur) și după titular
    (de verificat — titularul poate avea mai multe mărci)."""
    db = SessionLocal()
    try:
        cases = db.query(CourtCase).all()
        db.query(CourtLink).delete()
        links = set()
        by_party: Dict[str, List[str]] = {}
        for c in cases:
            for reg in c.mark_regs or []:
                for (app,) in db.query(Mark.app_number).filter(Mark.reg_number.in_([reg, reg.zfill(6), reg.lstrip("0")])):
                    links.add((c.id, app, "nr"))
            for app in c.mark_apps or []:
                if db.get(Mark, app) is not None:
                    links.add((c.id, app, "nr"))
            if not c.mark_regs and not c.mark_apps:
                for p in c.parti or []:
                    k = party_key(p.get("nume"))
                    if len(k) >= 4 and not k.startswith("oficiul de stat"):
                        by_party.setdefault(k, []).append(c.id)
        if by_party:
            hits: Dict[str, List[str]] = {}
            for app, holder in db.query(Mark.app_number, Mark.holder).yield_per(5000):
                k = party_key(holder)
                if k in by_party:
                    hits.setdefault(k, []).append(app)
            # Un titular cu multe mărci: dosarul nu se poate atribui uneia anume — nu-l lipim pe toate.
            for k, apps in hits.items():
                if len(apps) <= 15:
                    for cid in by_party[k]:
                        for app in apps:
                            links.add((cid, app, "titular"))
        for cid, app, kind in links:
            db.add(CourtLink(case_id=cid, app_number=app, kind=kind))
        db.commit()
        return len(links)
    finally:
        db.close()


def start_backfill_if_empty() -> None:
    db = SessionLocal()
    try:
        empty = db.query(CourtCase).first() is None
    finally:
        db.close()
    if empty:
        threading.Thread(target=sync, kwargs={"full": True}, daemon=True).start()
    else:
        # Legăturile se refac la pornire (regulile de potrivire se pot schimba între versiuni).
        threading.Thread(target=lambda: job.update(links=link_cases()), daemon=True).start()
