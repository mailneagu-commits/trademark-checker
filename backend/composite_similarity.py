"""
Scor compus de similaritate pentru monitorizarea pe buletine (ProMonitor).

Scorul final = MAXIMUL dintre metodele de mai jos, toate aplicate pe denumirile normalizate
(litere mici, fără diacritice, fără spații / cratime / punctuație / ® ™):

  • nucleu      — se scot prefixele și sufixele descriptive (BIO-, ECO-, PRO-, -IA, -UL, …) și se
                  compară nucleele cu Levenshtein normalizat (1 − distanța / lungimea maximă);
  • subșir      — cel mai lung subșir comun (≥ 4 caractere) / lungimea mărcii mai scurte;
  • includere   — nucleul unei mărci (≥ 4 caractere) cuprins integral în cealaltă → minimum 0,80;
  • trigrame    — similaritatea Dice pe trigrame (2 × comune / total);
  • metoda existentă (textual 70% + fonetic 30%) — calculată în daily_monitor și comparată acolo.

Prefiltru: perechile fără nicio trigramă comună primesc 0 fără alte calcule (mii de comparații pe zi).
Listele de afixe de mai jos se pot edita direct; afixele se scot doar dacă nucleul rămas are ≥ 4 litere.
"""
from __future__ import annotations

import os
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Dict, Tuple

from rapidfuzz.distance import Levenshtein

# ── Configurare (ușor de editat) ────────────────────────────────────────────────────────────────
PREFIXES = ["bio", "eco", "pro", "neo", "euro", "multi", "super", "mega", "max", "maxi", "mini",
            "top", "smart", "natur", "vita", "ro", "the"]
SUFFIXES = ["ia", "ul", "ului", "ii", "ele", "lor", "ro", "plus", "max", "pro", "ex", "ix", "line",
            "shop", "store", "group", "sa", "srl"]
MIN_CORE = 4                 # nucleul rămas după scoaterea unui afix
MIN_SUBSTRING = int(os.environ.get("MONITOR_MIN_SUBSTRING", "5"))  # subșirul comun minim („subșir”)
INCLUSION_FLOOR = 0.80       # scorul minim când un nucleu e cuprins integral în cealaltă marcă

# Pragul minim de afișare a unui conflict (dacă marca monitorizată nu are prag propriu).
MIN_DISPLAY = float(os.environ.get("MONITOR_MIN_LIKE", "0.55"))

# Grade: ≥ 85% ridicat, 70–84% mediu, 55–69% scăzut.
GRADES = ((0.85, "ridicat"), (0.70, "mediu"), (0.0, "scăzut"))

METHOD_LABELS = {
    "nucleu": "nucleu fără afixe",
    "levenshtein": "Levenshtein",
    "subsir": "subșir comun",
    "includere": "includere",
    "trigrame": "trigrame (Dice)",
    "existenta": "textual + fonetic",
}


def grade(score: float) -> str:
    for lim, name in GRADES:
        if score >= lim:
            return name
    return "scăzut"


def normalize(name: str) -> str:
    t = (name or "").replace("®", "").replace("™", "")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)


_PRE = sorted(PREFIXES, key=len, reverse=True)
_SUF = sorted(SUFFIXES, key=len, reverse=True)


def core(norm: str) -> str:
    """Nucleul distinctiv: un prefix și un sufix descriptiv scoase (cele mai lungi care se potrivesc),
    numai dacă rămân cel puțin MIN_CORE caractere."""
    s = norm
    for p in _PRE:
        if s.startswith(p) and len(s) - len(p) >= MIN_CORE:
            s = s[len(p):]
            break
    for x in _SUF:
        if s.endswith(x) and len(s) - len(x) >= MIN_CORE:
            s = s[:-len(x)]
            break
    return s


def _trigrams(s: str) -> set:
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else ({s} if s else set())


# Subșiruri comune care nu spun nimic despre marcă (cuvinte descriptive des întâlnite).
GENERIC_SUBSTRINGS = {"sport", "group", "grup", "brand", "event", "events", "center", "centre",
                      "centru", "studio", "media", "design", "medical", "pharma", "beauty", "store",
                      "market", "house", "home", "point", "smart", "super", "power", "solution",
                      "service", "consult", "academ", "estate", "travel", "tours", "invest",
                      "global", "international", "foods", "coffee", "garden", "residence"}


def _words(name: str):
    t = (name or "").replace("®", "").replace("™", "")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return [w for w in re.split(r"[^a-z0-9]+", t) if w]


def prepare(name: str) -> Dict:
    n = normalize(name)
    return {"norm": n, "core": core(n), "tri": _trigrams(n), "words": _words(name)}


def _affix_rest(rest_pre: str, rest_suf: str) -> bool:
    """Ce rămâne dintr-un cuvânt în jurul nucleului sunt doar afixe descriptive (sau nimic)."""
    return (not rest_pre or rest_pre in PREFIXES) and (not rest_suf or rest_suf in SUFFIXES)


def _included(c: str, other: Dict) -> bool:
    """Nucleul c apare într-un cuvânt al celeilalte mărci, înconjurat doar de afixe descriptive:
    PLAFAR în „BioPlafar.ro”, VITAL în „ECOVITAL”. Nu și RAMI în „PENSIERAMI” sau POST în „APOSTOLORUM”."""
    if len(c) < MIN_CORE:
        return False
    for w in other["words"] + [other["norm"]]:
        i = w.find(c)
        while i >= 0:
            if _affix_rest(w[:i], w[i + len(c):]):
                return True
            i = w.find(c, i + 1)
    return False


def _longest_common(a: Dict, b: Dict) -> str:
    """Cel mai lung subșir comun, căutat în interiorul cuvintelor (nu peste granița dintre cuvinte)."""
    best = ""
    for wa in a["words"]:
        for wb in b["words"]:
            if min(len(wa), len(wb)) <= len(best):
                continue
            m = SequenceMatcher(None, wa, wb, autojunk=False).find_longest_match(0, len(wa), 0, len(wb))
            if m.size > len(best):
                best = wa[m.a:m.a + m.size]
    return best


def _lev(a: str, b: str) -> float:
    m = max(len(a), len(b))
    return 1.0 - Levenshtein.distance(a, b) / m if m else 0.0


def composite(a: Dict, b: Dict) -> Tuple[float, str]:
    """(scor 0..1, metoda) pentru două denumiri pregătite cu prepare()."""
    na, nb = a["norm"], b["norm"]
    if not na or not nb:
        return 0.0, ""
    if na == nb:
        return 1.0, "identica"
    common = a["tri"] & b["tri"]
    if not common:                                    # prefiltru rapid
        return 0.0, ""
    best, method = 0.0, ""

    def take(score: float, m: str):
        nonlocal best, method
        if score > best:
            best, method = score, m

    # b) nuclee fără afixe
    ca, cb = a["core"], b["core"]
    # (Levenshtein pe denumirile întregi e deja în metoda existentă — aici contează doar nucleele.)
    if ca != na or cb != nb:
        take(_lev(ca, cb), "nucleu")

    # c) cel mai lung subșir comun (în interiorul cuvintelor, fără cuvintele descriptive uzuale)
    lcs = _longest_common(a, b)
    if len(lcs) >= MIN_SUBSTRING and lcs not in GENERIC_SUBSTRINGS:
        take(len(lcs) / min(len(na), len(nb)), "subsir")

    # d) includere: nucleul (≥ 4) al unei mărci cuprins integral în cealaltă, lângă afixe descriptive
    if best < INCLUSION_FLOOR and (_included(ca, b) or _included(cb, a)):
        take(INCLUSION_FLOOR, "includere")

    # e) Dice pe trigrame
    take(2 * len(common) / (len(a["tri"]) + len(b["tri"])), "trigrame")

    return round(min(best, 1.0), 4), method
