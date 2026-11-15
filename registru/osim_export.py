"""Exporturile XML din registrul OSIM (WIPO ST.66, „RO Trade Mark Complete”), salvate manual din
https://api.osim.ro:8443/tm-registry/ și puse în folderul osim_export/. BOPI rămâne sursa principală:
exportul doar completează ce lipsește (mărci absente, nr. / data înregistrării, denumire, clase, produse)."""
import glob
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from datetime import date
from typing import Dict, Iterator

from db import Mark, SessionLocal, SourceFile, norm

FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "osim_export")
NS = "{http://www.wipo.int/standards/XMLSchema/trademarks}"

# Starea din registru → starea ProWatch (fără diacritice și litere mici, după prefix).
_STATUS = [("inregistrat", "Înregistrată"), ("decazut", "Expirată"), ("expirat", "Expirată"),
           ("respins", "Respinsă"), ("retras", "Retrasă"), ("publicat", "Cerere publicată"),
           ("admis", "Admisă la înregistrare")]


def _plain(s: str) -> str:
    return norm(s or "").replace(" ", "")


def _status(code: str) -> str:
    c = _plain(code)
    return next((v for k, v in _STATUS if c.startswith(k)), "")


def _key(app: str) -> str:
    a = re.sub(r"\s+", "", app or "").upper()
    if re.fullmatch(r"M\d{9}", a):
        return a
    return a.zfill(6) if a.isdigit() else ""


def records(path: str) -> Iterator[Dict]:
    for _, el in ET.iterparse(path):
        if el.tag != NS + "TradeMark":
            continue
        g = lambda p: (el.findtext(NS + p) or "").strip()
        goods = {}
        for cd in el.iter(NS + "ClassDescription"):
            c = (cd.findtext(NS + "ClassNumber") or "").strip().lstrip("0")
            if c.isdigit() and 1 <= int(c) <= 45:
                goods[c] = (cd.findtext(NS + "GoodsServicesDescription") or "").strip()
        yield {"app": _key(g("ApplicationNumber")), "filing": g("ApplicationDate")[:10],
               "reg": g("RegistrationNumber").lstrip("0"), "reg_date": g("RegistrationDate")[:10],
               "status_code": g("MarkCurrentStatusCode"), "status_date": g("MarkCurrentStatusDate")[:10],
               "type": g("MarkFeature"), "name": (el.findtext(f".//{NS}MarkVerbalElementText") or "").strip(),
               "image": el.findtext(f".//{NS}MarkImageFilename") or "", "goods": goods}
        el.clear()


def _apply(db, r: Dict, label: str, today: str) -> str:
    """'new' / 'filled' / ''"""
    from ingest import expiry_of
    if not r["app"]:
        return ""
    status = _status(r["status_code"])
    ev = {"date": today, "bopi": "Registrul OSIM", "chapter": "Date din exportul registrului OSIM",
          "detail": f"{label} · stare în registru: {r['status_code']}"
                    + (f" ({r['status_date']})" if r["status_date"] else "")}
    m = db.get(Mark, r["app"])
    if m is None:
        m = Mark(app_number=r["app"], name=r["name"], name_norm=norm(r["name"]), mark_type=r["type"],
                 status=status, status_date=r["status_date"], filing_date=r["filing"],
                 reg_number=r["reg"], reg_date=r["reg_date"], classes=sorted(int(c) for c in r["goods"]),
                 goods=r["goods"], holder="", holder_norm="", holder_address="", events=[ev])
        m.expiry_date = expiry_of(m)
        db.add(m)
        return "new"
    changed = False
    for f, v in (("name", r["name"]), ("mark_type", r["type"]), ("filing_date", r["filing"]),
                 ("reg_number", r["reg"]), ("reg_date", r["reg_date"])):
        if v and not getattr(m, f):
            setattr(m, f, v)
            changed = True
    if r["name"] and not m.name_norm:
        m.name_norm = norm(m.name)
    if r["goods"] and not m.goods:
        m.goods, m.classes = r["goods"], sorted(int(c) for c in r["goods"])
        changed = True
    if status and m.status in ("", "Cerere publicată", "Admisă la înregistrare") and status != m.status:
        m.status, m.status_date = status, r["status_date"] or m.status_date
        changed = True
    if changed:
        if m.status == "Înregistrată" and not m.expiry_date:
            m.expiry_date = expiry_of(m)
        m.events = (m.events or []) + [ev]
    return "filled" if changed else ""


def import_all() -> None:
    """Fiecare fișier o singură dată (marcaj după conținut); un fișier nou / schimbat se citește la pornire."""
    today = date.today().isoformat()
    for path in sorted(glob.glob(os.path.join(FOLDER, "*.xml"))):
        with open(path, "rb") as f:
            marker = "osim-export:" + hashlib.sha1(f.read()).hexdigest()
        name = os.path.basename(path)
        db = SessionLocal()
        try:
            if db.get(SourceFile, marker):
                continue
            n = {"new": 0, "filled": 0, "": 0}
            for i, r in enumerate(records(path), 1):
                n[_apply(db, r, f"export {name}", today)] += 1
                if i % 500 == 0:
                    db.commit()
            db.add(SourceFile(url=marker, kind="migration", status="ok", marks=n["new"] + n["filled"],
                              label=f"Export registru OSIM {name}: {n['new']} noi, {n['filled']} completate"))
            db.commit()
            print(f"[OSIM-XML] {name}: {n}")
        except Exception as e:
            db.rollback()
            print(f"[OSIM-XML] {name}: {type(e).__name__}: {e}")
        finally:
            db.close()
