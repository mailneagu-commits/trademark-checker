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


# Fonturile vechi din PDF-urile BOPI (≈2003–2017) codifică diacriticele altfel: „Õi” = „şi”,
# „b|uturi” = „băuturi”, „protecÛie” = „protecţie”, „R{DULESCU” = „RĂDULESCU”. Le refacem doar într-o
# marcă în care apar semnele sigure (Õ, Û, „|” / „{” lipite de litere) — „HAJDÚ”, „BOKÚ” rămân neatinse.
_LEGACY_MARK = re.compile(r"[ÕÛ]|(?<=[A-Za-zĂÂÎŞŢăâîşţ])[|{]|[|{](?=[A-Za-zĂÂÎŞŢăâîşţ])"
                          # doar majuscule: „ÔI” = „ŞI”, „MARAMUREÔ”, „ASOCIAÚIA”, „COMERÚ” (dar nu „CÔTE”, „HAJDÚ”)
                          r"|\bÔI\b|(?<![CHP])Ô(?=[A-Z]|\b)(?!TEL|TE\b|LE\b)|ÚI(?:A|I|LOR)?\b|ERÚ\b")
_LEGACY_MAP = str.maketrans({"Õ": "ş", "Ô": "Ş", "Û": "ţ", "Ú": "Ţ"})


# Alt caz: text românesc din Windows-1250 citit ca Windows-1252 („cofetãrie”, „substanþelor”, „ºi”).
# Doar în listele de produse (în nume, „ã” / „þ” pot fi legitime: „JOÃO”, „ÞÓR”).
_CP1252 = str.maketrans({"ã": "ă", "Ã": "Ă", "þ": "ţ", "Þ": "Ţ", "º": "ş", "ª": "Ş"})


def fix_cp1252_goods(text: str) -> str:
    if not text or not re.search(r"[A-Za-zăâîşţ][ãþº][a-zăâîşţ]|\bºi\b", text):
        return text
    return text.translate(_CP1252)


def has_legacy_diacritics(*texts) -> bool:
    return any(_LEGACY_MARK.search(t or "") for t in texts)


def fix_legacy_diacritics(text: str) -> str:
    if not text:
        return text
    t = re.sub(r"(?<=[A-Za-zĂÂÎŞŢăâîşţÕÛÔÚ])\|", "ă", text)
    t = re.sub(r"\|(?=[a-zăâîşţÕÛ])", "ă", t)
    t = re.sub(r"(?<=[A-ZĂÂÎŞŢÔÚ])\{|\{(?=[A-ZĂÂÎŞŢÔÚ])", "Ă", t)
    t = t.translate(_LEGACY_MAP)
    t = re.sub(r"\s*˜{3,}\s*", " ", t)               # separatorul „˜˜˜˜˜˜˜” dintre fișe
    return re.sub(r"  +", " ", t).strip()


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def fix_record(x, legacy: Optional[bool] = None):
    """Diacriticele vechi refăcute peste tot într-o fișă (dict / listă / text), dacă fișa are semnele
    sigure; separatorul „˜˜˜” se scoate oricum."""
    if legacy is None:
        legacy = has_legacy_diacritics(*_strings(x))
    if isinstance(x, str):
        return fix_legacy_diacritics(x) if legacy else re.sub(r"\s*˜{3,}\s*", " ", x).strip() if "˜" in x else x
    if isinstance(x, dict):
        out = {k: (fix_goods(v) if k == "goods" and isinstance(v, dict) else fix_record(v, legacy))
               for k, v in x.items()}
        if isinstance(out.get("name"), str):
            # Coduri INID lipite de denumire („JIDVEI … 531) Clasificare Viena…”, „SOBYC (511 )Produse…”).
            out["name"] = _INID_TAIL.sub("", out["name"]).strip()
            if garbled_name(out["name"]) or _PRODUCTS_AS_NAME.match(out["name"]):
                out["name"] = ""               # completată apoi din TMview (ingest._fill_names)
        return out
    if isinstance(x, list):
        return [fix_record(v, legacy) for v in x]
    return x


_INID_TAIL = re.compile(r"\s*\(?\s*5[0-9]{2}\s*\)\s*(Clasificare|Produse|Culori|Mențiuni|Men).*$", re.S)
_PRODUCTS_AS_NAME = re.compile(r"^\s*Produse (și|şi|si)/sau servicii")
_GLYPHS = set('!"#$%&\'’()*+,-./:;<=>?@[\\]^_`{|}~')


def garbled_name(name: str) -> bool:
    """Denumire tipărită cu un font cu codificare proprie (BOPI 2002–2003): textul extras e o înșiruire
    de semne „!"#$%&’"#()*&+…” — nu se poate decoda; denumirea se ia din TMview."""
    t = (name or "").replace(" ", "")
    if len(t) < 4:
        return False
    return sum(c in _GLYPHS for c in t) / len(t) >= 0.6 and t[0] in '!"#$%&'


def fix_goods(goods: dict) -> dict:
    legacy = has_legacy_diacritics(*goods.values())
    return {c: fix_cp1252_goods(fix_record(t, legacy)) for c, t in goods.items()}


# Numele titularului / mandatarului se termină la prima virgulă — afară de forma juridică sau de
# denumirea firmei care continuă după virgulă: „APPLE, INC.”, „BRASOVEANU, CERNAT SI ASOCIATII”,
# „SZATMARI-FILIP, ROȘU - SOCIETATE CIVILĂ DE AVOCAȚI”, „X CO., LTD.”.
_FIRM_PART = re.compile(
    r"^(?:INC|LTD|LLC|LLP|L\.?P|CO|CORP|CORPORATION|COMPANY|LIMITED|PLC|GMBH|AG|KG|SE|N\.?V|B\.?V|S\.?A|"
    r"S\.?P\.?A|S\.?L|S\.?A\.?S|S\.?R\.?L|S\.?C\.?A|S\.?P\.?R\.?L|S\.?C\.?P\.?I|SARL|SAS|PTY|"
    r"PTE|OY|AB|AS|A/S|ApS|K\.?K|S\.?C|IFN|SPA)\.?(?:\s*(?:LTD|LIMITED|INC|LLC)\.?)?$"
    r"|\b(?:ASOCIA[TȚŢ]II|ASOCIA[TȚŢ]ILOR|AVOCA[TȚŢ]I|SOCIETATE|SOCIET[AĂ][TȚŢ]II|CABINET|PARTENERII|PARTNERS|"
    r"ASSOCIATES|& ?CO)\b", re.I)


def party_name(full: str) -> str:
    parts = [p.strip() for p in (full or "").split(",")]
    name = parts[0]
    for p in parts[1:]:
        if not p or re.search(r"\d", p) or not _FIRM_PART.search(p):
            break
        name += ", " + p
    return name


def compact_app(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").upper()


def display_app(app: str) -> str:
    m = re.match(r"^M(\d{4})(\d+)$", app or "")
    if m:
        return f"M {m.group(1)} {m.group(2)}"
    r = re.match(r"^R(\d+)$", app or "")       # marcă reînnoită, cunoscută doar după nr. de marcă
    return f"R{r.group(1)}" if r else (app or "")


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


def _clean_lines(text: str, keep_short: bool = False) -> str:
    """Fără antete și rânduri goale. `keep_short`: rândurile dintr-o literă / din cifre rămân — sunt
    denumiri reale („1”, „108”, „G”, „BAG BANII\n2082”); nr. de pagină e tăiat după poziție."""
    out = []
    for line in (text or "").split("\n"):
        t = line.strip()
        if not t or RE_HEADER.match(t):
            continue
        if not keep_short and (re.fullmatch(r"\d{1,4}", t) or (len(t) == 1 and t.isalpha())):
            continue
        out.append(line)
    return "\n".join(out)


def _page_bottom(clean, page) -> float:
    """Marginea de jos a textului, deasupra numărului de pagină: cifre singure pe rând, centrate,
    în subsolul paginii."""
    try:
        y0 = page.height * 0.88
        words = clean.crop((0, y0, page.width, page.height), strict=False).extract_words()
        nums = [w for w in words if re.fullmatch(r"\d{1,4}", w["text"])
                and page.width * 0.4 < (w["x0"] + w["x1"]) / 2 < page.width * 0.6
                and not any(o is not w and abs(o["top"] - w["top"]) < 4 for o in words)]
        if nums:
            return min(w["top"] for w in nums) - 1
    except Exception:
        pass
    return page.height


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
# Din 2023 apare și ca notificare OSIM / decizie în cap. cererilor admise și al mărcilor înregistrate:
# „La data de 02.02.2024, prin adresa nr. …, OSIM transmite … NOTIFICARE DE NEINVOCARE…”,
# „Cu aplicarea prevederilor art. 22…”, „Prin Hotărârea nr. …”, „NU S-A RASPUNS LA NOTIFICARE…”.
_DISC_OPENERS = (r"(?:Prin scrisoarea|Prin adresa|Prin răspunsul|Prin raspunsul|Prin Hot[ăa]r\w*|Solicitantul|Titularul|"
                 r"[ÎI]?n conformitate|Conform\w* (?:cu )?(?:art|adresei|prevederilor)|Potrivit|"
                 r"La data de\s+\d|Cu a\w+ prevederilor|NU S-A R[ĂA]SPUNS|Se fac aplicabile|La depunerea cererii|"
                 r"Se invoc[ăa] prioritatea|[ÎI]n cazul dumneav)")
RE_DISCLAIMER = re.compile(r"\s+(?=" + _DISC_OPENERS + r")")
RE_DISC_START = re.compile(r"^" + _DISC_OPENERS)


def split_disclaimer(name: str) -> Tuple[str, str]:
    name = (name or "").strip()
    # Toată „denumirea” e de fapt declarația (marcă figurativă, fără text în (540)).
    if len(name) > 40 and RE_DISC_START.match(name) and re.search(r"\bart\.?\s*\d|\bnr\.|OSIM|NOTIFICARE", name, re.I):
        return "", name
    parts = RE_DISCLAIMER.split(name, maxsplit=1)
    if len(parts) > 1:
        rest = parts[1].strip()
        # Doar o declarație adevărată (articol de lege, adresă OSIM…), nu o denumire care conține
        # întâmplător „Potrivit” / „Titularul”.
        if len(rest) > 25 and re.search(r"\bart\.?\s*\d|\bart\b|\bnr\.?\s*\d|OSIM|NOTIFICARE|drept exclusiv|"
                                         r"Legea|Legii|denumir|elementul|elementele|cuv[âa]nt|Comisi|contesta|refuz|prioritat",
                                         rest, re.I):
            return parts[0].strip(), rest
    return name, ""


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
        # Arhiva tipărește nr. de marcă pe 6 cifre (065304); în bază e fără zerouri, ca în tabele.
        rec["reg_number"] = n.group(0).lstrip("0") if n else ""
    if "151" in seg:
        rec["date_151"] = _iso(seg["151"])
    if "220" in seg:
        rec["filing_date"] = _iso(seg["220"])
    for code, key in (("732", "holder_address"), ("740", "representative_address")):
        if code in seg:
            rec[key] = _flat(seg[code])
    # Arhiva (≈2002–2008): denumirea verbală, centrată pe verticală lângă codul (540), e citită
    # cu un rând mai sus, la capătul câmpului anterior: „…BUCUREȘTI\nROSAL\n(540)\n(511)…”.
    # Doar la mărcile verbale (fără (531) Viena) — la cele figurative rândul anterior e adresa.
    for code in ("540", "541", "546"):
        if code in seg and not _flat(seg[code]) and "531" not in seg:
            k = next((i for i, m in enumerate(marks) if m.group(1) == code), None)
            if k:
                prev = marks[k - 1].group(1)
                lines = [l for l in seg.get(prev, "").rstrip().split("\n")]
                last = lines[-1].strip() if lines else ""
                if prev in ("732", "740", "750", "151", "111", "220", "300") and len(lines) >= 2 and last \
                        and not last.endswith((",", "-")) \
                        and not re.search(r"\bROM[AÂ]NIA$|\bnr\.|\bsector\b|\bjud(?:\.|ețul)", last, re.I):
                    seg[code] = lines[-1]
                    seg[prev] = "\n".join(lines[:-1])
            break
    raw_name = seg.get("540", seg.get("541", seg.get("546", "")))
    if "531" in seg:
        # Marcă figurativă / combinată: literele singure pe rând vin din textul logo-ului, nu din denumire.
        raw_name = "\n".join(l for l in raw_name.split("\n") if not (len(l.strip()) == 1 and l.strip().isalpha()))
    name = _flat(raw_name)
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
            rec[key] = party_name(full)
    return rec


def _thumb(page, img) -> Optional[bytes]:
    try:
        x0, top = max(img["x0"], 0), max(img["top"], 0)
        x1, bottom = min(img["x1"], page.width), min(img["bottom"], page.height)
        if x1 - x0 < 8 or bottom - top < 8:
            return None
        pil = page.crop((x0, top, x1, bottom), strict=False).to_image(resolution=150).original.convert("RGB")
        pil.thumbnail((320, 320))
        buf = io.BytesIO()
        pil.save(buf, "WEBP", quality=80)
        return buf.getvalue()
    except Exception:
        return None


RE_ANY_APP = re.compile(r"\bM\s?(\d{4})\s?(\d{5})\b")
# Anexele de la sfârșitul buletinului (liste lungi de produse): „Anexa M 2024 10268” + „Clasa 2: …”.
# Fișa mărcii are atunci în (511) doar „2. Conform Anexa M 2024 10268 pdf publicat in bopi…”.
RE_ANNEX = re.compile(r"(?m)^\s*Anexa\s+M\s*(\d{4})\s*(\d{4,6})\s*$")


def _annex_goods(text: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    parts = RE_ANNEX.split(text)
    for i in range(1, len(parts) - 2, 3):
        app = f"M{parts[i]}{parts[i + 1]}"
        chunk = parts[i + 2]
        cl = re.split(r"(?:^|\n|\s)Clasa\s+(\d{1,2})\s*:", chunk)
        goods = out.setdefault(app, {})
        for j in range(1, len(cl) - 1, 2):
            c = int(cl[j])
            if 1 <= c <= 45:
                txt = _flat(cl[j + 1])
                goods[str(c)] = (goods[str(c)] + " " + txt).strip() if goods.get(str(c)) else txt
    return out


def _prepare_fonts(pdf) -> None:
    """BOPI 10/2017–04/2019: fonturi Arial fără tabel de caractere (text „(cid:N)”) — literele se
    recunosc după formă (cidfix). Regula: marca se ia întotdeauna din buletinul OSIM; TMview doar
    completează ce lipsește."""
    import cidfix
    cidfix.FIX.clear()
    for page in pdf.pages[1:15]:
        n = sum(1 for c in page.chars if c["text"].startswith("(cid:"))
        page.flush_cache()
        if n > 100:
            print(f"[CIDFIX] fonturi refăcute: {cidfix.prepare(pdf)}")
            return


def parse_pdf(path: str, kind: str, want_images: bool = True,
              have_image: Optional[set] = None) -> Iterator[Tuple[str, Dict]]:
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
    annex: List[str] = []
    in_annex = False
    table_names: Dict[str, str] = {}
    mode, is_table = "inid", False
    with pdfplumber.open(path) as pdf:
        _prepare_fonts(pdf)
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
            # Nu și o pagină de fișe în care (511) trimite la anexă: „Conform Anexa M 2024 10268 pdf…”.
            if n_inid <= 1 and len(set(RE_ANY_APP.findall(full))) >= 6 and \
                    not any(c in full for c in ("(511)", "(531)", "(740)", "Conform Anexa")):
                if kind == "daily" and "(540)" in full:
                    # Tabelul-sumar al buletinului zilnic: denumirea exactă a fiecărei cereri.
                    try:
                        for t in clean.extract_tables():
                            for row in t or []:
                                cells = [_cell(c) for c in row]
                                if len(cells) >= 5 and RE_APP_CELL.fullmatch(cells[1] or ""):
                                    # „PUTNA-\nVRANCEA” → „PUTNA-VRANCEA” (cratima de la capăt de rând rămâne).
                                    raw = re.sub(r"(?<=\S)-\n(?=[A-ZĂÂÎȘȚŞŢ0-9])", "-", row[4] or "")
                                    table_names[compact_app(cells[1])] = _flat(raw)
                    except Exception:
                        pass
                page.flush_cache()
                continue
            # Paginile de anexă (text pe toată lățimea, fără fișe) — citite separat, nu pe coloane.
            if RE_ANNEX.search(full) or (in_annex and n_inid == 0):
                in_annex = True
                annex.append(_clean_lines(full))
                page.flush_cache()
                continue
            in_annex = False
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
            bottom = _page_bottom(clean, page)
            for box in ((0, top0, page.width / 2, bottom), (page.width / 2, top0, page.width, bottom)):
                # strict=False: unele pagini au cutia decalată (BOPI 09–10/2025) — decupăm ce e în pagină.
                col = clean.crop(box, strict=False)
                stream.append(_clean_lines(col.extract_text() or "", keep_short=True))
                found = sorted(col.search(RE_APP.pattern, regex=True, case=False), key=lambda m: m["top"])
                if want_images and col.images:
                    for img in col.images:
                        owner = None
                        for m in found:
                            if m["top"] <= img["top"]:
                                owner = compact_app(m["groups"][0])
                        owner = owner or last_app
                        # `have_image`: mărcile care au deja logo în bază (buletin recitit) — nu-l mai decupăm.
                        if owner and owner not in images and not (have_image and owner in have_image):
                            b = _thumb(page, img)
                            if b:
                                images[owner] = b
                if found:
                    last_app = compact_app(found[-1]["groups"][0])
            page.flush_cache()

    text = "\n".join(stream)
    seen_tab: set = set()
    if kind == "monthly" and len(RE_APP.findall(text)) < 30:
        # Buletinele 2001–2004 nu au fișe INID (doar câteva izolate), ci tabelul mărcilor înregistrate.
        for chapter, rec in parse_old_tables(path):
            seen_tab.add(rec["app_number"])
            yield chapter, rec
    if text.count("º") + text.count("ª") + text.count("þ") > 20:
        text = text.translate(LEGACY)
    if "(cid:" in text:
        for k, v in CID_LETTERS.items():
            text = text.replace(k, v)
    annex_goods = _annex_goods("\n".join(annex)) if annex else {}
    starts = [m.start() for m in RE_APP.finditer(text)]
    yielded: set = set()
    for i, s in enumerate(starts):
        block = text[s:starts[i + 1] if i + 1 < len(starts) else len(text)]
        # Tot ce urmează după separatorul de după marcă nu mai e al ei.
        parts = RE_SEP.split(block)
        if "(511)" in parts[0] or len(parts) == 1:
            block = parts[0]
        rec = parse_block(block)
        if not rec or rec["app_number"] in seen_tab:
            continue
        if rec["app_number"] in images:
            rec["image"] = images[rec["app_number"]]
        tn = table_names.get(rec["app_number"])
        same = lambda a: re.sub(r"[\s-]", "", a or "")
        if tn and same(tn) == same(rec.get("name")) and "- " in tn and "- " not in rec.get("name", ""):
            tn = None                      # diferă doar o cratimă de la capăt de rând: fișa e mai curată
        if tn and tn != rec.get("name"):
            # Denumirea din tabelul-sumar e cea publicată; textul în plus din fișă rămâne declarație.
            name, disc = split_disclaimer(tn)
            rec["name"] = name
            if disc and not rec.get("disclaimer"):
                rec["disclaimer"] = disc
        ag = annex_goods.get(rec["app_number"])
        if ag:
            goods = dict(rec.get("goods") or {})
            for c, txt in ag.items():
                if txt and (not goods.get(c) or re.search(r"Conform\s+Anexa", goods[c], re.I)):
                    goods[c] = txt
            rec["goods"], rec["classes"] = goods, sorted(int(c) for c in goods)
        chapter = fixed or ("registered" if rec.get("reg_number") else
                            "rejected" if rec["app_number"] in rejected else "admitted")
        yielded.add(rec["app_number"])
        yield chapter, rec
    # Cereri respinse care apar doar în tabelul de respingeri (ex. după contestații) — fără fișă
    # INID în acest buletin: actualizăm doar starea mărcii, dacă e deja în bază.
    for app in sorted(rejected - yielded):
        yield "rejected", {"app_number": app, "_status_only": True}


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
    if "adresă veche" in h or "adresa veche" in h:
        return "Modificare adresă titular"
    if "titular vechi" in h or "cedent" in h:
        if "CESIUN" in t and ("PARȚIAL" in t or "PARTIAL" in t or "PARŢIAL" in t):
            return "Cesiune parțială"
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


RE_LIM_HEAD = re.compile(r"Nr\.\s*depozit,\s*dat[ăa]\s*depozit,\s*nr\.\s*marc[ăa]", re.I)
RE_OBS = re.compile(r"(\d{1,2})\s*-\s*(neschimbat[ăa]|modificat[ăa]|[șşs]tears[ăa]|[șşs]ters[ăa]?|radiat[ăa]|eliminat[ăa]|retras[ăa])", re.I)
RE_LIM_CLASS = re.compile(r"(?:^|\n)\s*(\d{1,2})\s*-\s+(?=[A-ZĂÂÎȘȚŞŢa-zăâîșțşţ])")


def _lim_goods(text: str) -> Dict[str, str]:
    """„5 - Preparate farmaceutice…  30 - Produse de brutărie…” → {clasă: produse}."""
    parts = RE_LIM_CLASS.split("\n" + (text or ""))
    goods: Dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        c = int(parts[i])
        if 1 <= c <= 45:
            goods[str(c)] = _flat(parts[i + 1])
    return goods


def _lim_record(ids: str, obs_text: str, goods_text: str) -> Optional[Dict]:
    app, reg = _ids(ids)
    if not (app or reg):
        return None
    obs = {str(int(c)): st.lower() for c, st in RE_OBS.findall(obs_text or "")}
    deleted = sorted((c for c, st in obs.items() if re.match(r"[șşs]te|radiat|elimin|retras", st)), key=int)
    return {"kind": "limitation", "title": "Limitare la cererea titularului", "app_number": app,
            "reg_number": reg, "obs": obs, "deleted": deleted, "goods": _lim_goods(goods_text)}


def _parse_limitation_text(text: str) -> Iterator[Dict]:
    """Formatul 2023+: fișe verticale „Nr. depozit, dată depozit, nr. marcă / Nume titular /
    Observații / Clasificare după limitare”."""
    chunks = RE_LIM_HEAD.split(text)
    for ch in chunks[1:]:
        m = RE_APP_CELL.search(ch)
        if not m:
            continue
        ids = ch[m.start():m.start() + 60].split("\n")[0]
        cls_pos = re.search(r"Clasificare\s+după\s+limitare", ch, re.I)
        before = ch[:cls_pos.start()] if cls_pos else ch
        after = ch[cls_pos.end():] if cls_pos else ""
        rec = _lim_record(ids, before, after)
        if rec:
            yield rec


def parse_changes(path: str) -> Iterator[Dict]:
    """Rândurile tabelelor de modificări / reînnoiri / radieri / licențe / limitări."""
    mode, title = "inid", ""
    lim_text: List[str] = []
    lim_rows: List[List[str]] = []
    with pdfplumber.open(path) as pdf:
        _prepare_fonts(pdf)
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
            if "LIMIT" in title.upper() or RE_LIM_HEAD.search(full):
                lim_text.append(_clean_lines(full))
            for table in clean.extract_tables():
                if not table or len(table) < 2:
                    continue
                header = " ".join(_cell(c) for c in table[0])
                if "după limitare" in header.lower():
                    # Formatul 2022: rânduri; un rând fără nr. depozit continuă rândul anterior (pagină nouă).
                    ci = next(i for i, c in enumerate(table[0]) if "limitare" in _cell(c).lower())
                    oi = next((i for i, c in enumerate(table[0]) if "observ" in _cell(c).lower()), None)
                    for row in table[1:]:
                        cells = [_cell(c) for c in row]
                        ids, cls_t = cells[0], cells[ci] if ci < len(cells) else ""
                        obs_t = cells[oi] if oi is not None and oi < len(cells) else ""
                        if RE_APP_CELL.search(ids) or re.search(r"\d{5,}", ids):
                            lim_rows.append([ids, obs_t, cls_t])
                        elif lim_rows:
                            lim_rows[-1][1] += " " + obs_t
                            lim_rows[-1][2] += "\n" + cls_t
                    continue
                if RE_LIM_HEAD.match(_cell(table[0][0])):
                    continue            # fișele verticale 2023+ — citite din text, mai jos
                kind = _change_kind(title, header)
                if not kind or kind == "limitation":
                    continue
                # Coloana „Denumire marcă” (transferuri, schimbări de nume / adresă, licențe, radieri):
                # singura sursă BOPI pentru denumirea mărcilor vechi, reînnoite (lista de reînnoiri n-o are).
                ni = next((i for i, c in enumerate(table[0]) if "denumire" in _cell(c).lower()), None)
                for row in table[1:]:
                    cells = [_cell(c) for c in row]
                    tm_name = cells[ni] if ni is not None and ni < len(cells) and kind != "representation" else ""
                    if kind == "representation" and len(cells) > 1:
                        # Nr. din registru (adesea gol) + nr. cererii în coloana a doua.
                        cells = [cells[1] + " " + cells[0]] + cells[1:]
                    if not cells or not cells[0].strip() or cells[0].startswith("("):
                        continue
                    rec: Dict = {"kind": kind, "title": title}
                    if kind == "renewal":
                        rec["reg_number"] = re.sub(r"\D.*$", "", cells[0]).lstrip("0")
                        # „008649(5R)” — a câta reînnoire, tipărită de OSIM (fără sufix = prima).
                        nr = re.search(r"\((\d*)\s*R\)", cells[0])
                        rec["renewal_no"] = int(nr.group(1) or 1) if nr else 0
                        rec["expiry"] = _iso(cells[1] if len(cells) > 1 else "")
                        rec["new"] = cells[3] if len(cells) > 3 else ""
                        rec["rep"] = cells[4] if len(cells) > 4 else ""      # (740) mandatarul la reînnoire
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
                        if kind in ("Cesiune", "Cesiune parțială", "Fuziune", "Modificare titular", "Modificare adresă titular"):
                            rec["old"], rec["new"] = (cells[2] if len(cells) > 2 else ""), (cells[3] if len(cells) > 3 else "")
                        elif kind == "representation":
                            rec["old"], rec["new"] = (cells[2] if len(cells) > 2 else ""), (cells[3] if len(cells) > 3 else "")
                        elif kind == "license":
                            rec["new"] = " · ".join(c for c in cells[3:6] if c)
                    if tm_name and not RE_CODE.fullmatch(tm_name.strip()):
                        rec["name"] = tm_name
                    yield rec
            page.flush_cache()
    for ids, obs_t, cls_t in lim_rows:
        rec = _lim_record(ids, obs_t, cls_t.replace(" | ", "\n"))
        if rec:
            yield rec
    if not lim_rows:
        yield from _parse_limitation_text("\n".join(lim_text))


# ── Buletinele 2001–2004: mărcile înregistrate apar doar în tabel ───────────────
# Antet: Nr. depozit (210) | Data depozit (151) | Nr. marcă (111) | Titular (732) | Denumire (540) | Clase (511)
TABLE_CODES = ("(210)", "(151)", "(111)", "(732)", "(540)", "(511)")


def parse_old_tables(path: str) -> Iterator[Tuple[str, Dict]]:
    with pdfplumber.open(path) as pdf:
        _prepare_fonts(pdf)
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            codes = {w["text"]: w for w in words if w["text"] in TABLE_CODES}
            if len(codes) < 6:
                page.flush_cache()
                continue
            head_bottom = max(w["bottom"] for w in codes.values())
            # Codurile sunt centrate deasupra coloanelor: granița dintre două coloane e la mijlocul
            # distanței dintre centrele codurilor.
            body = sorted((w for w in words if w["top"] > head_bottom + 1), key=lambda w: (round(w["top"]), w["x0"]))
            # Codurile sunt centrate deasupra coloanelor; începutul real al unei coloane e marginea
            # stângă cea mai frecventă a cuvintelor dintre centrul codului anterior și centrul ei.
            from collections import Counter
            centers = [(codes[c]["x0"] + codes[c]["x1"]) / 2 for c in TABLE_CODES]
            lefts = Counter(round(w["x0"]) for w in body)
            xs = [0]
            for k in range(1, len(centers)):
                cand = [(n, x) for x, n in lefts.items() if centers[k - 1] < x <= centers[k]]
                xs.append((max(cand)[1] - 2) if cand else (centers[k - 1] + centers[k]) / 2)

            def col(w):
                i = 0
                for k, x in enumerate(xs):
                    if w["x0"] >= x:
                        i = k
                return i

            # Rândurile încep la „M” din prima coloană; fiecare cuvânt aparține rândului care
            # începe deasupra lui (titularul / denumirea / clasele continuă pe rândurile următoare).
            starts = sorted(w["top"] for w in body if col(w) == 0 and w["text"] == "M")
            rows: List[List[List[str]]] = [[[] for _ in TABLE_CODES] for _ in starts]
            for w in sorted(body, key=lambda w: (w["top"], w["x0"])):
                i = -1
                for k, t in enumerate(starts):
                    if w["top"] >= t - 6:
                        i = k
                    else:
                        break
                if i >= 0:
                    rows[i][col(w)].append(w["text"])
            for r in rows:
                dep = " ".join(r[0])
                m = re.match(r"M\s*(\d{4})\s*(\d{4,6})", dep)
                if not m:
                    continue
                reg = re.sub(r"\D", "", " ".join(r[2])).lstrip("0")
                classes = sorted({int(x) for x in re.findall(r"\d{1,2}", " ".join(r[5])) if 1 <= int(x) <= 45})
                holder = _flat(" ".join(r[3])).translate(LEGACY)
                name = _flat(" ".join(r[4])).translate(LEGACY)
                yield "registered", {
                    "app_number": f"M{m.group(1)}{m.group(2)}", "reg_number": reg,
                    "date_151": _iso(" ".join(r[1])), "holder": holder, "holder_address": holder,
                    "name": name, "classes": classes, "goods": {str(c): "" for c in classes},
                }
            page.flush_cache()
