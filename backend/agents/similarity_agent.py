from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein
from typing import List, Dict
from datetime import datetime, date as _date
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from nice_classes_ro import get_nice_description, get_nice_short
from agents.phonetic_agent import phonetic_score

OFFICE_NAMES = {
    "EM": "EUIPO (Uniunea Europeană)",
    "WO": "WIPO (Internațional)",
    "RO": "OSIM (România)",
    "DE": "DPMA (Germania)",
    "FR": "INPI (Franța)",
    "IT": "UIBM (Italia)",
    "ES": "OEPM (Spania)",
    "PT": "INPI (Portugalia)",
    "NL": "BOIP (Olanda)",
    "BE": "BOIP (Belgia)",
    "AT": "APO (Austria)",
    "PL": "UPRP (Polonia)",
    "CZ": "IPO (Cehia)",
    "HU": "HIPO (Ungaria)",
    "BG": "BPO (Bulgaria)",
    "HR": "DZIV (Croația)",
    "SK": "IPO (Slovacia)",
    "SI": "SIPO (Slovenia)",
    "GR": "OBI (Grecia)",
    "SE": "PRV (Suedia)",
    "DK": "DKPTO (Danemarca)",
    "FI": "PRH (Finlanda)",
    "IE": "IPOI (Irlanda)",
    "GB": "UKIPO (Marea Britanie)",
    "CH": "IGE/IPI (Elveția)",
    "TR": "TURKPATENT (Turcia)",
    "US": "USPTO (SUA)",
    "MD": "AGEPI (Moldova)",
    "UA": "UKRPATENT (Ucraina)",
    "SA": "SAIP (Arabia Saudită)",
    "RU": "ROSPATENT (Rusia)",
    "AM": "AIPA (Armenia)",
}

VALIDITY_YEARS = 10  # standard trademark validity


def _parse_date(raw: str) -> str:
    if not raw:
        return ""
    return str(raw)[:10]


def _calc_expiry(reg_date: str, app_date: str) -> str:
    base = reg_date or app_date
    if not base:
        return ""
    try:
        dt = datetime.strptime(base[:10], "%Y-%m-%d")
        expiry = dt.replace(year=dt.year + VALIDITY_YEARS)
        return expiry.strftime("%Y-%m-%d")
    except Exception:
        return ""




class SimilarityAgent:
    def __init__(self, threshold_very_high: float = 90.0, threshold_high: float = 75.0,
                 threshold_medium: float = 60.0, threshold_small: float = 35.0):
        self.threshold_very_high = threshold_very_high  # risc foarte ridicat ≥ 90%
        self.threshold_high      = threshold_high       # risc ridicat ≥ 75%
        self.threshold_medium    = threshold_medium     # risc mediu ≥ 60%
        self.threshold_small     = threshold_small      # risc scăzut ≥ 35%

    def _calculate(self, query: str, candidate: str) -> Dict:
        q = query.upper().strip()
        c = candidate.upper().strip()

        fuzzy_score   = fuzz.ratio(q, c)
        token_score   = fuzz.token_sort_ratio(q, c)
        partial_score = fuzz.partial_ratio(q, c)   # prinde query-ul ca subcuvânt

        max_len  = max(len(q), len(c), 1)
        lev_dist = Levenshtein.distance(q, c)
        lev_score = (1 - lev_dist / max_len) * 100

        # Scor fonetic
        ph = phonetic_score(q, c)
        ph_combined = ph["phonetic_score"]

        # Pentru mărci multi-cuvânt, partial_ratio contează mai mult
        is_multiword = len(c.split()) > 1
        if is_multiword:
            # dacă query-ul apare ca parte din candidat, scoatem în față
            textual = (fuzzy_score * 0.20 + token_score * 0.25 +
                       lev_score * 0.15 + partial_score * 0.40)
        else:
            textual = (fuzzy_score * 0.40 + token_score * 0.30 +
                       lev_score * 0.20 + partial_score * 0.10)

        combined = textual * 0.70 + ph_combined * 0.30

        return {
            "fuzzy_ratio":            round(fuzzy_score, 1),
            "token_sort_ratio":       round(token_score, 1),
            "partial_ratio":          round(partial_score, 1),
            "levenshtein_similarity": round(lev_score, 1),
            "levenshtein_distance":   lev_dist,
            "textual_score":          round(textual, 1),
            "phonetic_score":         round(ph_combined, 1),
            "jaro_winkler":           ph["jaro_winkler"],
            "metaphone":              ph["metaphone"],
            "metaphone_code_query":   ph["metaphone_code_query"],
            "metaphone_code_candidate": ph["metaphone_code_candidate"],
            "soundex_query":          ph["soundex_query"],
            "soundex_candidate":      ph["soundex_candidate"],
            "combined_score":         round(combined, 1),
            "is_multiword":           is_multiword,
        }

    def _normalize(self, tm: Dict) -> Dict:
        # Prioritate: applicants_detail (din endpoint detail, câmpuri separate)
        # Fallback: applicantName din search results (string concatenat nume+adresă)
        detail_applicants = tm.get("applicants_detail", [])
        if detail_applicants:
            applicants = [
                {
                    "name":    a.get("organizationName") or a.get("fullName") or "",
                    "address": a.get("fullAddress", ""),
                    "country": a.get("nationalityCode", "") or a.get("incorporationCountryCode", ""),
                }
                for a in detail_applicants
                if a.get("organizationName") or a.get("fullName")
            ]
        else:
            # applicantName = ["Nume Adresă Țară"] — extragem prima parte ca nume
            raw = tm.get("applicantName", [])
            applicants = []
            for entry in raw:
                if not entry:
                    continue
                # Încearcă să separe numele de adresă: primele 1-3 cuvinte sunt de obicei numele
                parts = str(entry).strip().split()
                # Heuristică: dacă avem cod de țară la final (2 majuscule), îl extragem
                country = ""
                if parts and len(parts[-1]) == 2 and parts[-1].isupper():
                    country = parts[-1]
                    parts = parts[:-1]
                applicants.append({"name": entry, "address": "", "country": country})

        nice_raw   = tm.get("niceClass", [])
        nice_ints  = [int(c) for c in nice_raw if str(c).isdigit()]
        nice_strs  = [str(c) for c in nice_ints]
        nice_detailed = [
            {"class": c, "short": get_nice_short(c), "description": get_nice_description(c)}
            for c in nice_ints
        ]

        app_date = _parse_date(tm.get("applicationDate", ""))
        reg_date = _parse_date(tm.get("registrationDate", ""))
        # Use real expiryDate from detail endpoint if available, else calculate
        exp_raw  = _parse_date(tm.get("expiryDate", ""))
        exp_date = exp_raw if exp_raw else _calc_expiry(reg_date, app_date)

        office_code = tm.get("tmOffice") or tm.get("office", "")
        office_name = OFFICE_NAMES.get(office_code, office_code)

        goods_raw = tm.get("goodAndServices", [])

        goods_normalized = []
        for g in goods_raw:
            nc      = str(g.get("niceClass", ""))
            cls_num = int(nc) if nc.isdigit() else 0
            goods_normalized.append({
                "niceClass":        nc,
                "niceClassInt":     cls_num,
                "niceShort":        get_nice_short(cls_num) if cls_num else "",
                "niceDescription":  get_nice_description(cls_num) if cls_num else "",
                "goodsAndServices": g.get("goodsAndServices", ""),
            })

        return {
            **tm,
            "tmName":           tm.get("tmName") or tm.get("name", ""),
            "office":           office_code,
            "officeName":       office_name,
            "status":           (tm.get("markCurrentStatusCode") or
                                 tm.get("tradeMarkStatus") or
                                 tm.get("status", "")),
            "niceClass":        nice_strs,
            "niceDetailed":     nice_detailed,
            "goodAndServices":  goods_normalized,
            "applicants":       applicants,
            "applicationNumber":   tm.get("applicationNumber", ""),
            "registrationNumber":  tm.get("registrationNumber", ""),
            "applicationDate":  app_date,
            "registrationDate": reg_date,
            "expiryDate":       exp_date,
            "expiryIsReal":     bool(exp_raw),
            "imageUrl":         tm.get("markImageURI") or tm.get("imageUrl"),
            # (550) Natura / tipul mărcii
            "markFeature":      tm.get("markFeature", ""),
            "kindMark":         tm.get("kindMark", ""),
            # Status detaliat
            "markCurrentStatusDate": tm.get("markCurrentStatusDate", ""),
            # (450) Publicare
            "publicationDate":  tm.get("publicationDate", ""),
            # Opoziție
            "oppositionStartDate": tm.get("oppositionStartDate", ""),
            "oppositionEndDate":   tm.get("oppositionEndDate", ""),
            # (531) Coduri Vienna
            "viennaCodes":      tm.get("viennaCodes", []),
            # Țări desemnate Madrid
            "designatedCountries": tm.get("designatedCountries", []),
            # (740) Reprezentant
            "representatives":  tm.get("representatives", []),
            # Link dosar oficial
            "officeUrl":        tm.get("officeUrl", ""),
        }

    def analyze(self, query: str, trademarks: List[Dict],
                nice_classes: List[str] = None) -> Dict:
        today = _date.today()
        conflicts          = []
        similar            = []
        expired_conflicts  = []
        expired_similar    = []

        def _roots(w: str):
            roots = {w}
            if len(w) >= 4:
                roots.add(w[:4])
                roots.add(w[-4:])
                mid = (len(w) - 4) // 2
                roots.add(w[mid:mid + 4])
            return roots

        for tm in trademarks:
            normalized = self._normalize(tm)
            name = normalized["tmName"]
            if not name:
                continue

            # Detectăm dacă marca e inactivă / expirată
            status_raw = (normalized.get("status") or "").lower()
            is_expired = any(w in status_raw for w in [
                "expir", "lapsed", "cancelled", "refused",
                "withdrawn", "surrendered", "invalidated", "abandoned",
            ])
            if not is_expired:
                exp_str = normalized.get("expiryDate", "")
                if exp_str and normalized.get("expiryIsReal"):
                    try:
                        if _date.fromisoformat(exp_str[:10]) < today:
                            is_expired = True
                    except Exception:
                        pass

            scores = self._calculate(query, name)
            sc     = scores["combined_score"]

            words_q = query.upper().split()
            words_c = name.upper().split()

            similar_word_match = any(
                fuzz.ratio(wq, wc) >= 80 or bool(_roots(wq) & _roots(wc))
                for wq in words_q
                for wc in words_c
                if len(wq) >= 4 and len(wc) >= 4
            )
            if similar_word_match and sc < self.threshold_medium:
                sc = max(sc, self.threshold_medium)

            if (query.upper() in name.upper() or name.upper() in query.upper()) and sc < self.threshold_medium:
                sc = max(sc, self.threshold_medium)

            if sc >= self.threshold_very_high:
                risk_level = "very_high"
            elif sc >= self.threshold_high:
                risk_level = "high"
            elif sc >= self.threshold_medium:
                risk_level = "medium"
            elif sc >= self.threshold_small:
                risk_level = "low"
            else:
                partial = scores.get("partial_ratio", 0)
                word_match = any(
                    fuzz.ratio(wq, wc) >= 65
                    for wq in words_q
                    for wc in words_c
                    if len(wq) >= 3 and len(wc) >= 3
                )
                if partial >= 60 or word_match:
                    risk_level = "low"
                else:
                    continue

            entry = {**normalized, "similarity": scores, "risk_level": risk_level}

            if is_expired:
                if sc >= self.threshold_high:
                    expired_conflicts.append(entry)
                else:
                    expired_similar.append(entry)
            else:
                if sc >= self.threshold_high:
                    conflicts.append(entry)
                else:
                    similar.append(entry)

        for lst in (conflicts, similar, expired_conflicts, expired_similar):
            lst.sort(key=lambda x: x["similarity"]["combined_score"], reverse=True)

        return {
            "conflicts":         conflicts,
            "similar":           similar,
            "expired_conflicts": expired_conflicts,
            "expired_similar":   expired_similar,
        }
