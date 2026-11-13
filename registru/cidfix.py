"""Buletine BOPI cu fonturi Arial subset fără tabel de corespondență cod → literă (BOPI 10/2017–04/2019,
01–02/2004): textul iese „(cid:N)”. Fiecare glif e totuși forma exactă din Arial — îl recunoaștem după
contur (încadrare, centru de greutate, dispersie, arie, lățime), comparat cu semnăturile Arial din
arial_sigs.json, și dăm pdfminer-ului litera corectă."""
import io
import json
import os
import re
from typing import Dict

from pdfminer.pdffont import PDFSimpleFont
from pdfminer.pdftypes import resolve1

_SIGS = None
FIX: Dict[str, Dict[int, str]] = {}          # nume font (ex. „NKBEID+ArialMT”) → cod → literă
_orig = PDFSimpleFont.to_unichr


def _to_unichr(self, cid):
    # Tabelul ToUnicode al PDF-ului are prioritate; recunoașterea după contur doar pentru codurile lipsă.
    m = FIX.get(getattr(self, "basefont", ""))
    if m and cid in m:
        um = getattr(self, "unicode_map", None)
        try:
            if um is not None and um.get_unichr(cid):
                return _orig(self, cid)
        except Exception:
            pass
        return m[cid]
    return _orig(self, cid)


PDFSimpleFont.to_unichr = _to_unichr


def _sigs():
    global _SIGS
    if _SIGS is None:
        with open(os.path.join(os.path.dirname(__file__), "arial_sigs.json"), encoding="utf-8") as f:
            _SIGS = json.load(f)
    return _SIGS


def _base(name: str) -> str:
    b = name.split("+")[-1]
    return b if b in _sigs()["sigs"] else ""


# Aceeași grilă ca în arial_sigs.json („bits”): punctele aflate în conturul literei.
_XS, _YS = range(-80, 1240, 40), range(-240, 1000, 40)


def _bits(cs) -> int:
    from fontTools.pens.pointInsidePen import PointInsidePen
    v = 0
    for y in _YS:
        for x in _XS:
            p = PointInsidePen(None, (x, y))
            cs.draw(p)
            v = (v << 1) | bool(p.getResult())
    return v


def _font_map(spec) -> Dict[int, str]:
    from fontTools.cffLib import CFFFontSet
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.statisticsPen import StatisticsPen
    base = _base(str(resolve1(spec.get("BaseFont")).name))
    ref, bits = _sigs()["sigs"][base], _sigs()["bits"][base]
    enc = resolve1(spec.get("Encoding"))
    if not isinstance(enc, dict):
        return {}
    codes, code = {}, 0
    for tok in resolve1(enc.get("Differences")) or []:
        if isinstance(tok, int):
            code = tok
        else:
            codes[code] = getattr(tok, "name", str(tok))
            code += 1
    desc = resolve1(spec.get("FontDescriptor")) or {}
    ff = resolve1(desc.get("FontFile3"))
    if ff is None:
        return {}
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(ff.get_data()), None)
    cs = cff[cff.fontNames[0]].CharStrings
    out = {}
    for code, g in codes.items():
        # Doar glifele fără nume („g12”) — cele cu nume standard („one”) se citesc deja corect.
        if not re.fullmatch(r"g\d+", g) or g not in cs:
            continue
        bp, sp = BoundsPen(None), StatisticsPen(None)
        cs[g].draw(bp)
        cs[g].draw(sp)
        b = list(bp.bounds or (0, 0, 0, 0))
        sig = b + [sp.meanX, sp.meanY, sp.stddevX, sp.stddevY, abs(sp.area) / 1000, cs[g].width, sp.correlation * 300]
        # Întâi cele mai apropiate după măsuri, apoi după forma punct cu punct (Ă / Â, / / \\).
        near = sorted(ref, key=lambda r: sum(abs(x - y) for x, y in zip(r[1:], sig)))[:6]
        mine = _bits(cs[g])
        best = min(near, key=lambda r: bin(mine ^ int(bits[r[0]], 16)).count("1"))
        out[code] = best[0]
    return out


def prepare(pdf) -> int:
    """Pentru un pdfplumber.PDF deschis: recunoaște glifele fonturilor Arial fără corespondență.
    Întoarce câte fonturi au fost refăcute."""
    FIX.clear()
    n = 0
    for page in pdf.pages:
        fonts = resolve1((page.page_obj.resources or {}).get("Font")) or {}
        for ref in fonts.values():
            spec = resolve1(ref)
            name = str(getattr(resolve1(spec.get("BaseFont")), "name", ""))
            if name in FIX or not _base(name):
                continue
            try:
                FIX[name] = _font_map(spec)
                n += bool(FIX[name])
            except Exception as e:
                print(f"[CIDFIX] {name}: {type(e).__name__}: {e}")
                FIX[name] = {}
    return n
