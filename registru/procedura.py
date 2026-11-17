"""
Etapele procedurii OSIM pentru o cerere de marcă (Legea 84/1998, procedura de după 2020):

  1. publicarea cererii în buletinul zilnic  → 2 luni de OBSERVAȚII;
  2. după observații                         → EXAMINARE, până la publicarea în „Cereri admise”;
  3. publicarea în „Cereri admise” (bilunar)  → 2 luni de OPOZIȚIE;
  4. după opoziție                           → în așteptarea înregistrării, apoi ÎNREGISTRATĂ
                                                (publicată în buletinul lunar).

Termenul de 2 luni (art. 181 Cod procedură civilă): se împlinește în ziua corespunzătoare din luna a
doua; dacă ziua nu există, în ultima zi a lunii; dacă ultima zi e nelucrătoare (sâmbătă, duminică,
sărbătoare legală), se prelungește până la prima zi lucrătoare.

Etapele se calculează doar pentru cererile publicate din 2022 (formatul actual al buletinelor);
pentru cele mai vechi rămâne starea din buletine / TMview.
"""
from __future__ import annotations

import calendar
import os
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Dict, List, Optional

OBS = "În perioada de observații"
EXAM = "În examinare"
OPP = "În perioada de opoziție"
OPP_CLOSED = "Perioadă de opoziție închisă – în așteptarea înregistrării"
REG = "Înregistrată"
STATUSES = (OBS, EXAM, OPP, OPP_CLOSED)
# Starea finală din buletine — oprește fluxul (o admitere după respingere, la contestație, îl reia).
TERMINAL = ("Respinsă", "Retrasă", "Radiată")

FROM = os.environ.get("PROC_FROM", "2022-01-01")       # publicări de la care se calculează etapele
WORKING_DAY_SHIFT = os.environ.get("PROC_WORKING_DAY", "1") == "1"
LATE_DAYS = 90       # opoziție închisă de peste 3 luni, fără înregistrare → posibilă opoziție depusă

PUBLISHED = "Cereri de marcă publicate"
ADMITTED = "Cereri de marcă admise la înregistrare"
REGISTERED = "Mărci înregistrate"


# ── Zile nelucrătoare ────────────────────────────────────────────────────────────

def _orthodox_easter(y: int) -> date:
    """Paștele ortodox (algoritmul iulian al lui Meeus, convertit în calendarul gregorian; 1900–2099)."""
    a, b, c = y % 4, y % 7, y % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return date(y, month, day + 1) + timedelta(days=13)


@lru_cache(maxsize=None)
def holidays(y: int) -> frozenset:
    """Sărbătorile legale din România (Codul muncii, art. 139)."""
    fixed = [(1, 1), (1, 2), (1, 24), (5, 1), (6, 1), (8, 15), (11, 30), (12, 1), (12, 25), (12, 26)]
    if y >= 2024:
        fixed += [(1, 6), (1, 7)]           # Boboteaza, Sf. Ioan — din 2024
    days = {date(y, m, d) for m, d in fixed}
    easter = _orthodox_easter(y)
    days |= {easter, easter + timedelta(days=1), easter + timedelta(days=49), easter + timedelta(days=50)}
    if y >= 2018:
        days.add(easter - timedelta(days=2))    # Vinerea Mare — din 2018
    return frozenset(days)


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def add_months(start: date, months: int = 2) -> date:
    """Ziua corespunzătoare peste `months` luni; dacă nu există (31 → 30 / 28), ultima zi a lunii."""
    k = start.year * 12 + start.month - 1 + months
    y, m = k // 12, k % 12 + 1
    return date(y, m, min(start.day, calendar.monthrange(y, m)[1]))


def term_end(start: str, months: int = 2) -> str:
    """Sfârșitul termenului de `months` luni de la data `start` (YYYY-MM-DD), inclusiv prelungirea la
    prima zi lucrătoare."""
    if not start:
        return ""
    try:
        d = add_months(date.fromisoformat(start[:10]), months)
    except ValueError:
        return ""
    while WORKING_DAY_SHIFT and not is_working_day(d):
        d += timedelta(days=1)
    return d.isoformat()


# ── Etapele unei mărci ───────────────────────────────────────────────────────────

def _first(events: List[Dict], chapter: str) -> str:
    return min((e.get("date") or "" for e in events if e.get("chapter") == chapter and e.get("date")), default="")


def _admise_date(events: List[Dict]) -> str:
    """Data buletinului „Cereri admise”: lista bilunară (data exactă) are prioritate față de capitolul
    din buletinul lunar (datat la sfârșitul lunii)."""
    lists = [e["date"] for e in events if e.get("chapter") == ADMITTED and e.get("date")
             and str(e.get("bopi", "")).startswith("Cereri admise")]
    return min(lists) if lists else _first(events, ADMITTED)


def compute(m, today: Optional[str] = None) -> Dict[str, str]:
    """Datele și statutul procedurii pentru marca `m` (rând Mark). Nu modifică marca."""
    today = today or date.today().isoformat()
    events = m.events or []
    pub = m.publication_date or _first(events, PUBLISHED)
    out = {"pub_date": pub or "", "obs_end": "", "admise_date": "", "opp_end": "",
           "reg_pub_date": "", "proc_status": ""}
    admise = _admise_date(events)
    # Cererile publicate din 2022 și cele admise din 2022 (publicate mai devreme sau lipsă din buletinul zilnic).
    if max(pub or "", admise or "") < FROM:
        return out
    out["obs_end"] = term_end(pub)
    out["admise_date"] = admise
    out["opp_end"] = term_end(out["admise_date"])
    out["reg_pub_date"] = _first(events, REGISTERED) or (m.reg_date if m.status == REG else "") or ""
    if m.status == REG or out["reg_pub_date"]:
        out["proc_status"] = REG
    elif m.status in TERMINAL:
        out["proc_status"] = m.status
    elif out["admise_date"]:
        out["proc_status"] = OPP if today <= out["opp_end"] else OPP_CLOSED
    else:
        out["proc_status"] = OBS if today <= out["obs_end"] else EXAM
    return out


def anomalies(m, p: Dict[str, str], today: Optional[str] = None) -> List[str]:
    """Situațiile care nu se încadrează în flux, pentru raportul de verificare."""
    today = today or date.today().isoformat()
    out = []
    if not (p.get("obs_end") or p.get("admise_date")):
        return out
    if p["reg_pub_date"] and not p["admise_date"]:
        out.append("înregistrată fără publicare în „Cereri admise”")
    if p["admise_date"] and p["pub_date"] and p["admise_date"] < p["pub_date"]:
        out.append("admisă înainte de publicarea cererii")
    if p["admise_date"] and p["obs_end"] and p["admise_date"] < p["obs_end"]:
        out.append("admisă înainte de sfârșitul observațiilor")
    if p["reg_pub_date"] and p["opp_end"] and p["reg_pub_date"] < p["opp_end"]:
        out.append("înregistrată înainte de sfârșitul opoziției")
    if p["proc_status"] == OPP_CLOSED and p["opp_end"] and \
            (date.fromisoformat(today) - date.fromisoformat(p["opp_end"])).days > LATE_DAYS:
        out.append("opoziție închisă de peste 3 luni, fără înregistrare (posibilă opoziție depusă)")
    if not any(e.get("chapter") == PUBLISHED for e in (m.events or [])) and \
            (p["admise_date"] or p["reg_pub_date"]):
        out.append("lipsea din buletinul zilnic — adăugată din „Cereri admise” / buletinul lunar")
    return out


_ORDER = {"": 0, OBS: 1, EXAM: 2, OPP: 3, OPP_CLOSED: 4, REG: 5}


def apply(db, m, source: str = "automat", today: Optional[str] = None) -> bool:
    """Recalculează și salvează etapele mărcii; schimbarea de statut se trece în jurnal. Statutul
    merge doar înainte — cu excepția stărilor finale din buletine (respingere / retragere), a
    reluării după contestație și a corecției manuale a administratorului (care are prioritate)."""
    from db import ProcLog
    p = compute(m, today)
    old = m.proc_status or ""
    new = p["proc_status"]
    if m.proc_manual:
        new = m.proc_manual
    elif old in _ORDER and new in _ORDER and _ORDER[new] < _ORDER[old] and \
            not (old == REG and m.status != REG):
        new = old                     # nu înapoi (ex. o fișă recitită dintr-un buletin mai vechi)
    for k in ("obs_end", "admise_date", "opp_end", "reg_pub_date"):
        setattr(m, k, p[k])
    if new != old:
        m.proc_status = new
        db.add(ProcLog(app_number=m.app_number, old=old, new=new, source=source, at=datetime.utcnow()))
        return True
    return False
