"""
VariantAgent — implementează toate strategiile de căutare:

1. Identic          — termenul exact
2. Fonetic          — substituții C↔K, V↔W, S↔Z, PH↔F, I↔Y etc. (inclusiv combinații)
3. Ortografic       — variante wildcard ? la fiecare poziție (inserție/substituție/ștergere)
4. Prefix/infix/sufix — *TERMEN*, *TERMEN, TERMEN*, subșiruri
5. Vocale similare  — A↔E, O↔U, I↔E, substituții de vocale
6. Plurale/stemming — adaugă/elimină S, ES; elimină sufixe ING, ED, ER
7. Abrevieri/acronime — prima literă din fiecare cuvânt; prescurtări

Convenție wildcard TMview:
  ?  = exact 1 caracter
  *  = 0 sau mai multe caractere
"""
from typing import List, Set
import re

MAX_PAGES_PER_TERM = 5   # limita internă pentru query-uri cu multe rezultate


def build_truncated_root_variants(name: str) -> List[str]:
    """Generate left/right truncated root searches such as *TERM and TERM*."""
    upper = name.upper().strip()
    variants: List[str] = []
    seen: Set[str] = set()

    def add(term: str):
        if term not in seen:
            seen.add(term)
            variants.append(term)

    if len(upper) >= 2:
        add(f"*{upper}")
        add(f"{upper}*")

    return variants


def build_input_list(name: str, substring_lengths: List[int] = None) -> List[str]:
    """Reproduce exact logica build_input_list din ProtectMARK.

    substring_lengths: optional list of integer lengths for internal substrings
    to generate (e.g. [3,4,5]). If None, defaults to [3,4,5].
    """
    upper = name.upper().strip()
    n = len(upper)
    result: List[str] = []
    seen: Set[str] = set()

    def add(term: str):
        if term not in seen:
            seen.add(term)
            result.append(term)

    # 1. Termenul exact
    add(upper)

    # 2. Wildcard simplu
    add(f"*{upper}*")
    add(f"*{upper}")
    add(f"{upper}*")

    # 3. Variante cu ? la fiecare poziție
    for i in range(n):
        prefix = upper[:i]
        suffix_full = upper[i:]        # inclusiv char[i]
        suffix_skip = upper[i + 1:]   # fără char[i]

        # Inserție: ? înainte de poziția i (char[i] rămâne)
        add(f"*{prefix}?{suffix_full}*")

        # Substituție/ștergere: ? înlocuiește char[i]
        if suffix_skip:
            add(f"*{prefix}?{suffix_skip}*")
        else:
            # Ultima literă — și variantă cu suffix wildcard
            add(f"*{prefix}?*")

    # 4. Tăieri progresive din margini:
    #    elimină k litere din stânga și din dreapta (k >= 1)
    #    până când segmentul rămas are minimum 4 litere.
    if n >= 5:
        max_trim = n - 4
        for k in range(1, max_trim + 1):
            add(f"*{upper[k:]}*")     # fără primele k litere
            add(f"*{upper[:-k]}*")    # fără ultimele k litere

        # Tăieri simultane din stânga și dreapta, păstrând minimum 4 litere.
        for left_trim in range(1, n - 3):
            for right_trim in range(1, n - left_trim - 2):
                middle = upper[left_trim:n - right_trim]
                if len(middle) >= 4:
                    add(f"*{middle}*")

    # 5. Substrings utile (prefex/suffix și segmente interne)
    # By default generate a wider set of window sizes so the UI exposes
    # longer prefix/suffix patterns such as *KARTEZ* and *RTEZIAN*.
    if substring_lengths is None:
        substring_lengths = list(range(3, min(n, 8) + 1))
    # sanitize and unique lengths
    lengths = sorted({int(x) for x in substring_lengths if isinstance(x, int) or (isinstance(x, str) and str(x).isdigit())})
    for L in lengths:
        if n >= L:
            for i in range(0, n - L + 1):
                sub = upper[i:i+L]
                add(f"*{sub}*")

    return result


# Substituții fonetice pereche (bidirecționale)
_PHONETIC_PAIRS: List[tuple] = [
    ("CK", "K"),  ("CK", "C"),
    ("PH", "F"),  ("F",  "PH"),
    ("C",  "K"),  ("K",  "C"),
    ("S",  "Z"),  ("Z",  "S"),
    ("I",  "Y"),  ("Y",  "I"),
    ("W",  "V"),  ("V",  "W"),
    ("X",  "KS"), ("KS", "X"),
    ("QU", "KW"), ("KW", "QU"),
    ("CH", "K"),  ("K",  "CH"), ("TCH","CH"),
    ("GE", "JE"), ("GI", "JI"),
    ("AE", "E"),  ("OE", "O"),
    ("OU", "U"),  ("OO", "U"),
    ("EI", "AI"), ("AI", "EI"),
    ("TZ", "Z"),  ("TS", "Z"),
    ("SCH","SH"), ("SH", "S"),
]


def build_phonetic_variants(name: str) -> List[str]:
    """
    Generează variante fonetice prin substituții pereche (C↔K, V↔W etc.).
    Pasul 2 aplică substituții și pe variantele deja generate, astfel
    încât BUCOVINA → BUKOVINA (C→K) + BUCOWINA (V→W) + BUKOWINA (ambele).
    """
    upper = name.upper().strip()
    seen: Set[str] = set()
    variants: List[str] = []

    def add(w: str):
        if w == upper:
            return
        term = f"*{w}*"
        if term not in seen:
            seen.add(term)
            variants.append(term)
        if w not in seen:
            seen.add(w)
            variants.append(w)

    # Pas 1: substituții simple pe termenul original
    first_level: List[str] = []
    for src, dst in _PHONETIC_PAIRS:
        if src in upper:
            replaced = upper.replace(src, dst)
            if replaced != upper:
                add(replaced)
                first_level.append(replaced)

    # Pas 2: substituții pe variantele din pasul 1
    # (prinde combinații ca BUKOWINA = BUCOVINA cu C→K ȘI V→W)
    for word in first_level:
        for src, dst in _PHONETIC_PAIRS:
            if src in word:
                replaced = word.replace(src, dst)
                if replaced != upper:
                    add(replaced)

    return variants



ALL_EU_TERRITORIES = [
    "AT","BE","BG","HR","CY","CZ","DK","EE","FI","FR",
    "DE","GR","HU","IE","IT","LV","LT","LU","MT","NL",
    "PL","PT","RO","SK","SI","ES","SE"
]

_EU_COUNTRY_SET = set(ALL_EU_TERRITORIES)

# Țări non-UE căutate după oficiu (nu după teritoriu TMview)
_NON_EU_OFFICES = {"GB", "CH", "TR", "UA", "MD", "US", "SA", "RU", "AM"}


_BENELUX = {"BE", "NL", "LU"}
_BENELUX_OFFICES = {"BX", "BENELUX"}

def build_offices_and_territories(user_offices: List[str]):
    """
    Convertește selecția utilizatorului în offices + territories pentru TMview.

    Logică:
    - EM       → territories = ["EM"] + toate cele 27 state membre UE
                 Reproduce exact comportamentul TMview când selectezi 'EUIPO':
                 · mărci EUIPO (office EM)
                 · mărci WIPO care desemnează UE sau state membre individual
                 · mărci naționale din fiecare stat UE
    - WO       → offices = ["WO"]
                 (mărci internaționale WIPO indiferent de teritorii desemnate)
    - BX/BENELUX → territories = ["BX"] + offices = ["EM"]
                 (caută atât Benelux național, cât și marcile europene care acoperă Benelux)
    - BE/NL/LU → territories = [cod_țară]
    - Orice altă țară → territories = [cod_țară]

    Important: pentru o selecție de țară națională (de exemplu RO), nu adăugăm
    automat EUIPO, deoarece aceasta lărgește excesiv căutarea și produce rezultate
    necorespunzătoare pentru cerințele de clasă/office specifice.
    """
    offices_set: Set[str] = set()
    territories_set: Set[str] = set()

    for code in user_offices:
        c = code.upper()
        if c in _BENELUX_OFFICES:
            territories_set.add("BX")
            offices_set.add("EM")
        elif c in _BENELUX:
            territories_set.add(c)
        elif c == "WO":
            offices_set.add("WO")
        elif c == "EM":
            territories_set.add("EM")
            territories_set.update(ALL_EU_TERRITORIES)
        elif c in _EU_COUNTRY_SET:
            territories_set.add(c)
        else:
            territories_set.add(c)

    return sorted(offices_set), sorted(territories_set)


# ── Substituții de vocale similare ────────────────────────────────────
_VOWEL_PAIRS: List[tuple] = [
    ("A", "E"),  ("E", "A"),   # MARC → MERC
    ("O", "U"),  ("U", "O"),   # BOLO → BULU
    ("I", "E"),  ("E", "I"),   # KIMI → KEME
    ("A", "O"),  ("O", "A"),   # MARO → MORO
    ("IE","I"),  ("I", "IE"),  # DIETA → DIETA
]

# ── Sufixe pentru stemming ─────────────────────────────────────────────
_SUFFIXES = ["ING", "INGS", "ED", "ER", "ERS", "LY", "TION", "SION",
             "MENT", "NESS", "ABLE", "IBLE"]


def build_plural_stem_variants(name: str) -> List[str]:
    """
    Strategie 6: Plurale și stemming.
    - Adaugă S / ES la final
    - Elimină S / ES de la final
    - Elimină sufixe comune (-ING, -ED, -ER, -TION etc.)
    """
    upper = name.upper().strip()
    seen: Set[str] = set()
    variants: List[str] = []

    def add(w: str):
        if w and w != upper and len(w) >= 2:
            for term in (f"*{w}*", w):
                if term not in seen:
                    seen.add(term)
                    variants.append(term)

    # Plurale: adaugă S sau ES
    if not upper.endswith("S"):
        add(upper + "S")
    if not upper.endswith("ES"):
        add(upper + "ES")

    # Elimină S / ES de la final
    if upper.endswith("ES") and len(upper) > 4:
        add(upper[:-2])
    elif upper.endswith("S") and len(upper) > 3:
        add(upper[:-1])

    # Stemming: elimină sufixe
    for suf in _SUFFIXES:
        if upper.endswith(suf) and len(upper) - len(suf) >= 3:
            add(upper[:-len(suf)])

    return variants


def build_vowel_variants(name: str) -> List[str]:
    """
    Strategie 5: Substituții de vocale similare (A↔E, O↔U, I↔E etc.)
    """
    upper = name.upper().strip()
    seen: Set[str] = set()
    variants: List[str] = []

    def add(w: str):
        if w and w != upper:
            for term in (f"*{w}*", w):
                if term not in seen:
                    seen.add(term)
                    variants.append(term)

    for src, dst in _VOWEL_PAIRS:
        if src in upper:
            replaced = upper.replace(src, dst)
            if replaced != upper:
                add(replaced)

    return variants


def build_abbreviation_variants(name: str) -> List[str]:
    """
    Strategie 7: Abrevieri și acronime.
    - MUSCLE SAUCE → MS, M.S., MUSCL, SAUC
    - BUCOVINA → BUC, BUKOV
    """
    upper = name.upper().strip()
    words = upper.split()
    variants: List[str] = []
    seen: Set[str] = set()

    def add(w: str):
        if w and w != upper and len(w) >= 2:
            for term in (f"*{w}*", w):
                if term not in seen:
                    seen.add(term)
                    variants.append(term)

    if len(words) >= 2:
        # Acronim: prima literă din fiecare cuvânt
        acronym = "".join(w[0] for w in words if w)
        add(acronym)
        # Acronim cu puncte: M.S.
        add(".".join(w[0] for w in words if w) + ".")
        # Prescurtare: primele 4-5 litere din fiecare cuvânt
        for w in words:
            if len(w) >= 4:
                add(w[:4])
                add(w[:5])
    else:
        # Cuvânt unic: primele 3-5 litere (prefix)
        w = words[0]
        for n in (3, 4, 5):
            if len(w) > n:
                add(w[:n])

    return variants

def build_primary_phonetic_variants(name: str) -> List[str]:
    """Return the most relevant phonetic family variant(s) for the combined search list."""
    upper = name.upper().strip()
    if not upper:
        return []
    phonetics = []
    for term in build_phonetic_variants(name):
        plain = term.replace("*", "")
        if plain and plain.upper() != upper and "Y" in plain.upper():
            phonetics.append(plain.upper())
            break
    return phonetics


def generate_all_variants(name: str, substring_lengths: List[int] = None) -> dict:
    inputs = build_input_list(name, substring_lengths)
    truncated_root = build_truncated_root_variants(name)
    primary_phonetic_variants = build_primary_phonetic_variants(name)
    phonetic = build_phonetic_variants(name)
    plurals = build_plural_stem_variants(name)
    vowels = build_vowel_variants(name)
    abbreviations = build_abbreviation_variants(name)

    all_extra = phonetic + plurals + vowels + abbreviations

    for term in truncated_root:
        if term not in inputs:
            inputs.append(term)

    for phonetic_name in primary_phonetic_variants:
        for term in build_input_list(phonetic_name, substring_lengths):
            if term not in inputs:
                inputs.append(term)

    return {
        "original":      name.upper().strip(),
        "search_terms":  inputs,
        "wildcard":      [t for t in inputs if "*" in t or "?" in t],
        "phonetic":      phonetic,
        "plurals":       plurals,
        "vowels":        vowels,
        "abbreviations": abbreviations,
        "truncated_root": truncated_root,
        "all_extra":     all_extra,
    }
