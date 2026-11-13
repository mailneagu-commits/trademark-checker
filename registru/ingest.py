"""
Construirea și actualizarea bazei de mărci din buletinele BOPI publicate pe osim.ro.

Sursele (2022 →): buletinele zilnice (cereri publicate), listele „cereri admise” (2023 →) și
buletinele lunare (cap. 1 înregistrate, cap. 2 respinse). Se procesează în ordine
cronologică, câte un PDF, iar fiecare PDF terminat e notat în `source_files`, ca o repornire
să continue de unde a rămas. Rulează într-un fir de fundal al serverului.
"""
import os
import re
import tempfile
import threading
import traceback
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import requests

from bopi_parser import display_app, fix_record, parse_changes, parse_pdf, party_name
from db import Mark, MarkChange, MarkImage, SessionLocal, SourceFile, norm

OSIM = "https://www.osim.ro"
INDEX_PAGE = OSIM + "/buletinul-oficial-de-proprietate-industriala-marci"
DAILY_URL = OSIM + "/images/Publicatii/Marci/{y}/bopi{d:%d%m%Y}.pdf"
START = date(2022, 1, 1)                 # buletine zilnice + formatul lunar actual
ARCHIVE_START = int(os.environ.get("ARCHIVE_START", "2001"))   # arhiva lunară mai veche
UA = {"User-Agent": "Mozilla/5.0 (registru marci; contact: protectmark)"}

STATUS = {
    "published":  "Cerere publicată",
    "admitted":   "Admisă la înregistrare",
    "registered": "Înregistrată",
    "rejected":   "Respinsă",
}
_RANK = {"Cerere publicată": 1, "Admisă la înregistrare": 2, "Respinsă": 3, "Înregistrată": 4}
CHAPTER_LABEL = {
    "published":  "Cereri de marcă publicate",
    "admitted":   "Cereri de marcă admise la înregistrare",
    "registered": "Mărci înregistrate",
    "rejected":   "Cereri de marcă respinse",
}

job: Dict = {"running": False, "current": "", "done": 0, "total": 0, "marks": 0,
             "started_at": None, "finished_at": None, "error": ""}
_lock = threading.Lock()


# ── Lista surselor ──────────────────────────────────────────────────────────────

def _month_end(y: int, m: int) -> date:
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))


def list_sources(today: Optional[date] = None) -> List[Dict]:
    today = today or date.today()
    out: List[Dict] = []
    html = requests.get(INDEX_PAGE, headers=UA, timeout=60).text
    for href in sorted(set(re.findall(r'href="([^"]+\.pdf)"', html))):
        url = href if href.startswith("http") else OSIM + href
        name = href.rsplit("/", 1)[-1].lower()
        m = re.match(r"cereri_admise_(\d{2})(\d{2})(\d{4})\.pdf$", name)
        if m:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if d >= START:
                out.append({"url": url, "kind": "admise", "date": d,
                            "label": f"Cereri admise {d:%d.%m.%Y}"})
            continue
        ym = _archive_month(href)
        if ym:
            y, mo, part = ym
            if y < ARCHIVE_START:
                continue
            out.append({"url": url, "kind": "monthly", "date": _month_end(y, mo),
                        "label": f"BOPI {mo:02d}/{y}{part}"})
            # Tabelele aceluiași buletin (transferuri, reînnoiri, radieri...) — pas separat.
            out.append({"url": url + "#changes", "kind": "changes", "date": _month_end(y, mo),
                        "label": f"BOPI {mo:02d}/{y}{part}"})
    # Buletinele lunare 09–10/2022 au și fișierul întreg, și părțile lui: părțile se sar (dubluri).
    whole = {(x["kind"], x["date"]) for x in out if x["kind"] in ("monthly", "changes") and " partea" not in x["label"]}
    out = [x for x in out if " partea" not in x["label"] or (x["kind"], x["date"]) not in whole]
    d = START
    while d <= today:
        if d.weekday() < 5:
            out.append({"url": DAILY_URL.format(y=d.year, d=d), "kind": "daily", "date": d,
                        "label": f"BOPI {d:%d.%m.%Y}"})
        d += timedelta(days=1)
    # Întâi perioada curentă (2022 →), cronologic; apoi arhiva, de la nou la vechi, ca mărcile
    # valabile recente să apară primele. Aceeași zi: publicare, admitere, buletin lunar, tabele.
    order = {"daily": 0, "admise": 1, "monthly": 2, "changes": 3}
    cur = sorted((x for x in out if x["date"] >= START), key=lambda s: (s["date"], order[s["kind"]], s["url"]))
    old = sorted((x for x in out if x["date"] < START), key=lambda s: (-s["date"].toordinal(), order[s["kind"]], s["url"]))
    return cur + old


def _archive_month(href: str):
    """(an, lună, parte) pentru un buletin lunar: mrc_12_2025.pdf, mrc_12-partea-II_2022.pdf,
    mrc_01_09_full.pdf, mrc_01-17.pdf, mrc_8_2016.pdf, mrc1201.pdf, mrc01041.pdf …"""
    name = href.rsplit("/", 1)[-1].lower()
    if not name.startswith("mrc"):
        return None
    folder = href.rsplit("/", 2)[-2] if href.count("/") >= 2 else ""
    fy = re.match(r"^(\d{4})m?$", folder)
    m = re.match(r"mrc_(\d{1,2})[-_](?:partea-(\w+)_)?(\d{4})\.pdf$", name)
    if m:
        return int(m.group(3)), int(m.group(1)), (f" partea {m.group(2).upper()}" if m.group(2) else "")
    m = re.match(r"mrc_?(\d{1,2})[-_]?(\d{2})(?:\d)?(?:_full)?\.pdf$", name)
    if m:
        mo, yy = int(m.group(1)), int(m.group(2))
        y = int(fy.group(1)) if fy else 2000 + yy
        if 1 <= mo <= 12:
            return y, mo, ""
    m = re.match(r"mrc_?(\d{1,2})[-_](\d{4})\.pdf$", name)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(2)), int(m.group(1)), ""
    return None


# ── Salvarea unei mărci ─────────────────────────────────────────────────────────

def _add_years(iso: str, years: int) -> str:
    try:
        d = date.fromisoformat(iso)
        try:
            return d.replace(year=d.year + years).isoformat()
        except ValueError:
            return d.replace(year=d.year + years, day=28).isoformat()
    except ValueError:
        return ""


def _upsert(db, chapter: str, rec: Dict, src: Dict) -> None:
    rec = fix_record(rec)                      # diacriticele fonturilor vechi BOPI („Õi” → „şi”)
    app = rec["app_number"]
    m = db.get(Mark, app)
    if m is None and rec.get("_status_only"):
        return                      # respingere doar în tabel, pentru o cerere care nu e în bază
    if m is None:
        m = Mark(app_number=app, events=[], classes=[], goods={})
        db.add(m)
        db.flush()
    bdate = src["date"].isoformat()
    # Arhiva se procesează după buletinele noi: un buletin mai vechi decât ce știm deja despre
    # marcă doar completează câmpurile goale, nu le suprascrie.
    newest = max((e.get("date", "") for e in (m.events or [])), default="")
    is_newer = bdate >= newest
    # (151) apare la toate tipurile de buletin și e data de depozit a cererii.
    if rec.get("date_151") and not m.filing_date:
        m.filing_date = rec["date_151"]
    if rec.get("filing_date"):
        m.filing_date = rec["filing_date"]
    # Denumirea publicată + un text în plus (notificare / decizie OSIM tipărită în câmpul (540) al
    # capitolelor următoare): rămâne denumirea publicată, restul e declarație.
    if rec.get("name") and m.name and rec["name"] != m.name and rec["name"].startswith(m.name + " "):
        rest = rec["name"][len(m.name):].strip()
        if len(rest) > 40 and re.search(r"\bart\.?\s*\d|\bnr\.?\s*\d|OSIM|NOTIFICARE|drept exclusiv|Legea|"
                                        r"Comisi|contesta|refuz", rest, re.I):
            rec = dict(rec, name=m.name, disclaimer=rec.get("disclaimer") or rest)
    # Denumirea din buletinul zilnic (cererea publicată, cu tabelul-sumar) e cea de referință: capitolele
    # următoare (admise, înregistrate) n-o mai înlocuiesc — acolo fișa are adesea text OSIM în (540).
    published = CHAPTER_LABEL["published"]
    keep_name = chapter != "published" and bool(m.name) and \
        any(e.get("chapter") == published and e.get("name") for e in (m.events or []))
    for f in ("name", "representative", "representative_address", "vienna", "colors", "disclaimer"):
        if f == "name" and keep_name:
            continue
        if f == "name" and chapter == "published" and rec.get("name") and \
                not any(e.get("chapter") == CHANGE_LABEL["representation"] for e in (m.events or [])):
            m.name = rec["name"]           # și la recitirea unui buletin mai vechi decât ultimul eveniment
            continue
        if rec.get(f) and (is_newer or not getattr(m, f)):
            setattr(m, f, rec[f])
    if rec.get("holder") and not m.holder:
        m.holder, m.holder_address = rec["holder"], rec.get("holder_address") or rec["holder"]
    if rec.get("goods") and (is_newer or not m.goods):
        m.goods = rec["goods"]
        m.classes = rec["classes"]
    # Denumirea se păstrează întreagă, oricât de lungă (cerință: nicio denumire trunchiată).
    m.name, m.holder = (m.name or ""), (m.holder or "")
    m.representative = (m.representative or "")
    m.name_norm = norm(m.name)
    m.holder_norm = norm(m.holder)
    m.rep_norm = norm(m.representative)
    _current_rep(m)
    if chapter == "published" and not m.publication_date:
        m.publication_date = bdate
    if chapter == "registered":
        m.reg_number = rec.get("reg_number") or m.reg_number
        m.reg_date = m.reg_date if (m.reg_date and m.reg_date < bdate) else bdate
    new_status = STATUS.get(chapter, "")
    # O admitere publicată după o respingere (contestație admisă) schimbă starea înapoi în „Admisă”.
    if new_status and bdate >= (m.status_date or "") and \
            (_RANK.get(new_status, 0) >= _RANK.get(m.status, 0) or chapter == "rejected" or
             (chapter == "admitted" and m.status == "Respinsă" and bdate > (m.status_date or ""))):
        if not (m.status == "Înregistrată" and chapter != "registered"):
            m.status, m.status_date = new_status, bdate
    elif new_status and not m.status:
        m.status, m.status_date = new_status, bdate
    if rec.get("image") and not m.has_image:
        db.merge(MarkImage(app_number=app, data=rec["image"], content_type="image/webp"))
        m.has_image = True
    m.mark_type = ("Combinată" if m.has_image and m.name else
                   "Figurativă" if m.has_image else "Verbală")
    ev = {"date": bdate, "bopi": src["label"], "chapter": CHAPTER_LABEL.get(chapter, chapter)}
    if rec.get("holder"):
        # Titularul la data acestui buletin — titularul afișat e cel din documentul cel mai recent.
        ev["holder"], ev["holder_address"] = rec["holder"], rec.get("holder_address") or rec["holder"]
    # Instantaneu pentru istoricul mărcii (ce s-a schimbat de la un document la altul).
    if rec.get("classes"):
        ev["classes"] = rec["classes"]
    if rec.get("name"):
        ev["name"] = rec["name"]
    if rec.get("reg_number"):
        ev["reg_number"] = rec["reg_number"]
    if rec.get("holder") or rec.get("name"):
        # Reprezentantul (740) la data documentului — gol = fără reprezentant; așa se vede în istoric
        # dacă un mandatar a fost înscris ulterior.
        ev["representative"] = rec.get("representative") or ""
    _add_event(m, ev)
    if chapter == "registered":
        _merge_stub(db, m)
        _apply_pending(db, m)
    _recompute_holder(m)
    m.expiry_date = expiry_of(m)
    m.updated_at = datetime.utcnow()


def _apply_limitation(m: Mark, c: MarkChange) -> str:
    """Limitarea la cererea titularului: produsele claselor modificate se înlocuiesc cu lista
    nouă, clasele șterse dispar, celelalte rămân neschimbate."""
    import json as _json
    try:
        meta = _json.loads(c.old_value or "{}")
        goods = _json.loads(c.new_value or "{}")
    except ValueError:
        return ""
    obs, deleted = meta.get("obs", {}), set(meta.get("deleted", []))
    # O fișă BOPI ulterioară limitării (ex. marca înregistrată) are deja lista finală de produse:
    # limitarea recitită nu o mai suprascrie (doar se consemnează în istoric).
    later = [e for e in (m.events or []) if (e.get("date") or "") > (c.bulletin_date or "")
             and e.get("chapter") == CHAPTER_LABEL["registered"]]
    if later and m.goods:
        return "limitare reflectată în fișa din " + later[-1].get("bopi", "")
    cur = dict(m.goods or {})
    for k in deleted:
        cur.pop(k, None)
    for k, txt in goods.items():
        if k not in deleted and txt and obs.get(k, "modificată").startswith(("modific", "neschimb")):
            if obs.get(k, "").startswith("neschimb") and cur.get(k):
                continue
            cur[k] = txt
    classes = {int(k) for k in cur} | {int(k) for k, st in obs.items() if k not in deleted}
    classes -= {int(k) for k in deleted}
    m.goods = cur
    m.classes = sorted(classes)
    parts = []
    mod = [k for k, st in obs.items() if st.startswith("modific")]
    if mod:
        parts.append("clase restrânse: " + ", ".join(sorted(mod, key=int)))
    if deleted:
        parts.append("clase șterse: " + ", ".join(sorted(deleted, key=int)))
    parts.append("clase după limitare: " + ", ".join(str(x) for x in m.classes))
    return "; ".join(parts)


def _add_event(m: Mark, ev: Dict) -> None:
    """Adaugă evenimentul; dacă există deja (buletin recitit), îl completează cu informațiile noi."""
    key = (ev.get("date"), ev.get("bopi"), ev.get("chapter"))
    events = [dict(e) for e in (m.events or [])]
    for e in events:
        if (e.get("date"), e.get("bopi"), e.get("chapter")) == key:
            changed = False
            for k, v in ev.items():
                # „representative” gol înseamnă „fără reprezentant” — se păstrează și el.
                if (v not in (None, "", []) or (k == "representative" and k not in e)) and e.get(k) != v:
                    e[k] = v
                    changed = True
            if changed:
                m.events = events          # listă nouă → SQLAlchemy observă modificarea JSON
            return
    m.events = sorted(events + [ev], key=lambda e: e["date"])


def _recompute_holder(m: Mark) -> None:
    """Titularul curent = cel din documentul cu data cea mai recentă (buletin, cesiune, fuziune,
    schimbare de nume, reînnoire), indiferent de ordinea în care au fost citite buletinele.
    La aceeași dată: cesiunea / schimbarea de nume > buletinul mărcii > lista de reînnoiri."""
    ADDR = "Modificare adresă titular"
    evs = [e for e in (m.events or []) if e.get("holder")]
    # Schimbarea de adresă nu schimbă titularul (poate privi un co-titular) — dă doar adresa nouă.
    cands = [e for e in evs if e.get("chapter") != ADDR] or evs
    if not cands:
        return
    RANK = {"Cesiune": 4, "Fuziune": 4, "Modificare titular": 3}

    def prio(e):
        # La aceeași dată: cesiunea / fuziunea > schimbarea de nume > buletinul mărcii > reînnoirea.
        return RANK.get(e.get("chapter"), e.get("prio", 1))
    top = max(e["date"] for e in cands)
    same_day = [e for e in cands if e["date"] == top]
    if len(same_day) > 1:
        # Mai multe modificări în aceeași zi: urmăm lanțul vechi → nou — titularul final e cel care nu
        # apare ca titular „vechi” într-o altă modificare din ziua respectivă.
        befores = [norm(e.get("holder_before") or "") for e in same_day]
        final = [e for e in same_day if not any(norm(e["holder"]) and norm(e["holder"]) in b
                                                for b, o in zip(befores, same_day) if o is not e)]
        if final:
            same_day = final
    best = max(same_day, key=prio)
    addr = [e for e in evs if e.get("chapter") == ADDR and e["date"] >= best["date"]
            and norm(e["holder"]) == norm(best["holder"]) and e.get("holder_address")]
    if addr and best.get("chapter") != ADDR:
        best = dict(best, holder_address=max(addr, key=lambda e: e["date"])["holder_address"])
    if norm(best["holder"]) != norm(m.holder) or (best.get("holder_address") and best["holder_address"] != m.holder_address):
        m.holder = best["holder"]
        # Lista de reînnoiri are doar numele: păstrăm adresa cunoscută dacă e același titular.
        if best.get("holder_address"):
            m.holder_address = best["holder_address"]
        elif not m.holder_address.upper().startswith(best["holder"].upper()[:12]):
            m.holder_address = best["holder"]
        m.holder_norm = norm(m.holder)


def _apply_pending(db, m: Mark) -> None:
    """Modificările (transferuri, reînnoiri, radieri) publicate înainte ca marca să ajungă în bază —
    ex. o reînnoire din 2025 a unei mărci încărcate acum din arhiva 2015."""
    q = db.query(MarkChange).filter(MarkChange.applied == False)  # noqa: E712
    conds = [MarkChange.app_number == m.app_number]
    if m.reg_number:
        conds.append(MarkChange.reg_number == m.reg_number)
    from sqlalchemy import or_
    for c in q.filter(or_(*conds)).order_by(MarkChange.bulletin_date):
        _apply_change(db, m, c)


CHANGE_LABEL = {
    "renewal": "Reînnoire", "representation": "Modificarea reprezentării mărcii", "license": "Licență înscrisă",
    "limitation": "Limitare la cererea titularului", "radiated": "Radiată la cererea titularului",
    "withdrawn": "Cerere retrasă",
}


def _apply_change(db, m: Mark, c: MarkChange) -> None:
    label = CHANGE_LABEL.get(c.kind, c.kind)
    detail = ""
    holder_ev = {}
    if c.kind in ("Cesiune", "Fuziune", "Modificare titular", "Modificare adresă titular") and c.new_value:
        new_holder = party_name(c.new_value)
        holder_ev = {"holder": new_holder, "holder_address": c.new_value, "prio": 2,
                     "holder_before": (c.old_value or "").strip()}
        detail = f"titular nou: {new_holder}" + (f" (anterior: {party_name(c.old_value)})" if c.old_value else "")
    elif c.kind == "Cesiune parțială" and c.new_value:
        detail = f"cesiune parțială către {party_name(c.new_value)}"
    elif c.kind == "representation" and c.new_value:
        detail = f"„{c.old_value}” → „{c.new_value}”"
        holder_ev["name"] = c.new_value
        holder_ev["name_before"] = c.old_value or ""
        if norm(c.new_value) != norm(m.name):
            m.name, m.name_norm = c.new_value, norm(c.new_value)
    elif c.kind == "renewal":
        dl = _DATE_LIKE.match(c.representative or "")
        if dl:
            # Coloane decalate în lista de reînnoiri (REFLEKTOR TV): în locul mandatarului e data de la
            # care curge reînnoirea = vechea expirare → noua expirare e cu 10 ani mai târziu.
            iso = _renewal_from(c.representative, m.expiry_date)
            if iso and not c.expiry:
                c.expiry = iso
            c.representative = ""
        detail = f"valabilă până la {c.expiry[8:10]}.{c.expiry[5:7]}.{c.expiry[:4]}" if c.expiry else ""
        if c.new_value:      # titularul la data reînnoirii (doar numele)
            holder_ev = {"holder": c.new_value.strip(), "holder_address": "", "prio": 0}
        rep = (c.representative or "").strip()
        if rep:
            # Mandatarul de la reînnoire — adesea înscris după depunere (cererea fusese fără reprezentant).
            holder_ev["representative"] = rep
            last_doc = max((e.get("date") or "" for e in (m.events or []) if e.get("representative")), default="")
            if not m.representative or (norm(rep) != norm(m.representative) and c.bulletin_date >= last_doc):
                m.representative, m.rep_norm = rep, norm(rep)
                m.representative_address = rep
    elif c.kind == "license":
        detail = c.new_value[:200]
    elif c.kind == "limitation":
        before = list(m.classes or [])
        detail = _apply_limitation(m, c)
        holder_ev["classes_before"], holder_ev["classes"] = before, list(m.classes or [])
    elif c.kind in ("radiated", "withdrawn") and c.bulletin_date >= (m.status_date or ""):
        m.status, m.status_date = ("Radiată" if c.kind == "radiated" else "Retrasă"), c.bulletin_date
    ev = {"date": c.bulletin_date, "bopi": c.label, "chapter": label}
    if detail:
        ev["detail"] = detail
    if c.kind == "renewal" and c.expiry:
        ev["expiry"] = c.expiry
    if c.kind == "renewal" and getattr(c, "renewal_no", 0):
        ev["renewal_no"] = c.renewal_no
    ev.update(holder_ev)
    _add_event(m, ev)
    _recompute_holder(m)
    _current_rep(m)
    m.expiry_date = expiry_of(m)
    m.updated_at = datetime.utcnow()
    c.applied = True


def _renewal_stub(db, c: MarkChange) -> Optional[Mark]:
    """O marcă reînnoită care nu e (încă) în bază — înregistrată înainte de arhiva disponibilă.
    O păstrăm după nr. de marcă, cu titularul, clasele și noua expirare; denumirea lipsește
    din listele de reînnoiri. Când apare marca originală, se unesc (vezi _merge_stub)."""
    if not c.reg_number or not c.expiry or c.expiry < date.today().isoformat():
        return None
    key = f"R{c.reg_number}"
    m = db.get(Mark, key)
    if m is None:
        classes = [int(x) for x in re.findall(r"\d{1,2}", c.old_value or "") if 1 <= int(x) <= 45]
        m = Mark(app_number=key, reg_number=c.reg_number, status="Înregistrată", status_date=c.bulletin_date,
                 holder=c.new_value, holder_norm=norm(c.new_value), holder_address=c.new_value,
                 classes=classes, goods={}, events=[], mark_type="")
        db.add(m)
        db.flush()
    if not m.name:
        # Denumirea dintr-un tabel de modificări (transfer, adresă, licență…) citit anterior.
        prev = db.query(MarkChange).filter(MarkChange.reg_number == c.reg_number, MarkChange.name != "")\
            .order_by(MarkChange.bulletin_date.desc()).first()
        if prev is not None:
            _name_stub(m, prev.name, prev.app_number, prev.label, prev.bulletin_date)
    return m


def _name_stub(m: Mark, name: str, app: str, label: str, bdate: str) -> None:
    m.name, m.name_norm = name, norm(name)
    m.mark_type = "Combinată" if m.has_image else "Verbală"
    detail = f"nr. depozit {display_app(app)}" if app else ""
    ev = {"date": bdate, "bopi": label, "chapter": "Denumire preluată din tabelele BOPI"}
    if detail:
        ev["detail"] = detail
        ev["app"] = app                      # nr. de depozit original (M…)
    _add_event(m, ev)
    _stub_filing_date(m)


def _stub_filing_date(m: Mark) -> None:
    """Data de depozit a unei mărci reînnoite vechi, din nr. de depozit (anul) și expirare (ziua, luna):
    protecția curge 10 ani de la depozit (Legea 84/1998), deci expirarea = depozit + 10·n ani.
    Doar dacă diferența de ani e un multiplu pozitiv de 10; altfel rămâne necompletată."""
    if m.filing_date or not m.app_number.startswith("R") or not m.expiry_date:
        return
    app = next((e.get("app") or e.get("detail", "") for e in reversed(m.events or [])
                if e.get("app") or e.get("chapter") == "Denumire preluată din tabelele BOPI"), "")
    mm = re.search(r"M\s*(\d{4})\s*\d{4,6}", app or "")
    if not mm:
        return
    y, ey = int(mm.group(1)), int(m.expiry_date[:4])
    if ey > y and (ey - y) % 10 == 0:
        try:
            m.filing_date = date(y, int(m.expiry_date[5:7]), int(m.expiry_date[8:10])).isoformat()
        except ValueError:
            m.filing_date = date(y, int(m.expiry_date[5:7]), 28).isoformat()


_DATE_LIKE = re.compile(r"^\s*\d{1,2}[./]\d{1,2}[./]\d{2,4}\s*$")


def _renewal_from(d: str, expiry: str) -> str:
    """„17/01/2022” cu expirarea curentă 2022-01-17 → 2032-01-17 (altfel „”)."""
    p = re.findall(r"\d+", d or "")
    if len(p) != 3 or len(p[2]) != 4:
        return ""
    iso = f"{p[2]}-{int(p[1]):02d}-{int(p[0]):02d}"
    return f"{int(p[2]) + 10}{iso[4:]}" if iso == (expiry or "")[:10] else ""


def _current_rep(m: Mark) -> None:
    """Reprezentantul actual = cel din cel mai recent document BOPI care numește unul (cerere,
    înregistrare, reînnoire). Ex. Beauty Mania: fără reprezentant la înregistrare (2012), reînnoită
    în 2021 prin Cabinetul Cătălin Neagu."""
    evs = [e for e in (m.events or []) if (e.get("representative") or "").strip()
           and not _DATE_LIKE.match(e["representative"]) and (e.get("bopi") or "").startswith(("BOPI", "Cereri"))]
    if not evs:
        return
    last = max(evs, key=lambda e: e.get("date") or "")
    cur = norm(m.representative)
    cur_date = max((e.get("date") or "" for e in evs if norm(e["representative"]) == cur), default="")
    if not cur or ((last.get("date") or "") > cur_date and norm(last["representative"]) != cur):
        m.representative = last["representative"].strip()
        m.rep_norm = norm(m.representative)
        m.representative_address = m.representative


def _merge_stub(db, m: Mark) -> None:
    """Marca originală a apărut (arhivă) — preia reînnoirile păstrate pe nr. de marcă."""
    if not m.reg_number:
        return
    stub = db.get(Mark, f"R{m.reg_number.lstrip('0')}")
    if stub is None or stub is m:
        return
    for ev in stub.events or []:
        if ev not in (m.events or []):
            m.events = sorted((m.events or []) + [ev], key=lambda e: e["date"])
    if not m.classes and stub.classes:
        m.classes = stub.classes
    # Denumirea / logo-ul aduse din TMview pe marca reînnoită rămân, dacă originalul nu le are.
    if not m.name and stub.name:
        m.name, m.name_norm = stub.name, norm(stub.name)
    img = db.get(MarkImage, stub.app_number)
    if img is not None:
        if not m.has_image:
            db.merge(MarkImage(app_number=m.app_number, data=img.data, content_type=img.content_type))
            m.has_image = True
        db.delete(img)
    m.mark_type = ("Combinată" if m.has_image and m.name else
                   "Figurativă" if m.has_image else "Verbală")
    _current_rep(m)                      # reprezentantul de la reînnoire (pe marca reînnoită)
    db.delete(stub)


def _process_changes(db, path: str, src: Dict) -> int:
    n = 0
    bdate = src["date"].isoformat()
    # Reprocesare fără dubluri.
    db.query(MarkChange).filter(MarkChange.label == src["label"], MarkChange.bulletin_date == bdate)\
        .delete(synchronize_session=False)
    for r in map(fix_record, parse_changes(path)):
        if r["kind"] == "limitation":
            import json as _json
            r["old"] = _json.dumps({"obs": r.get("obs", {}), "deleted": r.get("deleted", [])}, ensure_ascii=False)
            r["new"] = _json.dumps(r.get("goods", {}), ensure_ascii=False)
        c = MarkChange(kind=r["kind"], app_number=r.get("app_number", ""), reg_number=r.get("reg_number", ""),
                       old_value=r.get("old", ""), new_value=r.get("new", ""), expiry=r.get("expiry", ""),
                       title=r.get("title", ""), bulletin_date=bdate, label=src["label"],
                       name=(r.get("name") or "").strip(), representative=(r.get("rep") or "").strip(),
                       renewal_no=r.get("renewal_no") or 0)
        db.add(c)
        m = db.get(Mark, c.app_number) if c.app_number else None
        if m is None and c.reg_number:
            m = db.query(Mark).filter(Mark.reg_number == c.reg_number).first()
        if m is None and c.kind == "renewal":
            m = _renewal_stub(db, c)
        if m is not None:
            _apply_change(db, m, c)
            if m.app_number.startswith("R"):
                # Modificările publicate înainte de reînnoire (inclusiv în același buletin) — în așteptare.
                db.flush()
                _apply_pending(db, m)
            _stub_filing_date(m)
        if c.name and c.reg_number:
            # Marca reînnoită veche (R<nr.>), fără denumire: o preia din tabel.
            stub = db.get(Mark, f"R{c.reg_number}")
            if stub is not None and not stub.name:
                _name_stub(stub, c.name, c.app_number, c.label, c.bulletin_date)
        n += 1
        if n % 200 == 0:
            db.commit()
    return n


def expiry_of(m: Mark) -> str:
    """Protecția durează 10 ani de la data depozitului (Legea 84/1998, art. 39); o reînnoire
    publicată în BOPI o prelungește."""
    renewals = [e["expiry"] for e in (m.events or []) if e.get("expiry")]
    if renewals:
        return max(renewals)
    if m.status != "Înregistrată" or not m.filing_date:
        return ""
    exp = _add_years(m.filing_date, 10)
    # Data depozitului mai veche cu peste 10 ani decât înregistrarea: marcă UE transformată în marcă
    # națională (OSIM tipărește depozitul mărcii UE) — termenul curge în continuare din 10 în 10 ani.
    while m.reg_date and exp <= m.reg_date:
        exp = _add_years(exp, 10)
    return exp


# ── Procesarea unui PDF ─────────────────────────────────────────────────────────

def _download(url: str) -> Optional[str]:
    url = url.split("#", 1)[0]
    r = requests.get(url, headers=UA, timeout=600, stream=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(fd, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    with open(path, "rb") as f:
        if f.read(5) != b"%PDF-":
            os.remove(path)
            return None
    return path


def process_source(src: Dict) -> int:
    path = _download(src["url"])
    db = SessionLocal()
    try:
        if not path:
            # Zilele fără buletin (sărbători) rămân „missing”; cele recente se reîncearcă.
            if src["date"] < date.today() - timedelta(days=7):
                db.merge(SourceFile(url=src["url"], kind=src["kind"], label=src["label"],
                                    bulletin_date=src["date"].isoformat(), status="missing",
                                    processed_at=datetime.utcnow()))
                db.commit()
            return 0
        n = 0
        if src["kind"] == "changes":
            n = _process_changes(db, path, src)
        else:
            have = {a for (a,) in db.query(Mark.app_number).filter(Mark.has_image == True)}  # noqa: E712
            for chapter, rec in parse_pdf(path, src["kind"], have_image=have):
                _upsert(db, chapter, rec, src)
                n += 1
                if n % 200 == 0:
                    db.commit()
        # Un buletin de mărci din care nu s-a citit nimic (fonturi fără tabel de caractere — „(cid:2)…”,
        # ca BOPI 10/2017–04/2019) nu e „citit”: rămâne vizibil în /api/coverage, iar mărcile vin din TMview.
        status = "unreadable" if n == 0 and src["kind"] in ("monthly", "daily", "admise") else "ok"
        if status == "unreadable":
            print(f"[INGEST] {src['label']}: 0 mărci citite — buletin necitibil")
        db.merge(SourceFile(url=src["url"], kind=src["kind"], label=src["label"],
                            bulletin_date=src["date"].isoformat(), status=status, marks=n, error="",
                            processed_at=datetime.utcnow()))
        db.commit()
        return n
    finally:
        db.close()
        if path and os.path.exists(path):
            os.remove(path)


def run_ingest(limit: Optional[int] = None) -> None:
    """Procesează toate sursele încă neprocesate (în ordine cronologică)."""
    if not _lock.acquire(blocking=False):
        return
    try:
        job.update(running=True, error="", done=0, marks=0, started_at=datetime.utcnow().isoformat() + "Z",
                   finished_at=None)
        db = SessionLocal()
        try:
            done = {u for (u,) in db.query(SourceFile.url).filter(SourceFile.status.in_(("ok", "missing", "unreadable")))}
        finally:
            db.close()
        todo = [s for s in list_sources() if s["url"] not in done]
        if limit:
            todo = todo[:limit]
        job["total"] = len(todo)
        for s in todo:
            job["current"] = s["label"]
            try:
                job["marks"] += process_source(s)
            except Exception as e:
                traceback.print_exc()
                db = SessionLocal()
                try:
                    db.merge(SourceFile(url=s["url"], kind=s["kind"], label=s["label"],
                                        bulletin_date=s["date"].isoformat(), status="error",
                                        error=str(e)[:500], processed_at=datetime.utcnow()))
                    db.commit()
                finally:
                    db.close()
            job["done"] += 1
    except Exception as e:
        traceback.print_exc()
        job["error"] = str(e)
    finally:
        job.update(running=False, current="", finished_at=datetime.utcnow().isoformat() + "Z")
        _lock.release()
    try:
        enrich_renewed()
        enrich_logos()
    except Exception:
        traceback.print_exc()


def start_background(limit: Optional[int] = None) -> bool:
    if job["running"]:
        return False
    threading.Thread(target=run_ingest, kwargs={"limit": limit}, daemon=True).start()
    return True


# ── Denumirile mărcilor reînnoite înregistrate înainte de arhivă (TMview) ─────────

def _fetch_logo(db, m: Mark, st13: str) -> Optional[bool]:
    """Logo-ul din TMview, prin aplicația de monitorizare (TMview blochează cererile directe).
    True = salvat; False = TMview spune clar că marca n-are logo; None = nu s-a putut afla (blocaj)."""
    try:
        r = requests.get(f"{MONITOR_API}/api/tm-image", params={"st13": st13}, timeout=90)
    except Exception:
        return None
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        return None
    # 1458 octeți = imaginea-substitut TMview pentru mărcile fără logo.
    if len(r.content) <= 1600:
        return False
    try:
        import io
        from PIL import Image
        pil = Image.open(io.BytesIO(r.content)).convert("RGB")
        pil.thumbnail((320, 320))
        buf = io.BytesIO()
        pil.save(buf, "WEBP", quality=80)
        data, ct = buf.getvalue(), "image/webp"
    except Exception:
        data, ct = r.content, r.headers["content-type"]
    db.merge(MarkImage(app_number=m.app_number, data=data, content_type=ct))
    m.has_image = True
    m.mark_type = "Combinată" if m.name else "Figurativă"
    return True


# ── Logo-uri originale (TMview) ───────────────────────────────────────────────────
# Siglele decupate din PDF-urile BOPI au peste ele modelul de fundal al paginii. Originalul îl luăm din
# TMview, într-un tabel separat: (1) la cerere, când cineva deschide marca; (2) treptat, pentru toate,
# doar după ce încărcarea buletinelor s-a terminat — ca să nu încetinim citirea și să nu încărcăm TMview.
hq_job: Dict = {"running": False, "done": 0, "found": 0, "last": None}
_hq_pending: set = set()
_hq_lock = threading.Lock()


def _st13_of(m: Mark) -> str:
    if re.fullmatch(r"M\d{8,10}", m.app_number or ""):
        return "RO50000" + m.app_number
    if (m.app_number or "").isdigit():                 # marcă veche, depozit dinainte de 2001
        return "RO50000" + m.app_number.zfill(10)
    return _stub_st13(m)


def fetch_logo_hq(app: str) -> str:
    """Logo-ul original al unei mărci în mark_images_hq; întoarce starea (ok / none / fail)."""
    from db import MarkImageHQ
    db = SessionLocal()
    try:
        m = db.get(Mark, app)
        st13 = _st13_of(m) if m else ""
        if not st13:
            st, data, ct = "none", None, ""
        else:
            try:
                r = requests.get(f"{MONITOR_API}/api/tm-image", params={"st13": st13}, timeout=60)
            except Exception:
                r = None
            if r is None or r.status_code >= 500:
                st, data, ct = "fail", None, ""
            elif r.status_code != 200 or not r.headers.get("content-type", "").startswith("image") or len(r.content) <= 1600:
                st, data, ct = "none", None, ""             # 1458 octeți = substitutul TMview „fără logo”
            else:
                st, data, ct = "ok", r.content, r.headers["content-type"]
                try:
                    import io
                    from PIL import Image
                    pil = Image.open(io.BytesIO(r.content))
                    pil = pil.convert("RGBA" if pil.mode in ("RGBA", "LA", "P") else "RGB")
                    pil.thumbnail((800, 800))
                    buf = io.BytesIO()
                    pil.save(buf, "WEBP", quality=88)
                    data, ct = buf.getvalue(), "image/webp"
                except Exception:
                    pass
        db.merge(MarkImageHQ(app_number=app, data=data, content_type=ct or "image/webp", status=st,
                             fetched_at=datetime.utcnow()))
        db.commit()
        return st
    finally:
        db.close()


def request_logo_hq(app: str) -> None:
    """La deschiderea unei mărci: logo-ul original se aduce în fundal (o singură cerere odată)."""
    if _tmview_paused():
        return
    with _hq_lock:
        if app in _hq_pending or len(_hq_pending) > 20:
            return
        _hq_pending.add(app)

    def run():
        try:
            fetch_logo_hq(app)
        except Exception as e:
            print(f"[LOGO-HQ] {app}: {type(e).__name__}: {e}")
        finally:
            with _hq_lock:
                _hq_pending.discard(app)
    threading.Thread(target=run, daemon=True).start()


def enrich_logos_hq(limit: int = 150) -> None:
    """Treptat, toate siglele — doar după terminarea încărcării buletinelor."""
    import time as _t
    from db import MarkImageHQ
    if hq_job["running"] or job["running"] or _tmview_paused():
        return
    # Prioritatea stabilită cu utilizatorul: întâi datele mărcilor (produse, date, stări, depozite),
    # apoi siglele — cât timp mai sunt fișe TMview de completat, siglele așteaptă (cu excepția celor
    # cerute la deschiderea unei mărci).
    db = SessionLocal()
    try:
        from sqlalchemy import or_
        pending = db.query(Mark.app_number).filter(
            or_(Mark.app_number.like("R%"), Mark.tmview_checked.like("%|gsneed%"), Mark.tmview_checked.like("%|needfix%")),
            or_(Mark.tmview_checked.is_(None), ~Mark.tmview_checked.like("%|gs3%")),
            or_(Mark.tmview_checked.is_(None), ~Mark.tmview_checked.like("%|gsf|gsf|gsf%"))).first()
    finally:
        db.close()
    if pending is not None:
        return
    hq_job.update(running=True, done=0, found=0)
    try:
        retry = datetime.utcnow() - timedelta(days=30)
        db = SessionLocal()
        try:
            done = db.query(MarkImageHQ.app_number).filter(
                (MarkImageHQ.status != "fail") | (MarkImageHQ.fetched_at > retry))
            todo = [a for (a,) in db.query(Mark.app_number).filter(
                Mark.has_image == True, ~Mark.app_number.in_(done)).limit(limit)]  # noqa: E712
        finally:
            db.close()
        fails = 0
        for app in todo:
            st = fetch_logo_hq(app)
            hq_job["done"] += 1
            hq_job["found"] += st == "ok"
            fails = fails + 1 if st == "fail" else 0
            if fails >= 10:
                _tmview_backoff()
                break
            _t.sleep(2)
    finally:
        hq_job.update(running=False, last=datetime.utcnow().isoformat() + "Z")


def enrich_logos(limit: int = 300) -> int:
    """Logo-urile mărcilor reînnoite vechi („R…”): BOPI nu le are, iar fără logo tipul era dedus
    greșit „Verbală” (ELECTRA 3R29570). „|img” se pune doar după un răspuns clar de la TMview."""
    import time as _t
    if _tmview_paused():
        return 0
    db = SessionLocal()
    n = unknown = 0
    try:
        q = db.query(Mark).filter(Mark.app_number.like("R%"), Mark.has_image == False,  # noqa: E712
                                  Mark.tmview_checked.isnot(None), Mark.tmview_checked != "",
                                  ~Mark.tmview_checked.like("%|img%")).limit(limit)
        for m in q.all():
            ev = next((e for e in (m.events or []) if e.get("bopi") == "TMview"), None)
            st13 = (ev or {}).get("st13") or _stub_st13(m)
            if not st13 and ev:
                dep = re.sub(r"\D", "", ev.get("detail", ""))
                st13 = f"RO50000000{dep.zfill(7)}" if dep else ""
            res = _fetch_logo(db, m, st13) if st13 else False
            if res is None:
                unknown += 1
                if unknown >= 10:
                    _tmview_backoff()
                    break
                continue
            unknown = 0
            n += bool(res)
            m.tmview_checked = (m.tmview_checked or "") + "|img"
            db.commit()
            _t.sleep(1)
    finally:
        db.close()
    return n


def _stub_st13(m: Mark) -> str:
    """ST13-ul TMview al unei mărci reînnoite vechi: din căutarea TMview sau din nr. de depozit
    preluat din tabelele BOPI (RO50000 + M201307749)."""
    if re.fullmatch(r"M\d{8,10}", m.app_number or ""):
        return "RO50000" + m.app_number
    if (m.app_number or "").isdigit():                 # marcă veche, depozit dinainte de 2001
        return "RO50000" + m.app_number.zfill(10)
    for e in reversed(m.events or []):
        if e.get("st13"):
            return e["st13"]
        app = re.sub(r"\s+", "", e.get("app") or "")
        if re.fullmatch(r"M\d{8,10}", app):
            return "RO50000" + app
    return ""


goods_job: Dict = {"running": False, "done": 0, "found": 0, "last": None}
# Când TMview blochează serverul aplicației de monitorizare (Imperva), ProWatch face o pauză ca să nu
# afecteze căutările ProSearch / ProMonitor (pauza se pune la primele refuzuri, vezi _tmview_backoff).
tmview_pause = {"until": datetime.utcnow()}


def _tmview_paused() -> bool:
    # Fereastra monitorizării zilnice ProMonitor (zile lucrătoare, 15:00–15:30 ora României): TMview
    # rămâne liber pentru ea — completările ProWatch stau pe loc.
    from zoneinfo import ZoneInfo
    ro = datetime.now(ZoneInfo("Europe/Bucharest"))
    if ro.weekday() < 5 and (15, 0) <= (ro.hour, ro.minute) < (15, 30):
        return True
    return datetime.utcnow() < tmview_pause["until"]


def _weekend() -> bool:
    """Sâmbăta și duminica nu rulează monitorizarea ProMonitor — ProWatch poate lua mai mult din TMview."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Bucharest")).weekday() >= 5


def _tmview_backoff(hours: float = 0.25) -> None:
    tmview_pause["until"] = datetime.utcnow() + timedelta(hours=hours)
    print(f"[TMVIEW] pauză până la {tmview_pause['until']:%H:%M} UTC")
TM_FEATURE = {"Word": "Verbală", "Figurative": "Figurativă", "Combined": "Combinată", "Shape": "Tridimensională",
              "3-D": "Tridimensională", "Sound": "Sonoră", "Colour": "Culoare", "Position": "Poziție",
              "Pattern": "Model", "Motion": "Mișcare", "Multimedia": "Multimedia", "Hologram": "Hologramă",
              "Other": "Alt tip"}


# Starea curentă din TMview a cererilor în curs — BOPI nu publică opozițiile și contestațiile.
status_job: Dict = {"running": False, "done": 0, "found": 0, "last": None, "codes": {}}


def tm_status_ro(code: str) -> str:
    """Starea TMview tradusă — doar stările pe care BOPI nu le dă (opoziție, contestație…)."""
    c = (code or "").lower()
    if "oppos" in c:
        return "În opoziție"
    if "appeal" in c:
        return "În contestație"
    if "withdraw" in c:
        return "Retrasă"
    if "refus" in c or "reject" in c:
        return "Respinsă"
    if "examin" in c:
        return "În examinare"
    return ""


# Mărci înregistrate pe care TMview le dă încetate (anulare, decădere, renunțare…) fără ca BOPI să fi
# publicat radierea — de ex. M 2008 10256: „Registration cancelled” la 28.12.2022.
TM_ENDED = (("cancel", "Anulată"), ("invalid", "Anulată"), ("revo", "Decăzută"), ("surrender", "Renunțare"),
            ("expired", "Expirată"), ("ended", "Încetată"))


def tm_ended_ro(code: str) -> str:
    c = (code or "").lower()
    return next((ro for k, ro in TM_ENDED if k in c), "")


def _set_tm_status(m: Mark, d: Dict) -> None:
    m.tm_status = d.get("markCurrentStatusCode") or ""
    m.tm_status_date = (d.get("markCurrentStatusDate") or "")[:10]
    m.tm_status_checked = date.today().isoformat()


def refresh_status(app: str) -> None:
    """La deschiderea unei mărci: fișa TMview (stare, produse, date) se reaplică în fundal."""
    if _tmview_paused():
        return
    with _hq_lock:
        key = "st:" + app
        if key in _hq_pending or len(_hq_pending) > 20:
            return
        _hq_pending.add(key)

    def run():
        try:
            st13 = "RO50000" + (app.zfill(10) if app.isdigit() else app)
            if app.startswith("R"):
                db0 = SessionLocal()
                try:
                    m0 = db0.get(Mark, app)
                    st13 = _stub_st13(m0) if m0 else ""
                finally:
                    db0.close()
                if not st13:
                    return
            r = requests.get(f"{MONITOR_API}/api/tm-detail", params={"st13": st13}, timeout=60)
            d = r.json() if r.status_code == 200 else None
            if isinstance(d, dict) and d and "error" not in d:
                db = SessionLocal()
                try:
                    m = db.get(Mark, app)
                    if m is not None:
                        tc = m.tmview_checked or ""
                        _apply_detail(m, d, app, tc, db)      # toată fișa, nu doar starea
                        _set_tm_status(m, d)
                        if "|gs3" not in tc:
                            m.tmview_checked = tc[:-4] + "|gs3|img" if tc.endswith("|img") else tc + "|gs3"
                        db.commit()
                finally:
                    db.close()
        except Exception as e:
            print(f"[STATUS] {app}: {type(e).__name__}: {e}")
        finally:
            with _hq_lock:
                _hq_pending.discard("st:" + app)
    threading.Thread(target=run, daemon=True).start()


def enrich_status(limit: int = 600) -> None:
    """Pentru cererile publicate / admise: starea din fișa TMview, reverificată săptămânal."""
    import time as _t
    if status_job["running"] or _tmview_paused():
        return
    status_job.update(running=True, done=0, found=0)
    try:
        week = (date.today() - timedelta(days=7)).isoformat()
        db = SessionLocal()
        try:
            todo = [m.app_number for m in db.query(Mark).filter(
                Mark.status.in_(["Cerere publicată", "Admisă la înregistrare"]), Mark.app_number.like("M%"),
                Mark.tm_status_checked.is_(None) | (Mark.tm_status_checked < week)).order_by(
                Mark.tm_status_checked.asc().nullsfirst(), Mark.publication_date.asc()).limit(limit)]
            if len(todo) < limit:
                # Apoi mărcile înregistrate (anulări / decăderi nepublicate în BOPI) — reverificate lunar.
                month = (date.today() - timedelta(days=30)).isoformat()
                todo += [m.app_number for m in db.query(Mark).filter(
                    Mark.status == "Înregistrată", Mark.app_number.like("M%"),
                    Mark.tm_status_checked.is_(None) | (Mark.tm_status_checked < month)).order_by(
                    Mark.tm_status_checked.asc().nullsfirst()).limit(limit - len(todo))]
        finally:
            db.close()
        fails = 0
        for app in todo:
            d = None
            try:
                r = requests.get(f"{MONITOR_API}/api/tm-detail", params={"st13": "RO50000" + app}, timeout=120)
                d = r.json() if r.status_code == 200 else None
            except Exception as e:
                print(f"[STATUS] {app}: {type(e).__name__}")
            if not isinstance(d, dict) or "error" in d or not d:
                fails += 1
                if fails >= 10:
                    print("[STATUS] TMview nu răspunde — pauză")
                    _tmview_backoff()
                    break
                _t.sleep(3)
                continue
            fails = 0
            code = d.get("markCurrentStatusCode") or ""
            status_job["codes"][code] = status_job["codes"].get(code, 0) + 1
            db = SessionLocal()
            try:
                m = db.get(Mark, app)
                if m is not None:
                    tc = m.tmview_checked or ""
                    _apply_detail(m, d, app, tc, db)      # aceeași fișă: produse complete, data exactă a înregistrării
                    _set_tm_status(m, d)
                    if "|gs3" not in tc:
                        m.tmview_checked = tc[:-4] + "|gs3|img" if tc.endswith("|img") else tc + "|gs3"
                    db.commit()
                    status_job["found"] += bool(tm_status_ro(code))
            finally:
                db.close()
            status_job["done"] += 1
            _t.sleep(1)
    finally:
        status_job.update(running=False, last=datetime.utcnow().isoformat() + "Z")


def _apply_detail(m: Mark, d: Dict, app: str, tc: str, db=None) -> bool:
    """Fișa TMview aplicată unei mărci: produse (unde lipsesc / BOPI dă doar titlul clasei), adresă,
    date, tip, Viena, reprezentant, stare. Întoarce True dacă s-au completat produse."""
    found = False
    gs = {}
    for g in d.get("goodAndServices") or []:
        c = str(g.get("niceClass") or "").lstrip("0")
        if c.isdigit() and (g.get("goodsAndServices") or "").strip():
            gs[c] = (gs.get(c, "") + " " + g["goodsAndServices"].strip()).strip()
    if gs and (app.startswith("R") or "|gsneed" in tc or not m.goods):
        keep = {str(c) for c in (m.classes or [])} if app.startswith("R") else set()
        # Doar clasele reînnoite (o reînnoire poate fi parțială); fără clase cunoscute — toate.
        m.goods = {c: t for c, t in gs.items() if not keep or c in keep} or m.goods
        if not m.classes or not app.startswith("R"):
            m.classes = sorted(int(c) for c in gs)
        found = True
    elif gs and m.goods:
        # BOPI a tipărit uneori doar titlul clasei („Publicitate; conducerea… lucrări de birou.”),
        # iar fișa OSIM / TMview are lista completă care îl continuă — o preferăm (M 2016 01750).
        g = dict(m.goods)
        for c, t in gs.items():
            old = (g.get(c) or "").strip().rstrip(".")
            if old and len(t) > len(old) + 20 and norm(t).startswith(norm(old)[:60]):
                g[c] = t
        if g != m.goods:
            m.goods = g
            found = True
    a = (d.get("applicants_detail") or [{}])[0]
    if not m.holder and (a.get("name") or "").strip():
        m.holder = a["name"].strip()             # titular necitit din BOPI (ex. M 2019 00680)
        m.holder_norm = norm(m.holder)
    if a.get("address") and (not m.holder_address or m.holder_address == m.holder):
        m.holder_address = ", ".join(p for p in [m.holder or a.get("name"), a["address"].replace("\n", ", "),
                                                 a.get("country") or ""] if p)
    if not m.filing_date and d.get("applicationDate"):
        m.filing_date = d["applicationDate"][:10]
    if not m.expiry_date and (d.get("expiryDate") or "")[:4].isdigit():
        m.expiry_date = d["expiryDate"][:10]
    elif (d.get("expiryDate") or "")[:4].isdigit() and d["expiryDate"][:10] > (m.expiry_date or "") \
            and (d.get("markCurrentStatusCode") or "").lower().startswith("regist") and m.status == "Înregistrată":
        # Reînnoire nepublicată / din buletin necitibil (BOPI 10/2017–04/2019): TMview are noua expirare.
        m.events = sorted((m.events or []) + [{"date": date.today().isoformat(), "bopi": "TMview",
                          "chapter": "Reînnoire", "detail": f"valabilă până la {d['expiryDate'][8:10]}.{d['expiryDate'][5:7]}.{d['expiryDate'][:4]} (din TMview)",
                          "expiry": d["expiryDate"][:10]}], key=lambda e: e["date"])
        m.expiry_date = d["expiryDate"][:10]
    # Restul câmpurilor fișei, doar unde lipsesc (BOPI rămâne sursa de referință).
    rd = (d.get("registrationDate") or "")[:10]
    # Mărcile din arhivă au ca dată a înregistrării data buletinului lunar care a publicat
    # înregistrarea; TMview are data exactă (decizia) — o preferăm.
    from_monthly = any(e.get("chapter") == "Mărci înregistrate" and e.get("date") == m.reg_date
                       and "/" in (e.get("bopi") or "") for e in (m.events or []))
    if rd[:4].isdigit() and (not m.reg_date or (from_monthly and not app.startswith("R"))):
        m.reg_date = rd
    if not m.publication_date and (d.get("publicationDate") or "")[:4].isdigit():
        m.publication_date = d["publicationDate"][:10]
    # Tipul: din TMview unde lipsește; la mărcile reînnoite vechi („R…”) tipul era doar dedus (fără logo →
    # „Verbală”), deci fișa TMview are prioritate (ELECTRA 3R29570: „Combined”, logo alb-negru).
    if TM_FEATURE.get(d.get("markFeature") or "") and (not m.mark_type or app.startswith("R")):
        m.mark_type = TM_FEATURE[d["markFeature"]]
    if db is not None and d.get("markImageURI") and not m.has_image:
        st13 = re.search(r"/(RO[0-9A-Z]+)$", d["markImageURI"])
        if st13:
            _fetch_logo(db, m, st13.group(1))
            m.mark_type = TM_FEATURE.get(d.get("markFeature") or "") or m.mark_type
    if not m.vienna and d.get("viennaCodes"):
        m.vienna = ", ".join(str(v) for v in d["viennaCodes"])
    r0 = (d.get("representatives") or [{}])[0]
    if not m.representative and r0.get("fullName"):
        m.representative = r0["fullName"].strip()
        m.rep_norm = norm(m.representative)
        m.representative_address = ", ".join(p for p in [m.representative, (r0.get("fullAddress") or "").replace("\n", ", ")] if p)
    if d.get("markCurrentStatusCode") and not app.startswith("R"):
        _set_tm_status(m, d)                  # aceeași fișă — starea fără cerere în plus
    return found


def enrich_goods(limit: int = 1500) -> None:
    """Produsele și serviciile (511) mărcilor reînnoite vechi — listele de reînnoiri din BOPI dau
    doar clasele. Le luăm din fișa TMview (în română), prin aplicația de monitorizare."""
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import Text, cast, or_
    if goods_job["running"] or _tmview_paused():
        return
    goods_job.update(running=True, done=0, found=0)
    try:
        db = SessionLocal()
        try:
            cand = [(m.app_number, _stub_st13(m)) for m in db.query(Mark).filter(
                # „|gs2” = fișa TMview citită; „|gsf” = TMview a dat fișa goală (se reîncearcă de 3 ori).
                # + mărcile „|gsneed”: clase fără produse după o limitare recitită peste fișa înregistrată.
                # + „|needfix”: mărci din arhivă cu câmpuri lipsă (produse, adresă, dată, tip). Data publicării
                # cererii la mărcile din arhivă nu se mai caută (decizia utilizatorului, 12.11.2026).
                or_(Mark.app_number.like("R%"), Mark.tmview_checked.like("%|gsneed%"),
                    Mark.tmview_checked.like("%|needfix%")),
                ~Mark.tmview_checked.like("%|gs3%"),
                ~Mark.tmview_checked.like("%|gsf|gsf|gsf%")).order_by(
                # întâi cele fără produse deloc, apoi necitite, apoi cele cu fișa veche „|gs2” (câmpuri noi)
                (Mark.goods.is_(None) | cast(Mark.goods, Text).in_(["{}", "null"])).desc(),
                Mark.tmview_checked.like("%|gs2%").asc(),
                (Mark.name == "").asc(), Mark.tmview_checked.like("%|gsf%").asc()).limit(limit * 4)]
        finally:
            db.close()
        todo = [(a, s) for a, s in cand if s][:limit]

        def fetch(st13):
            # TMview (Imperva) dă des o fișă goală „{}” sub sarcină — reîncercăm de câteva ori, cu pauză.
            import time as _t
            d = None
            for i in range(3):
                try:
                    r = requests.get(f"{MONITOR_API}/api/tm-detail", params={"st13": st13}, timeout=120)
                    d = r.json() if r.status_code == 200 else None
                except Exception as e:
                    print(f"[GOODS] {st13}: {type(e).__name__}: {e}")
                    d = None
                if isinstance(d, dict) and d and "error" not in d:
                    return d
                _t.sleep(3 * (i + 1))
            return d if isinstance(d, dict) and "error" not in d else None

        from concurrent.futures import as_completed
        fails = 0
        db = SessionLocal()
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(fetch, st13): (app, st13) for app, st13 in todo}
                for fut in as_completed(futs):
                    app, st13 = futs[fut]
                    d = fut.result()
                    if not d:
                        # TMview blochează (Imperva) — ne oprim, ca să nu afectăm căutările din aplicația
                        # de monitorizare; continuăm la rularea următoare.
                        fails += 1
                        if fails >= 10:
                            print("[GOODS] TMview nu răspunde — pauză")
                            _tmview_backoff()
                            for f in futs:
                                f.cancel()
                            break
                        if d is None:
                            continue
                    else:
                        fails = 0
                    m = db.get(Mark, app)
                    if m is None:
                        continue
                    tc = m.tmview_checked or ""
                    if not d.get("goodAndServices") and not d.get("applicationNumber"):
                        # Fișă goală — TMview n-a răspuns de fapt; mai încercăm la rulările următoare.
                        m.tmview_checked = tc[:-4] + "|gsf|img" if tc.endswith("|img") else tc + "|gsf"
                        db.commit()
                        continue
                    if _apply_detail(m, d, app, tc, db):
                        goods_job["found"] += 1
                    tc = tc.replace("|gs2", "")
                    m.tmview_checked = tc[:-4] + "|gs3|img" if tc.endswith("|img") else tc + "|gs3"
                    goods_job["done"] += 1
                    db.commit()                                   # salvăm pe rând — progres vizibil
        finally:
            db.close()
    finally:
        goods_job.update(running=False, last=datetime.utcnow().isoformat() + "Z")


# Listele de reînnoiri din BOPI nu conțin denumirea. O luăm din TMview, după nr. de
# înregistrare, prin aplicația de monitorizare (are deja accesul la TMview).
MONITOR_API = os.environ.get("MONITOR_API", "https://web-production-c898f.up.railway.app")
enrich_job: Dict = {"running": False, "done": 0, "found": 0, "last": None}


def _regsearch(numbers: List[str], mode: str) -> Optional[Dict]:
    """Un lot de nr. de înregistrare căutat în TMview prin aplicația de monitorizare. Reîncearcă de
    2 ori (TMview / Imperva resetează uneori conexiunea); None dacă lotul n-a putut fi căutat."""
    import time
    for attempt in range(3):
        try:
            r = requests.get(f"{MONITOR_API}/api/tm-regsearch", timeout=600,
                             params={"numbers": ",".join(numbers), "mode": mode})
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "error" not in data:
                    return data
                print(f"[ENRICH] {mode}: răspuns cu eroare: {str(data)[:200]}")
            else:
                print(f"[ENRICH] {mode}: HTTP {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"[ENRICH] {mode}: {type(e).__name__}: {e}")
        time.sleep(15 * (attempt + 1))
    return None


def _apply_tmview_hit(db, m: Mark, t: Dict, today: str) -> None:
    if not m.name:                       # o denumire din tabelele BOPI rămâne
        m.name = (t.get("tmName") or "").strip()
        m.name_norm = norm(m.name)
    m.filing_date = m.filing_date or (t.get("applicationDate") or "")[:10]
    if not m.classes and t.get("niceClass"):
        m.classes = sorted(int(c) for c in t["niceClass"] if str(c).isdigit())
    if not m.holder and t.get("applicantName"):
        m.holder = t["applicantName"][0]
        m.holder_norm = norm(m.holder)
    m.mark_type = ("Combinată" if m.has_image else "Verbală") if m.name else m.mark_type
    st13 = t.get("ST13") or ""
    # Și fără markImageURI: o marcă fără denumire în TMview e figurativă — încercăm logo-ul.
    if st13 and not m.has_image and (t.get("markImageURI") or not m.name):
        _fetch_logo(db, m, st13)
    ev = {"date": today, "bopi": "TMview", "chapter": "Denumire preluată din TMview",
          "detail": f"nr. depozit {t.get('applicationNumber') or ''}".strip(), "st13": st13}
    # Control: titularul din TMview ar trebui să semene cu cel din lista de reînnoiri.
    tv = norm((t.get("applicantName") or [""])[0])
    if m.holder and tv:
        a = {w for w in norm(m.holder).split() if len(w) > 2 and w not in ("srl", "sas", "s r l", "ltd", "inc")}
        b = {w for w in tv.split() if len(w) > 2}
        if a and b and not (a & b):
            ev["detail"] += f" · titular în TMview: {t['applicantName'][0]} (de verificat)"
            print(f"[ENRICH] titular diferit R{m.reg_number}: {m.holder!r} vs TMview {t['applicantName'][0]!r}")
    m.events = sorted((m.events or []) + [ev], key=lambda e: e["date"])


def enrich_renewed(limit: int = 600) -> None:
    if enrich_job["running"] or _tmview_paused():
        return
    enrich_job.update(running=True, done=0, found=0, failed_batches=0)
    try:
        today = date.today().isoformat()
        db = SessionLocal()
        try:
            # „|v2” = căutată cu nr. completat la 6 cifre și cu reîncercări. Cele căutate înainte (fără
            # |v2) se mai caută o dată; cele negăsite se recaută după 30 de zile (TMview se actualizează).
            stale = (date.today() - timedelta(days=30)).isoformat()
            todo = [m.reg_number for m in db.query(Mark).filter(
                Mark.app_number.like("R%"), (Mark.name == "") | (Mark.filing_date == "") | (Mark.filing_date == None),  # noqa: E711
                # NULL = marcă reînnoită nou intrată (din arhivă / buletine noi) — necăutată încă.
                Mark.tmview_checked.is_(None) | (Mark.tmview_checked == "") | ~Mark.tmview_checked.like("%|v2%") |
                (Mark.tmview_checked < stale)).order_by(Mark.tmview_checked.asc().nullsfirst()).limit(limit)]
        finally:
            db.close()

        def _ok(res):
            # Un rezultat valid are „total”; altfel (eroare / HTTP ≠ 200 la TMview) se reîncearcă.
            return isinstance(res, dict) and "total" in res and "error" not in res

        fails = 0
        for i in range(0, len(todo), 25):
            batch = todo[i:i + 25]
            # TMview are numerele de înregistrare RO pe cel puțin 6 cifre (018279).
            data = _regsearch([n.zfill(6) for n in batch], "registrationNumber")
            if data is None:
                enrich_job["failed_batches"] += 1
                fails += 1
                if fails >= 3:
                    print("[ENRICH] 3 loturi eșuate la rând — pauză")
                    _tmview_backoff()
                    break
                continue
            fails = 0
            db = SessionLocal()
            try:
                for reg in batch:
                    m = db.get(Mark, f"R{reg}")
                    if m is None:
                        continue
                    res = data.get(reg.zfill(6)) or data.get(reg) or {}
                    if not _ok(res):
                        continue                        # reîncercăm la rularea următoare
                    hits = res.get("hits") or []
                    m.tmview_checked = today + "|pad|v2|img"
                    if hits:
                        _apply_tmview_hit(db, m, hits[0], today)
                        enrich_job["found"] += 1
                    enrich_job["done"] += 1
                db.commit()
            finally:
                db.close()
    finally:
        enrich_job.update(running=False, last=datetime.utcnow().isoformat() + "Z")


# Mărcile vechi (depozit 000001–~056xxx, până la numerotarea „M 2001 …”) încetate înainte de 11/2001
# — prima arhivă BOPI publicată online — nu apar în niciun buletin disponibil (ex. 032709 GAZETA
# MATEMATICA, înregistrată 1999, expirată 2004). Le preluăm din TMview: la cerere și treptat, în fundal.
OLD_MAX = 57000
# Seriile parcurse, în ordine: întâi anii ale căror buletine lunare nu se pot citi (fonturi fără tabel de
# caractere — textul iese „(cid:2)(cid:3)…”: BOPI 10/2017–04/2019, 01–02/2004; mărcile, în mare parte
# valabile, se iau din TMview), apoi numerotarea veche și „M 2000 / M 2001” (TMview: „M 2000 02551”).
# Numerele deja în bază se sar fără căutare.
OLD_M_MAX = 12000
OLD_SERIES = [("M", 2018), ("M", 2017), ("M", 2019), ("M", 2003), ("M", 2004),
              ("old", 0), ("M", 2000), ("M", 2001)]
OLD_M_YEARS = tuple(y for k, y in OLD_SERIES if k == "M")
_SPANS = [OLD_MAX if k == "old" else OLD_M_MAX for k, _ in OLD_SERIES]
OLD_END = sum(_SPANS)


def _old_key(i: int) -> str:
    """Poziția cursorului (1..OLD_END) → nr. de depozit: „M201705807”, „032709”…"""
    for (k, y), n in zip(OLD_SERIES, _SPANS):
        if i <= n:
            return str(i).zfill(6) if k == "old" else f"M{y}{i:05d}"
        i -= n
    return ""


def _span_end(i: int) -> int:
    """Ultima poziție a seriei în care cade i (un lot nu trece dintr-o serie în alta)."""
    acc = 0
    for n in _SPANS:
        acc += n
        if i <= acc:
            return acc
    return OLD_END


def _tm_app(key: str) -> str:
    return f"M {key[1:5]} {key[5:]}" if key.startswith("M") else key
OLD_CURSOR = "cursor:old-marks-v2"
old_job: Dict = {"running": False, "done": 0, "found": 0, "next": 0, "last": None}


def old_filing(exp: str, before: str) -> str:
    """Data depozitului dedusă din expirare (protecție 10 ani de la depozit, fiecare reînnoire +10):
    cu cât reînnoiri e nevoie ca depozitul să fie înaintea înregistrării (ISPH 008472: înreg. 1991,
    expiră 2018 → depus 1988)."""
    y = int(exp[:4]) - 10
    while before[:4].isdigit() and f"{y:04d}{exp[4:]}" > before and y > 1900:
        y -= 10
    return f"{y:04d}{exp[4:]}"


def _old_status(code: str) -> str:
    c = (code or "").lower()
    if "refus" in c or "reject" in c:
        return "Respinsă"
    if "withdraw" in c:
        return "Retrasă"
    if "filed" in c or "publish" in c or "examin" in c or "oppos" in c or "appeal" in c:
        return "Cerere publicată"
    return "Înregistrată"


def _create_old(db, num: str, t: Dict, d: Dict) -> Optional[Mark]:
    """O marcă veche din căutarea TMview (denumire, nr. înregistrare) + fișa ei (restul câmpurilor)."""
    key = num if num.startswith("M") else num.zfill(6)
    st13 = t.get("ST13") or "RO50000" + key.zfill(10)
    reg = (t.get("registrationNumber") or "").lstrip("0")
    stub = db.get(Mark, "R" + reg) if reg else None
    if stub is not None and _stub_st13(stub) in ("", st13):
        return None                        # marca reînnoită e deja în bază (R…), cu acest depozit
    if db.get(Mark, key) is not None:
        return None
    code = d.get("markCurrentStatusCode") or ""
    status = _old_status(code)
    exp = (d.get("expiryDate") or "")[:10]
    filing = (d.get("applicationDate") or "")[:10]
    if not filing and exp[:4].isdigit():
        filing = old_filing(exp, (d.get("registrationDate") or d.get("publicationDate") or "")[:10])
    holder = ((d.get("applicants_detail") or [{}])[0].get("name") or "").strip()
    name = (t.get("tmName") or "").strip()
    m = Mark(app_number=key, name=name, name_norm=norm(name), status=status,
             status_date=(d.get("registrationDate") or d.get("publicationDate") or "")[:10],
             filing_date=filing, reg_number=reg if status == "Înregistrată" else "",
             holder=holder, holder_norm=norm(holder), classes=[], goods={},
             tmview_checked=date.today().isoformat() + "|old|gs3",
             events=[{"date": (d.get("markCurrentStatusDate") or date.today().isoformat())[:10], "bopi": "TMview",
                      "chapter": ("Marcă preluată din TMview (buletinul BOPI nu poate fi citit)" if key.startswith("M")
                                  and int(key[1:5]) >= 2002 else "Marcă veche (înainte de BOPI online) preluată din TMview"),
                      "detail": f"nr. depozit {key}" + (f" · nr. marcă {reg}" if reg else ""), "st13": st13}])
    db.add(m)
    db.flush()
    _apply_detail(m, d, key, "", db)
    _set_tm_status(m, d)
    if not m.expiry_date and exp[:4].isdigit():
        m.expiry_date = exp
    if not m.mark_type:
        m.mark_type = "Combinată" if m.has_image else "Verbală"
    m.tmview_checked += "|img"
    return m


def _listify(v) -> List:
    return v if isinstance(v, list) else ([v] if v not in (None, "") else [])


def _create_skel(db, num: str, t: Dict) -> Optional[Mark]:
    """Varianta rapidă: marca intră direct din rezultatul căutării TMview (denumire, titular, clase, stare,
    depozit, expirare) — fără fișa completă; produsele, mandatarul și logo-ul se completează ulterior
    (_complete_skels)."""
    key = num if num.startswith("M") else num.zfill(6)
    st13 = t.get("ST13") or "RO50000" + key.zfill(10)
    reg = (t.get("registrationNumber") or "").lstrip("0")
    stub = db.get(Mark, "R" + reg) if reg else None
    if stub is not None and _stub_st13(stub) in ("", st13):
        return None
    if db.get(Mark, key) is not None:
        return None
    code = str(t.get("tradeMarkStatus") or "")
    status = _old_status(code) if code else ""
    exp = str(t.get("expiryDate") or "")[:10]
    filing = str(t.get("applicationDate") or "")[:10]
    if not filing and exp[:4].isdigit():
        filing = old_filing(exp, "")
    holder = "; ".join(str(h).strip() for h in _listify(t.get("applicantName")) if str(h).strip())
    classes = sorted({int(c) for c in re.findall(r"\d{1,2}", " ".join(map(str, _listify(t.get("niceClass")))))
                      if 1 <= int(c) <= 45})
    name = (t.get("tmName") or "").strip()
    m = Mark(app_number=key, name=name, name_norm=norm(name), status=status, status_date="",
             filing_date=filing, reg_number=reg if status == "Înregistrată" else "",
             holder=holder, holder_norm=norm(holder), holder_address=holder, classes=classes, goods={},
             expiry_date=exp if exp[:4].isdigit() and status == "Înregistrată" else "",
             tm_status=code, tm_status_date=date.today().isoformat(),
             mark_type="" if t.get("markImageURI") else ("Verbală" if name else ""),
             tmview_checked=date.today().isoformat() + "|old|skel",
             events=[{"date": date.today().isoformat(), "bopi": "TMview",
                      "chapter": ("Marcă preluată din TMview (buletinul BOPI nu poate fi citit)" if key.startswith("M")
                                  and int(key[1:5]) >= 2002 else "Marcă veche (înainte de BOPI online) preluată din TMview"),
                      "detail": f"nr. depozit {key}" + (f" · nr. marcă {reg}" if reg else "")
                                + " · date de bază, fișa completă urmează", "st13": st13}])
    db.add(m)
    db.flush()
    return m


def _complete_skels(limit: int = 150) -> int:
    """Mărcile intrate cu datele de bază: fișa TMview completă (produse, mandatar, logo, date exacte)."""
    import time as _t
    db = SessionLocal()
    try:
        todo = [a for (a,) in db.query(Mark.app_number).filter(Mark.tmview_checked.like("%|skel%")).limit(limit)]
    finally:
        db.close()
    n = fails = 0
    for a in todo:
        if _tmview_paused():
            break
        db = SessionLocal()
        try:
            m = db.get(Mark, a)
            st13 = next((e.get("st13") for e in (m.events or []) if e.get("st13")), "")
            d = None
            try:
                r = requests.get(f"{MONITOR_API}/api/tm-detail", params={"st13": st13}, timeout=120)
                d = r.json() if r.status_code == 200 else None
            except Exception as e:
                print(f"[OLD] fișă {a}: {type(e).__name__}")
            if not isinstance(d, dict) or not d.get("applicationNumber"):
                fails += 1
                if fails >= 3:
                    _tmview_backoff()
                    break
                continue
            fails = 0
            if not m.status:
                m.status = _old_status(d.get("markCurrentStatusCode") or "")
            if not m.reg_number and m.status == "Înregistrată":
                m.reg_number = re.sub(r"\D", "", str(d.get("registrationNumber") or "")).lstrip("0")
            m.status_date = m.status_date or (d.get("registrationDate") or d.get("publicationDate") or "")[:10]
            exp = (d.get("expiryDate") or "")[:10]
            if exp[:4].isdigit() and m.filing_date and not d.get("applicationDate"):
                m.filing_date = old_filing(exp, (d.get("registrationDate") or d.get("publicationDate") or "")[:10])
            _apply_detail(m, d, a, "", db)
            _set_tm_status(m, d)
            if not m.expiry_date and exp[:4].isdigit() and m.status == "Înregistrată":
                m.expiry_date = exp
            if not m.mark_type:
                m.mark_type = "Combinată" if m.has_image else "Verbală"
            m.events = [dict(e, detail=e["detail"].replace(" · date de bază, fișa completă urmează", ""))
                        if "fișa completă urmează" in (e.get("detail") or "") else e for e in (m.events or [])]
            m.tmview_checked = m.tmview_checked.replace("|skel", "|gs3|img")
            db.commit()
            n += 1
        except Exception as e:
            db.rollback()
            print(f"[OLD] fișă {a}: {type(e).__name__}: {e}")
        finally:
            db.close()
        _t.sleep(1)
    old_job["completed"] = old_job.get("completed", 0) + n
    return n


def _old_known(hit: Dict) -> bool:
    """Marca e deja în bază ca reînnoită veche („R…”) cu acest depozit — fără fișa TMview în plus."""
    reg = (hit.get("registrationNumber") or "").lstrip("0")
    if not reg:
        return False
    db = SessionLocal()
    try:
        stub = db.get(Mark, "R" + reg)
        return stub is not None and _stub_st13(stub) in ("", hit.get("ST13") or "")
    finally:
        db.close()


def _old_lookup(numbers: List[str]) -> Dict[str, Optional[Dict]]:
    """Nr. vechi → rezultatul TMview exact (sau None dacă nu există); cheile lipsă = lot neverificat."""
    data = _regsearch([_tm_app(n) for n in numbers], "applicationNumber")
    out: Dict[str, Optional[Dict]] = {}
    if data is None:
        return out
    for n in numbers:
        res = data.get(_tm_app(n))
        if not isinstance(res, dict) or res.get("status") != 200:
            continue
        hit = next((x for x in (res.get("hits") or []) + (res.get("first") or [])
                    if re.sub(r"\s+", "", x.get("applicationNumber") or "").zfill(6) == n), None)
        out[n] = hit
    return out


def fetch_old(num: str) -> Optional[str]:
    """La cerere (căutare / deschidere după nr.): o marcă veche lipsă adusă imediat din TMview."""
    num = re.sub(r"\s+", "", num).upper()
    if re.fullmatch(r"M\d{9}", num):
        if int(num[1:5]) not in OLD_M_YEARS:
            return None
    else:
        num = num.lstrip("0").zfill(6)
        if not num.isdigit() or int(num) > OLD_MAX:
            return None
    hit = _old_lookup([num]).get(num)
    if not hit:
        # Poate fi nr. de marcă (înregistrare) vechi, nu de depozit (ex. 008472).
        data = _regsearch([num], "registrationNumber") or {}
        res = data.get(num) if isinstance(data.get(num), dict) else {}
        hit = next((x for x in (res.get("hits") or []) + (res.get("first") or [])
                    if (x.get("registrationNumber") or "").zfill(6) == num
                    and (x.get("applicationNumber") or "").isdigit()), None)
        if not hit:
            return None
        num = hit["applicationNumber"].zfill(6)
        if int(num) > OLD_MAX:
            return None
    try:
        r = requests.get(f"{MONITOR_API}/api/tm-detail", params={"st13": hit.get("ST13")}, timeout=60)
        d = r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"[OLD] {num}: {type(e).__name__}: {e}")
        return None
    if not isinstance(d, dict) or not d.get("applicationNumber"):
        return None
    db = SessionLocal()
    try:
        m = _create_old(db, num, hit, d)
        db.commit()
        if m is None:
            stub = db.get(Mark, num)
            return stub.app_number if stub else None
        return m.app_number
    finally:
        db.close()


def _fill_names(limit: int = 100) -> None:
    """Mărci fără denumire (BOPI n-a putut fi citit / font cu codificare proprie; fișa TMview nu are denumirea):
    denumirea din căutarea TMview după nr. de depozit; fără denumire acolo → marcă figurativă, logo."""
    from sqlalchemy import or_
    from db import IS_PG
    db = SessionLocal()
    try:
        todo = [a for (a,) in db.query(Mark.app_number).filter(
            Mark.app_number.op("~")(r"^M\d{9}$") if IS_PG else Mark.app_number.like("M%"),
            Mark.name == "",
            or_(Mark.tmview_checked.is_(None), ~Mark.tmview_checked.like("%|nm%"))).order_by(
            Mark.has_image.asc(), Mark.tmview_checked.like("%|gname%").desc()).limit(limit)]
    finally:
        db.close()
    for i in range(0, len(todo), 25):
        batch = todo[i:i + 25]
        found = _old_lookup(batch)
        if not found:
            return
        db = SessionLocal()
        try:
            for a in batch:
                if a not in found:
                    continue
                m = db.get(Mark, a)
                hit = found[a] or {}
                name = (hit.get("tmName") or "").strip()
                if name:
                    m.name, m.name_norm = name, norm(name)
                    m.mark_type = m.mark_type or "Verbală"
                elif hit.get("ST13"):
                    _fetch_logo(db, m, hit["ST13"])
                m.tmview_checked = (m.tmview_checked or "") + "|nm"
                old_job["names"] = old_job.get("names", 0) + bool(name)
            db.commit()
        finally:
            db.close()


def enrich_old(batches: int = 8) -> None:
    """Treptat, toate numerele vechi de depozit (loturi de 25 în căutarea TMview, apoi fișa fiecărei
    mărci găsite). Cursorul se păstrează în baza de date — reluarea continuă de unde a rămas."""
    import time as _t
    if old_job["running"] or job["running"] or _tmview_paused():
        return
    old_job.update(running=True, done=0, found=0)
    try:
        db = SessionLocal()
        try:
            cur = db.get(SourceFile, OLD_CURSOR)
            start = int(cur.label) if cur and (cur.label or "").isdigit() else 1
        finally:
            db.close()
        old_job["next"] = start
        _fill_names()
        if start > OLD_END:
            # Toate mărcile sunt în bază cu datele de bază — acum fișele complete.
            _complete_skels(300 if _weekend() else 150)
            return
        fails = 0
        for _ in range(batches * (2 if _weekend() else 1)):
            if start > OLD_END or _tmview_paused():
                break
            stop = min(start + 25, _span_end(start) + 1)
            idx = list(range(start, stop))
            nums = [_old_key(i) for i in idx]
            db = SessionLocal()
            try:
                have = {a for (a,) in db.query(Mark.app_number).filter(Mark.app_number.in_(nums))}
            finally:
                db.close()
            look = [n for n in nums if n not in have]
            found = _old_lookup(look) if look else {}
            if look and not found:
                fails += 1
                if fails >= 3:
                    print("[OLD] TMview nu răspunde — pauză")
                    _tmview_backoff()
                    break
                continue
            fails = 0
            ok = True
            for n in look:
                if n not in found:
                    ok = False                    # rezultat lipsă — lotul se reia
                    continue
                hit = found[n]
                if not hit or _old_known(hit):
                    continue
                db = SessionLocal()
                try:
                    if _create_skel(db, n, hit) is not None:
                        old_job["found"] += 1
                    db.commit()
                except Exception as e:
                    db.rollback()
                    print(f"[OLD] {n}: {type(e).__name__}: {e}")
                    ok = False
                finally:
                    db.close()
                old_job["done"] += 1
            if not ok:
                break                             # cursorul rămâne pe lot; se reia la rularea următoare
            start = idx[-1] + 1
            db = SessionLocal()
            try:
                db.merge(SourceFile(url=OLD_CURSOR, kind="cursor", label=str(start), status="cursor"))
                db.commit()
            finally:
                db.close()
            old_job["next"] = start
    finally:
        old_job.update(running=False, last=datetime.utcnow().isoformat() + "Z")
