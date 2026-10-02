"""
Rotire automată a regiunii Railway când TMview blochează IP-ul containerului — același lucru
pe care îl făceam manual, până acum (schimbat railway.json, deploy, verificat, repetat).

TMview/Imperva blochează la nivel de IP, nu de cont — containerul de pe o altă regiune Railway
primește alt IP și de obicei scapă de blocaj. Cerem schimbarea regiunii prin API-ul Railway
(serviciul de pe un alt IP decât al TMview-ului blocat), care face singur un redeploy.

Necesită variabila de mediu RAILWAY_API_TOKEN (Railway → Account Settings → Tokens, sau un
token de proiect). RAILWAY_SERVICE_ID și RAILWAY_ENVIRONMENT_ID sunt puse automat de Railway.
Fără RAILWAY_API_TOKEN, rotirea e dezactivată — se loghează o singură dată motivul, apoi aplicația
continuă normal (poate fi rotit manual, ca înainte).
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"

# Aceleași regiuni folosite și la rotirea manuală, în ordinea încercată de obicei.
REGION_CYCLE: List[str] = ["us-west1", "europe-west4", "asia-southeast1", "us-east4"]

# Nu rotim mai des decât atât — un redeploy durează ~60-90s, iar un container proaspăt
# merită verificat cu căutări reale (nu doar primul ping) înainte să-l declarăm tot blocat.
COOLDOWN_SECONDS = 6 * 60
# Nu rotim în primele X secunde după un redeploy — containerul abia pornit încă nu a avut
# nicio căutare reală prin el; altfel am rotit la nesfârșit la fiecare pornire.
GRACE_AFTER_DEPLOY_SECONDS = 2 * 60

_STATE_KEY = "region_rotation"
_warned_no_token = False


def current_region() -> str:
    return os.environ.get("RAILWAY_REPLICA_REGION", "")


def _token() -> str:
    return os.environ.get("RAILWAY_API_TOKEN", "").strip()


def _graphql(query: str, variables: Dict, token: str) -> Dict:
    r = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=20,
    )
    data = r.json()
    if r.status_code != 200 or data.get("errors"):
        raise RuntimeError(f"Railway API error (HTTP {r.status_code}): {data.get('errors') or data}")
    return data["data"]


def _load_state() -> Dict:
    from db import SessionLocal
    from monitor_models import AppState
    db = SessionLocal()
    try:
        row = db.get(AppState, _STATE_KEY)
        return dict(row.value) if row and row.value else {}
    finally:
        db.close()


def _save_state(state: Dict) -> None:
    from db import SessionLocal
    from monitor_models import AppState
    db = SessionLocal()
    try:
        row = db.get(AppState, _STATE_KEY)
        if row is None:
            row = AppState(key=_STATE_KEY)
            db.add(row)
        row.value = state
        row.updated_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        print(f"[REGION-ROTATE] Nu am putut salva starea (DB indisponibilă?): {e}")
    finally:
        db.close()


def status() -> Dict:
    """Pentru diagnosticare: regiunea curentă, dacă rotirea e configurată, ultima încercare."""
    state = {}
    try:
        state = _load_state()
    except Exception:
        pass
    return {
        "current_region": current_region(),
        "configured": bool(_token()),
        "cycle": REGION_CYCLE,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "last": state,
    }


def _can_rotate_now() -> Optional[str]:
    """None dacă se poate roti acum, altfel motivul pentru care nu."""
    if not _token():
        return "RAILWAY_API_TOKEN nesetat"
    boot = globals().get("_boot_time", time.time())
    if time.time() - boot < GRACE_AFTER_DEPLOY_SECONDS:
        return "container pornit recent — las timp de verificare înainte să rotesc din nou"
    state = _load_state()
    last_at = state.get("at")
    if last_at:
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(last_at)).total_seconds()
            if age < COOLDOWN_SECONDS:
                return f"rotire recentă acum {int(age)}s — cooldown {COOLDOWN_SECONDS}s"
        except ValueError:
            pass
    return None


_boot_time = time.time()
_rotating = False  # evită rotiri suprapuse (declanșate de mai multe căutări simultane)


def rotate_region(reason: str) -> Dict:
    """Cere Railway să mute serviciul pe următoarea regiune din listă și să redeployeze.
    Nu aruncă excepții — un eșec aici nu trebuie să strice căutarea care l-a declanșat."""
    global _rotating, _warned_no_token

    token = _token()
    if not token:
        if not _warned_no_token:
            print("[REGION-ROTATE] RAILWAY_API_TOKEN nesetat — rotirea automată e dezactivată "
                  "(trebuie rotit manual regiunea în railway.json, ca înainte).")
            _warned_no_token = True
        return {"ok": False, "error": "RAILWAY_API_TOKEN nesetat"}

    blocker = _can_rotate_now()
    if blocker:
        print(f"[REGION-ROTATE] Sar peste rotire ({reason}): {blocker}")
        return {"ok": False, "skipped": blocker}

    if _rotating:
        return {"ok": False, "skipped": "rotire deja în curs"}
    _rotating = True
    try:
        service_id = os.environ.get("RAILWAY_SERVICE_ID", "")
        env_id     = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
        if not service_id or not env_id:
            print("[REGION-ROTATE] RAILWAY_SERVICE_ID/RAILWAY_ENVIRONMENT_ID lipsă — "
                  "nu rulăm pe Railway sau variabilele nu sunt încă disponibile.")
            return {"ok": False, "error": "service/environment id lipsă"}

        cur = current_region()
        try:
            idx = REGION_CYCLE.index(cur)
        except ValueError:
            idx = -1
        next_region = REGION_CYCLE[(idx + 1) % len(REGION_CYCLE)]
        if next_region == cur and len(REGION_CYCLE) > 1:
            next_region = REGION_CYCLE[(idx + 2) % len(REGION_CYCLE)]

        print(f"[REGION-ROTATE] {reason} — trec din {cur or '?'} pe {next_region}")
        try:
            _graphql(
                """
                mutation($serviceId: String!, $environmentId: String!, $input: ServiceInstanceUpdateInput!) {
                  serviceInstanceUpdate(serviceId: $serviceId, environmentId: $environmentId, input: $input)
                }
                """,
                {"serviceId": service_id, "environmentId": env_id, "input": {"region": next_region}},
                token,
            )
            _graphql(
                """
                mutation($serviceId: String!, $environmentId: String!) {
                  serviceInstanceDeploy(serviceId: $serviceId, environmentId: $environmentId)
                }
                """,
                {"serviceId": service_id, "environmentId": env_id},
                token,
            )
        except Exception as e:
            print(f"[REGION-ROTATE] Eroare API Railway: {e}")
            _save_state({"at": datetime.utcnow().isoformat(), "from": cur, "to": next_region,
                        "reason": reason, "ok": False, "error": str(e)})
            return {"ok": False, "error": str(e)}

        _save_state({"at": datetime.utcnow().isoformat(), "from": cur, "to": next_region,
                    "reason": reason, "ok": True})
        print(f"[REGION-ROTATE] Redeploy pornit spre {next_region} — aplicația repornește în ~1-2 min.")
        return {"ok": True, "from": cur, "to": next_region}
    finally:
        _rotating = False
