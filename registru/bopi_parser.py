"""
Citirea buletinelor BOPI Mărci (OSIM) din PDF:

- zilnic   (bopiDDMMYYYY.pdf, din 2022): cererile de marcă publicate
- admise   (cereri_admise_DDMMYYYY.pdf, din 2023): cererile admise la înregistrare
- lunar    (mrc_MM_YYYY.pdf): cap. 1 mărci înregistrate, cap. 2 cereri respinse, apoi tabele
           (transferuri, reînnoiri, limitări) — din care etapa 1 folosește doar cap. 1 și 2

Toate publică fiecare marcă ca bloc cu coduri INID — (210) nr. depozit, (111) nr. marcă,
(151) dată, (732) titular, (740) reprezentant, (540) denumire, (531) Viena, (591) culori,
(511) clase cu produse — pe pagini cu 2 coloane și un watermark (font Arial-Black).
Textul coloanelor e citit în ordine, ca un flux continuu, ca produsele unei mărci care
continuă pe coloana / pagina următoare să rămână la marca lor.
"""
import io
import re
from typing import Dict, Iterator, List, Optional, Tuple

import pdfplumber

WATERMARK = "Arial-Black"
INID = ("111", "116", "141", "151", "156", "180", "181", "186", "210", "220", "230", "300",
        "310", "320", "330", "511", "526", "531", "540", "541", "546", "550", "551", "554",
        "555", "556", "557", "591", "641", "646", "732", "740", "750")
RE_CODE = re.compile(r"\((" + "|".join(INID) + r")\)")
RE_APP = re.compile(r"\(210\)\s*(M\s*\d{4}\s*\d{4,6})", re.I)
RE_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4})")
RE_SEP = re.compile(r"─{3,}|(?:\(cid:\d+\)){3,}")
RE_CID = re.compile(r"\(cid:\d+\)")
# Buletinele mai vechi (≈2008–2015) folosesc o codare veche pentru diacritice.
LEGACY = str.maketrans({"ª": "Ș", "º": "ș", "Þ": "Ț", "þ": "ț", "ã": "ă", "Ã": "Ă"})
# Buletinele ≈2005–2009 (font WPMultinational): diacriticele apar ca glife fără cod Unicode.
CID_LETTERS = {"(cid:213)": "ș", "(cid:212)": "Ș", "(cid:219)": "ț", "(cid:218)": "Ț",
               "(cid:124)": "ă", "(cid:123)": "Ă"}
RE_HEADER = re.compile(r"^BULETINUL OFICIAL DE PROPRIETATE INDUSTRIAL", re.I)
RE_GOODS_HEAD = re.compile(r"^\s*Produse\s+și/sau\s+servicii\s+grupate\s+pe\s+clase\s*:?", re.I)
# „35. Publicitate…” sau, în buletinele mai vechi, „35 Publicitate…” (fără punct).
RE_CLASS = re.compile(r"(?:^|\n)\s*(\d{1,2})\s*(?:\.\s+|\s+(?=[A-ZĂÂÎȘȚŞŢ]))")

# Capitolele buletinului lunar, după titlul paginii de capitol (ordinea contează: titlul cap. 2
# conține și „PUBLICATE”, al cap. 1 și „PUBLICATE” și „ÎNREGISTRATE”).
CHAPTERS = (
    ("rejected", ("RESPINSE",)),
    ("renewed", ("REÎNNOITE", "REINNOITE")),
    ("withdrawn", ("RETRAGERI", "LIMITĂRI", "LIMITARI", "RETRASE")),
    ("changes", ("MODIFICĂRI", "MODIFICARI", "TRANSMITERI")),
    ("registered", ("ÎNREGISTRATE", "INREGISTRATE")),
    ("published", ("PUBLICATE",)),
)


def compact_app(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").upper()


def display_app(app: str) -> str:
    m = re.match(r"^M(\d{4})(\d+)$", app or "")
    if m:
        return f"M {m.group(1)} {m.group(2)}"
    r = re.match(r"^R(\d+)$", app or "")       # marcă reînnoită, cunoscută doar după nr. de marcă
    return f"marca nr. {r.group(1)}" if r else (app or "")


def _iso(d: str) -> str:
    m = RE_DATE.search(d or "")
    if not m:
        return ""
    try:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    except ValueError:
        return ""


def _flat(s: str) -> str:
    s = RE_CID.sub("", s or "")
    s = re.sub(r"(\S)\1{7,}", "", s)                    # linii-separator citite ca litere repetate
    s = re.sub(r"-\n(?=[a-zăâîșţțş])", "", s)   # cuvinte despărțite la capăt de rând
    return re.sub(r"\s+", " ", s).strip(" ,;")


def _clean_lines(text: str) -> str:
    out = []
    for line in (text or "").split("\n"):
        t = line.strip()
        if not t or RE_HEADER.match(t) or re.fullmatch(r"\d{1,4}", t):
            continue
        if len(t) == 1 and t.isalpha():          # litere rătăcite din watermark
            continue
        out.append(line)
    return "\n".join(out)


def _chapter_of(page_text: str) -> Optional[Tuple[str, bool]]:
    """Pagina de titlu a unui capitol / tabel: câteva rânduri, fără blocuri INID.
    Întoarce (capitol, e_tabel) sau None."""
    lines = [l.strip() for l in _clean_lines(page_text).split("\n") if l.strip()]
    if not lines or len(lines) > 8 or RE_APP.search(page_text):
        return None
    head = " ".join(lines).upper()
    for key, words in CHAPTERS:
        if any(w in head for w in words):
            return key, ("TABEL" in head)
    return None


# Declarația de neinvocare a unui drept exclusiv (art. 22 din Legea 84/1998) apare uneori
# lipită de denumirea mărcii, imediat după ea.
RE_DISCLAIMER = re.compile(r"\s+(?=(?:Prin scrisoarea|Prin adresa|Prin răspunsul|Solicitantul|Titularul|"
                           r"În conformitate|In conformitate|Conform (?:art|adresei|prevederilor)|Potrivit)\b)")


def split_disclaimer(name: str) -> Tuple[str, str]:
    parts = RE_DISCLAIMER.split(name or "", maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def parse_block(block: str) -> Optional[Dict]:
    """Un bloc INID → dict. `block` începe cu (210)."""
    marks = list(RE_CODE.finditer(block))
    seg: Dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
        seg.setdefault(m.group(1), block[m.end():end])
    app_m = re.match(r"\s*(M\s*\d{4}\s*\d{4,6})", seg.get("210", ""))
    if not app_m:
        return None
    rec: Dict = {"app_number": compact_app(app_m.group(1))}
    if "111" in seg:
        n = re.search(r"\d{3,7}", seg["111"])
        rec["reg_number"] = n.group(0) if n else ""
    if "151" in seg:
        rec["date_151"] = _iso(seg["151"])
    if "220" in seg:
        rec["filing_date"] = _iso(seg["220"])
    for code, key in (("732", "holder_address"), ("740", "representative_address")):
        if code in seg:
            rec[key] = _flat(seg[code])
    name = _flat(seg.get("540", seg.get("541", seg.get("546", ""))))
    rec["name"], disclaimer = split_disclaimer(name)
    if disclaimer:
        rec["disclaimer"] = disclaimer
    if "531" in seg:
        rec["vienna"] = _flat(re.sub(r"Clasificare\s+Viena\s*:", "", seg["531"], flags=re.I))
    if "591" in seg:
        rec["colors"] = _flat(re.sub(r"Culori\s+revendicate\s*:", "", seg["591"], flags=re.I))
    if "511" in seg:
        g = RE_GOODS_HEAD.sub("", seg["511"].strip())
        g = re.sub(r"^\s*clase\s*:", "", g, flags=re.I)
        parts = RE_CLASS.split("\n" + g)
        goods: Dict[str, str] = {}
        for i in range(1, len(parts) - 1, 2):
            c = int(parts[i])
            if 1 <= c <= 45:
                txt = _flat(parts[i + 1])
                goods[str(c)] = (goods.get(str(c), "") + " " + txt).strip() if str(c) in goods else txt
        rec["goods"] = goods
        rec["classes"] = sorted(int(c) for c in goods)
    for key in ("holder", "representative"):
        full = rec.get(key + "_address", "")
        if full:
            rec[key] = full.split(",")[0].strip()
    return rec


def _thumb(page, img) -> Optional[bytes]:
    try:
        x0, top = max(img["x0"], 0), max(img["top"], 0)
        x1, bottom = min(img["x1"], page.width), min(img["bottom"], page.height)
        if x1 - x0 < 8 or bottom - top < 8:
            return None
        pil = page.crop((x0, top, x1, bottom)).to_image(resolution=150).original.convert("RGB")
        pil.thumbnail((320, 320))
        buf = io.BytesIO()
        pil.save(buf, "WEBP", quality=80)
        return buf.getvalue()
    except Exception:
        return None


RE_ANY_APP = re.compile(r"\bM\s?(\d{4})\s?(\d{5})\b")


def parse_pdf(path: str, kind: str, want_images: bool = True) -> Iterator[Tuple[str, Dict]]:
    """Generează (capitol, marcă) pentru fiecare bloc INID din PDF.

    daily → „published”; admise → „admitted”; monthly → „registered” dacă blocul are (111),
    „rejected” dacă nr. de depozit apare în capitolul / tabelul cererilor respinse, altfel
    „admitted” (în formatul 2022 cererile admise, respinse și mărcile înregistrate curg fără
    pagini de titlu între ele). Paginile cu tabele și capitolele de modificări / reînnoiri /
    retrageri sunt sărite."""
    fixed = {"daily": "published", "admise": "admitted"}.get(kind)
    stream: List[str] = []
    images: Dict[str, bytes] = {}
    rejected: set = set()
    last_app = None
    mode, is_table = "inid", False
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            clean = page.filter(lambda o: WATERMARK not in (o.get("fontname") or ""))
            full = clean.extract_text() or ""
            n_inid = len(RE_APP.findall(full))
            if kind == "monthly":
                h = _chapter_of(full)
                if h:
                    mode, is_table = h
                elif is_table and (n_inid >= 2 or (n_inid and "(511)" in full)):
                    is_table = False                      # după tabel au reînceput blocurile INID
                if mode == "rejected":
                    rejected.update(compact_app("M" + a + b) for a, b in RE_ANY_APP.findall(full))
                if is_table or mode not in ("inid", "registered", "rejected", "published", "admitted"):
                    page.flush_cache()
                    continue
            # Pagină-tabel fără titlu propriu (multe nr. de depozit, fără blocuri INID).
            if n_inid <= 1 and len(RE_ANY_APP.findall(full)) >= 6:
                page.flush_cache()
                continue
            # Antetul paginii e centrat peste ambele coloane — îl tăiem, altfel bucățile lui
            # ajung în textul mărcilor din coloana stângă / dreaptă.
            top0 = 0
            try:
                hdr = [h for h in clean.search(r"BULETINUL OFICIAL DE PROPRIETATE", regex=True)
                       if h["top"] < page.height * 0.15]
                if hdr:
                    top0 = max(h["bottom"] for h in hdr) + 1
                    # Al doilea rând al antetului: data / intervalul de date ale cererilor.
                    dates = [d for d in clean.search(r"\d{2}[/.]\d{2}[/.]\d{4}", regex=True)
                             if top0 - 2 <= d["top"] <= top0 + 16]
                    if dates:
                        top0 = max(d["bottom"] for d in dates) + 1
            except Exception:
                pass
            for box in ((0, top0, page.width / 2, page.height), (page.width / 2, top0, page.width, page.height)):
                col = clean.crop(box)
                stream.append(_clean_lines(col.extract_text() or ""))
                found = sorted(col.search(RE_APP.pattern, regex=True, case=False), key=lambda m: m["top"])
                if want_images and col.images:
                    for img in col.images:
                        owner = None
                        for m in found:
                            if m["top"] <= img["top"]:
                                owner = compact_app(m["groups"][0])
                        owner = owner or last_app
                        if owner and owner not in images:
                            b = _thumb(page, img)
                            if b:
                                images[owner] = b
                if found:
                    last_app = compact_app(found[-1]["groups"][0])
            page.flush_cache()

    text = "\n".join(stream)
    if text.count("º") + text.count("ª") + text.count("þ") > 20:
        text = text.translate(LEGACY)
    if "(cid:" in text:
        for k, v in CID_LETTERS.items():
            text = text.replace(k, v)
    starts = [m.start() for m in RE_APP.finditer(text)]
    for i, s in enumerate(starts):
        block = text[s:starts[i + 1] if i + 1 < len(starts) else len(text)]
        # Tot ce urmează după separatorul de după marcă nu mai e al ei.
        parts = RE_SEP.split(block)
        if "(511)" in parts[0] or len(parts) == 1:
            block = parts[0]
        rec = parse_block(block)
        if not rec:
            continue
        if rec["app_number"] in images:
            rec["image"] = images[rec["app_number"]]
        chapter = fixed or ("registered" if rec.get("reg_number") else
                            "rejected" if rec["app_number"] in rejected else "admitted")
        yield chapter, rec


# ── Tabelele din buletinul lunar: transferuri, reînnoiri, radieri, licențe ──────

RE_TITLE = re.compile(r"^(Tabel|Modific|MODIFIC|Cesiun|CESIUN|Fuziun|FUZIUN|Licen|LICEN|LISTA|Lista|RETRAG|Retrag|"
                      r"Radier|RADIER|Mărci radiate|Cereri retrase)", re.I)
RE_APP_CELL = re.compile(r"M\s*(\d{4})\s*(\d{4,6})")


def _cell(c) -> str:
    return re.sub(r"\s+", " ", (c or "").replace("\n", " ")).strip()


def _change_kind(title: str, header: str) -> Optional[str]:
    h, t = header.lower(), title.upper()
    if "expir" in h and "marc" in h:
        return "renewal"
    if "vechea reprezentare" in h:
        return "representation"
    if "licen" in h or "LICEN" in t:
        return "license"
    if "titular vechi" in h or "cedent" in h:
        return ("Cesiune" if "CESIUN" in t else "Fuziune" if "FUZIUN" in t else
                "Modificare adresă titular" if "ADRES" in t else "Modificare titular")
    if "limitare" in h or "LIMIT" in t:
        return "limitation"
    if "RADIAT" in t:
        return "radiated"
    if "RETRAS" in t or "RETRAGER" in t:
        return "withdrawn"
    return None


def _ids(cell: str) -> Tuple[str, str]:
    """(nr. depozit, nr. marcă) dintr-o celulă ca „M 2004 08656 064710” sau „000589 013573”."""
    m = RE_APP_CELL.search(cell)
    rest = cell[m.end():] if m else cell
    app = f"M{m.group(1)}{m.group(2)}" if m else ""
    nums = re.findall(r"\d{5,7}", rest)
    if not m and len(nums) >= 2:          # format vechi: nr. depozit fără „M”, apoi nr. marcă
        return "", nums[1].lstrip("0")
    return app, (nums[0].lstrip("0") if nums else "")


def parse_changes(path: str) -> Iterator[Dict]:
    """Rândurile tabelelor de modificări / reînnoiri / radieri / licențe / limitări."""
    mode, title = "inid", ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            clean = page.filter(lambda o: WATERMARK not in (o.get("fontname") or ""))
            full = clean.extract_text() or ""
            h = _chapter_of(full)
            if h:
                mode = h[0]
            if mode not in ("changes", "renewed", "withdrawn"):
                page.flush_cache()
                continue
            for line in _clean_lines(full).split("\n")[:6]:
                if RE_TITLE.match(line.strip()):
                    title = line.strip()
                    break
            for table in clean.extract_tables():
                if not table or len(table) < 2:
                    continue
                header = " ".join(_cell(c) for c in table[0])
                kind = _change_kind(title, header)
                if not kind:
                    continue
                for row in table[1:]:
                    cells = [_cell(c) for c in row]
                    if kind == "representation" and len(cells) > 1:
                        # Nr. din registru (adesea gol) + nr. cererii în coloana a doua.
                        cells = [cells[1] + " " + cells[0]] + cells[1:]
                    if not cells or not cells[0].strip() or cells[0].startswith("("):
                        continue
                    rec: Dict = {"kind": kind, "title": title}
                    if kind == "renewal":
                        rec["reg_number"] = re.sub(r"\D.*$", "", cells[0]).lstrip("0")
                        rec["expiry"] = _iso(cells[1] if len(cells) > 1 else "")
                        rec["new"] = cells[3] if len(cells) > 3 else ""
                        # Clasele reînnoite („-” = nespecificate), păstrate pentru mărcile care nu
                        # sunt încă în bază (înregistrate înainte de arhiva disponibilă).
                        rec["old"] = ", ".join(re.findall(r"\d{1,2}", cells[2] if len(cells) > 2 else "")) or "-"
                        if not rec["reg_number"]:
                            continue
                    else:
                        app, reg = _ids(cells[0])
                        if kind in ("radiated", "withdrawn") and len(cells) > 2:
                            reg = reg or re.sub(r"\D", "", cells[2]).lstrip("0")
                        if not (app or reg):
                            continue
                        rec.update(app_number=app, reg_number=reg)
                        if kind in ("Cesiune", "Fuziune", "Modificare titular", "Modificare adresă titular"):
                            rec["old"], rec["new"] = (cells[2] if len(cells) > 2 else ""), (cells[3] if len(cells) > 3 else "")
                        elif kind == "representation":
                            rec["old"], rec["new"] = (cells[2] if len(cells) > 2 else ""), (cells[3] if len(cells) > 3 else "")
                        elif kind == "license":
                            rec["new"] = " · ".join(c for c in cells[3:6] if c)
                    yield rec
            page.flush_cache()
