"""
PhoneticAgent — calculează similaritatea fonetică între două denumiri de mărci.
Algoritmi folosiți:
  - Double Metaphone (cel mai precis pentru limbi europene)
  - Soundex
  - NYSIIS (New York State Identification and Intelligence System)
  - Jaro-Winkler (distanță string cu ponderi pentru prefix)
"""
import jellyfish
from typing import Dict


def _metaphone_match(a: str, b: str) -> float:
    """Compară codurile Double Metaphone. Returnează 0.0–1.0."""
    ma = jellyfish.metaphone(a.upper())
    mb = jellyfish.metaphone(b.upper())
    if not ma or not mb:
        return 0.0
    if ma == mb:
        return 1.0
    # Similaritate parțială prin prefix comun
    common = 0
    for ca, cb in zip(ma, mb):
        if ca == cb:
            common += 1
        else:
            break
    return common / max(len(ma), len(mb))


def _soundex_match(a: str, b: str) -> float:
    sa = jellyfish.soundex(a.upper())
    sb = jellyfish.soundex(b.upper())
    if sa == sb:
        return 1.0
    # Parțial: prima literă + primele cifre
    if sa[0] == sb[0]:
        match_digits = sum(1 for x, y in zip(sa[1:], sb[1:]) if x == y)
        return 0.4 + (match_digits / 3) * 0.4
    return 0.0


def _nysiis_match(a: str, b: str) -> float:
    try:
        na = jellyfish.nysiis(a.upper())
        nb = jellyfish.nysiis(b.upper())
    except Exception:
        return 0.0
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    common = sum(1 for ca, cb in zip(na, nb) if ca == cb)
    return common / max(len(na), len(nb))


def _truncated_root_match(query: str, candidate: str) -> float:
    """
    Compară rădăcinile trunchiate (prefixe, sufixe, medii).
    Min 3 litere, max 70% din lungime cuvântului.
    Returnează scor 0.0–1.0 bazat pe cel mai bun match.
    """
    q = query.upper().strip()
    c = candidate.upper().strip()

    if len(q) < 3 or len(c) < 3:
        return 0.0

    # Lungimi minime și maxime pentru subiecturi
    min_len = max(3, int(len(q) * 0.30))
    max_len = int(len(q) * 0.70)

    if min_len > len(q):
        return 0.0

    best_score = 0.0

    # Extrag toate subiecturi posibile din query
    for length in range(min_len, min(max_len + 1, len(q) + 1)):
        # Prefixe (right-truncated root): luam din stanga
        for start in range(len(q) - length + 1):
            substring = q[start:start + length]

            # Verific PREFIX*: daca candidatul incepe cu substring
            if c.startswith(substring):
                score = length / len(c)
                best_score = max(best_score, score)

            # Verific *SUFFIX: daca candidatul se termina cu substring
            if c.endswith(substring):
                score = length / len(c)
                best_score = max(best_score, score)

            # Verific *MIDDLE*: daca substring apare in interior (nu la capete)
            if substring in c[1:-1]:
                score = (length / len(c)) * 0.9  # Penalizez putin matching-ul de mijloc
                best_score = max(best_score, score)

    return best_score


def phonetic_score(query: str, candidate: str) -> Dict:
    """
    Calculează scorul fonetic complet între query și candidate.
    Returnează scoruri individuale și scorul combinat ponderat.
    Include și verificarea rădăcinilor trunchiate.
    """
    q = query.upper().strip()
    c = candidate.upper().strip()

    jw_score      = jellyfish.jaro_winkler_similarity(q, c)
    metaphone_sc  = _metaphone_match(q, c)
    soundex_sc    = _soundex_match(q, c)
    nysiis_sc     = _nysiis_match(q, c)
    truncated_sc  = _truncated_root_match(q, c)

    # Ponderi: Jaro-Winkler 25%, Metaphone 25%, Soundex 10%, NYSIIS 10%, Truncated 30%
    combined = (jw_score * 0.25 + metaphone_sc * 0.25 +
                soundex_sc * 0.10 + nysiis_sc * 0.10 + truncated_sc * 0.30) * 100

    return {
        "jaro_winkler":   round(jw_score * 100, 1),
        "metaphone":      round(metaphone_sc * 100, 1),
        "soundex":        round(soundex_sc * 100, 1),
        "nysiis":         round(nysiis_sc * 100, 1),
        "truncated_root": round(truncated_sc * 100, 1),
        "phonetic_score": round(combined, 1),
        "metaphone_code_query":     jellyfish.metaphone(q),
        "metaphone_code_candidate": jellyfish.metaphone(c),
        "soundex_query":            jellyfish.soundex(q),
        "soundex_candidate":        jellyfish.soundex(c),
    }
