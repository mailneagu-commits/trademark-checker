import os
import time
import requests
from typing import List, Dict

EUIPO_CLIENT_ID     = os.environ.get("EUIPO_CLIENT_ID", "").strip()
EUIPO_CLIENT_SECRET = os.environ.get("EUIPO_CLIENT_SECRET", "").strip()
EUIPO_SEARCH_URL    = "https://api.euipo.europa.eu/trademark-search/trademarks"
EUIPO_TOKEN_URL     = "https://euipo.europa.eu/cas-server-webapp/oidc/accessToken"

if EUIPO_CLIENT_ID:
    print(f"[EUIPO] Configured: {EUIPO_CLIENT_ID[:8]}...")
else:
    print("[EUIPO] Not configured — set EUIPO_CLIENT_ID and EUIPO_CLIENT_SECRET")


def euipo_available() -> bool:
    return bool(EUIPO_CLIENT_ID and EUIPO_CLIENT_SECRET)


# API-ul EUIPO cere OAuth2 (client_credentials) pe lângă client id/secret —
# fără Bearer token, gateway-ul respinge cu 401 "cannot pass security checks".
_token_cache = {"access_token": None, "expires_at": 0.0}


def _get_access_token(force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh and _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    # cas-server-webapp (Apereo CAS) autentifică de obicei clientul confidențial
    # via HTTP Basic Auth (RFC 6749 §2.3.1), nu prin client_id/secret în body.
    # Încercăm Basic Auth întâi; dacă eșuează, revenim la varianta cu body.
    resp = requests.post(
        EUIPO_TOKEN_URL,
        auth=(EUIPO_CLIENT_ID, EUIPO_CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "uid"},
        timeout=15,
    )
    if resp.status_code != 200:
        resp_body = requests.post(
            EUIPO_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id":     EUIPO_CLIENT_ID,
                "client_secret": EUIPO_CLIENT_SECRET,
                "grant_type":    "client_credentials",
                "scope":         "uid",
            },
            timeout=15,
        )
        if resp_body.status_code == 200:
            resp = resp_body
        else:
            raise Exception(
                f"EUIPO OAuth token error — basic_auth={resp.status_code}:{resp.text[:150]} "
                f"| body_params={resp_body.status_code}:{resp_body.text[:150]}"
            )

    data  = resp.json()
    token = data["access_token"]
    _token_cache["access_token"] = token
    _token_cache["expires_at"]   = now + data.get("expires_in", 28800)
    return token


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

    def _headers(token: str) -> dict:
        return {
            "Authorization":   f"Bearer {token}",
            "X-IBM-Client-Id": EUIPO_CLIENT_ID,
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
    token = _get_access_token()

    for q in queries:
        params = {"query": q + nc_filter, "size": 100, "page": 0}
        resp = requests.get(EUIPO_SEARCH_URL, headers=_headers(token), params=params, timeout=15)
        if resp.status_code == 401:
            # Token expirat/invalid — reîmprospătăm o dată și reîncercăm.
            token = _get_access_token(force_refresh=True)
            resp = requests.get(EUIPO_SEARCH_URL, headers=_headers(token), params=params, timeout=15)

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
