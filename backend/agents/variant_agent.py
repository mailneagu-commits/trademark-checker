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


def build_input_list(name: str) -> List[str]:
    """Reproduce exact logica build_input_list din ProtectMARK."""
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

    # 4. Subșiruri finale (ultimele 2 litere eliminate, prima literă eliminată)
    if n >= 4:
        add(f"*{upper[:-1]}*")    # fără ultima literă
        add(f"*{upper[1:]}*")     # fără prima literă

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
    - BE/NL/LU → territories = ["BX"]   (teritoriu Benelux)
    - Orice altă țară → territories = [cod_țară]
    """
    offices_set:     Set[str] = set()
    territories_set: Set[str] = set()

    has_eu_country = False
    for code in user_offices:
        c = code.upper()
        if c in _BENELUX:
            territories_set.add("BX")
            has_eu_country = True
        elif c == "WO":
            offices_set.add("WO")
        elif c == "EM":
            territories_set.add("EM")
            territories_set.update(ALL_EU_TERRITORIES)
        elif c in _EU_COUNTRY_SET:
            territories_set.add(c)
            has_eu_country = True
        else:
            territories_set.add(c)

    # Orice stat UE selectat → adăugăm și EUIPO (mărci europene acoperă toate țările UE)
    if has_eu_country and "EM" not in territories_set:
        offices_set.add("EM")

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


def generate_all_variants(name: str) -> dict:
    inputs        = build_input_list(name)
    phonetic      = build_phonetic_variants(name)
    plurals       = build_plural_stem_variants(name)
    vowels        = build_vowel_variants(name)
    abbreviations = build_abbreviation_variants(name)

    all_extra = phonetic + plurals + vowels + abbreviations

    return {
        "original":      name.upper().strip(),
        "search_terms":  inputs,
        "wildcard":      [t for t in inputs if "*" in t or "?" in t],
        "phonetic":      phonetic,
        "plurals":       plurals,
        "vowels":        vowels,
        "abbreviations": abbreviations,
        "all_extra":     all_extra,
    }
