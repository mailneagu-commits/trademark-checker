import os
import requests
from typing import List, Dict

EUIPO_CLIENT_ID     = os.environ.get("EUIPO_CLIENT_ID", "").strip()
EUIPO_CLIENT_SECRET = os.environ.get("EUIPO_CLIENT_SECRET", "").strip()
EUIPO_SEARCH_URL    = "https://api.euipo.europa.eu/trademark-search/trademarks"

if EUIPO_CLIENT_ID:
    print(f"[EUIPO] Configured: {EUIPO_CLIENT_ID[:8]}...")
else:
    print("[EUIPO] Not configured — set EUIPO_CLIENT_ID and EUIPO_CLIENT_SECRET")


def euipo_available() -> bool:
    return bool(EUIPO_CLIENT_ID and EUIPO_CLIENT_SECRET)


def _to_internal(tm: dict) -> dict:
    app_num    = tm.get("applicationNumber", "")
    verbal     = (tm.get("wordMarkSpecification") or {}).get("verbalElement", "")
    applicants = tm.get("applicants") or []
    names      = [a.get("name", "") for a in applicants if a.get("name")]

    def iso(d):
        return f"{d}T00:00:00.000Z" if d else None

    return {
        "ST13":             f"EM{app_num}",
        "tmName":           verbal,
        "tmOffice":         "EM",
        "tradeMarkStatus":  tm.get("status", ""),
        "niceClass":        tm.get("niceClasses") or [],
        "applicantName":    names,
        "applicationDate":  iso(tm.get("applicationDate")),
        "applicationNumber": app_num,
        "registrationDate": iso(tm.get("registrationDate")),
        "expiryDate":       iso(tm.get("expiryDate")),
        "markImageURI":     f"https://www.tmdn.org/tmview/getTMImage?ST13=EM{app_num}" if app_num else None,
        "goodAndServices":  [],
        "_source":          "euipo_api",
    }


def search_euipo(name: str, nice_classes: List[str]) -> List[Dict]:
    if not euipo_available():
        return []

    headers = {
        "X-IBM-Client-Id":     EUIPO_CLIENT_ID,
        "X-IBM-Client-Secret": EUIPO_CLIENT_SECRET,
        "Accept": "application/json",
    }

    nc_ints = [str(int(c)) for c in nice_classes if c.isdigit()]
    nc_filter = f";niceClasses=in=({','.join(nc_ints)})" if nc_ints else ""

    upper = name.upper()
    queries = [
        f"wordMarkSpecification.verbalElement=={upper}",
        f"wordMarkSpecification.verbalElement==*{upper}*",
    ]

    seen: set = set()
    all_marks: List[Dict] = []

    for q in queries:
        resp = requests.get(
            EUIPO_SEARCH_URL,
            headers=headers,
            params={"query": q + nc_filter, "size": 100, "page": 0},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            items = (data.get("trademarks") or data.get("items") or
                     data.get("results") or data.get("data") or [])
            for tm in items:
                key = tm.get("applicationNumber", "")
                if key and key not in seen:
                    seen.add(key)
                    all_marks.append(_to_internal(tm))
        elif resp.status_code in (401, 403):
            # Propagăm eroarea — nu returnăm [] silențios
            raise Exception(f"EUIPO {resp.status_code}: {resp.text[:120]}")
        else:
            print(f"[EUIPO] {resp.status_code}: {resp.text[:100]}")

    if all_marks:
        print(f"[EUIPO] Found {len(all_marks)} marks for '{name}'")
    return all_marks
