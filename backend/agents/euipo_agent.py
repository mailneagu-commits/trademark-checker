import os
import time
import requests
from typing import List, Dict

EUIPO_CLIENT_ID     = os.environ.get("EUIPO_CLIENT_ID", "")
EUIPO_CLIENT_SECRET = os.environ.get("EUIPO_CLIENT_SECRET", "")
EUIPO_TOKEN_URL     = "https://euipo.europa.eu/cas-server-webapp/oidc/accessToken"
EUIPO_SEARCH_URL    = "https://api.euipo.europa.eu/trademark-search/trademarks"

if EUIPO_CLIENT_ID:
    print(f"[EUIPO] Configured with Client ID: {EUIPO_CLIENT_ID[:8]}...")
else:
    print("[EUIPO] Not configured — set EUIPO_CLIENT_ID and EUIPO_CLIENT_SECRET env vars")

_token_cache: Dict = {"token": None, "expires_at": 0}


def euipo_available() -> bool:
    return bool(EUIPO_CLIENT_ID and EUIPO_CLIENT_SECRET)


def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    resp = requests.post(EUIPO_TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     EUIPO_CLIENT_ID,
        "client_secret": EUIPO_CLIENT_SECRET,
        "scope":         "uid",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"]      = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    return _token_cache["token"]


def _to_internal(tm: dict) -> dict:
    app_num   = tm.get("applicationNumber", "")
    verbal    = (tm.get("wordMarkSpecification") or {}).get("verbalElement", "")
    applicants = tm.get("applicants") or []
    names     = [a.get("name", "") for a in applicants if a.get("name")]

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
        "markImageURI":     None,
        "goodAndServices":  [],
        "_source":          "euipo_api",
    }


def search_euipo(name: str, nice_classes: List[str]) -> List[Dict]:
    if not euipo_available():
        return []
    try:
        token = _get_token()
    except Exception as e:
        print(f"[EUIPO] Token error: {e}")
        return []

    headers = {
        "X-IBM-Client-Id": EUIPO_CLIENT_ID,
        "Authorization":   f"Bearer {token}",
        "Accept":          "application/json",
    }

    nc_filter = ""
    nc_ints = [str(int(c)) for c in nice_classes if c.isdigit()]
    if nc_ints:
        nc_filter = f";niceClasses=in=({','.join(nc_ints)})"

    upper = name.upper()
    queries = [
        f"wordMarkSpecification.verbalElement=={upper}",
        f"wordMarkSpecification.verbalElement==*{upper}*",
    ]

    seen: set = set()
    all_marks: List[Dict] = []

    for q in queries:
        try:
            resp = requests.get(EUIPO_SEARCH_URL, headers=headers,
                                params={"query": q + nc_filter, "size": 100, "page": 0},
                                timeout=15)
            if resp.status_code == 200:
                for tm in resp.json().get("trademarks") or []:
                    key = tm.get("applicationNumber", "")
                    if key and key not in seen:
                        seen.add(key)
                        all_marks.append(_to_internal(tm))
            else:
                print(f"[EUIPO] {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[EUIPO] Request error: {e}")

    print(f"[EUIPO] {len(all_marks)} marks for '{name}'")
    return all_marks
