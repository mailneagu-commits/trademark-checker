"""
Rotire automată a regiunii Railway când TMview blochează IP-ul containerului — același lucru
pe care îl făceam manual, până acum (schimbat railway.json, deploy, verificat, repetat).

TMview/Imperva blochează la nivel de IP, nu de cont — containerul de pe o altă regiune Railway
primește alt IP și de obicei scapă de blocaj. Cerem schimbarea regiunii prin API-ul Railway
(serviciul de pe un alt IP decât al TMview-ului blocat), care face singur un redeploy.

Necesită variabila de mediu RAILWAY_API_TOKEN (Railway → Account Settings → Tokens; un token de
proiect e read-only și nu merge). RAILWAY_SERVICE_ID și RAILWAY_ENVIRONMENT_ID sunt puse automat de Railway.
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

# ID-urile actuale (Railway Metal) — vechile nume GCP ("us-west1", "europe-west4" etc.) și câmpul
# `region` din serviceInstanceUpdate sunt depreciate: Railway întorcea succes dar NU muta serviciul.
# Acum regiunea se setează prin `multiRegionConfig` = {regiune: {"numReplicas": N} | null}.
REGION_CYCLE: List[str] = ["us-west2", "europe-west4-drams3a", "asia-southeast1-eqsg3a", "us-east4-eqdc4a"]
# Starea salvată din versiunea veche conține numele vechi — le traducem ca să continuăm ciclul.
_LEGACY_NAMES = {"us-west1": "us-west2", "europe-west4": "europe-west4-drams3a",
                 "asia-southeast1": "asia-southeast1-eqsg3a", "us-east4": "us-east4-eqdc4a"}

# Nu rotim mai des decât atât — un redeploy durează ~60-90s, iar un container proaspăt
# merită verificat cu căutări reale (nu doar primul ping) înainte să-l declarăm tot blocat.
COOLDOWN_SECONDS = 6 * 60
# Nu rotim în primele X secunde după un redeploy — containerul abia pornit încă nu a avut
# nicio căutare reală prin el; altfel am rotit la nesfârșit la fiecare pornire.
GRACE_AFTER_DEPLOY_SECONDS = 2 * 60
# Prima verificare TMview după pornire (scheduler.py) — imediat după perioada de grație, ca un
# container proaspăt mutat să afle repede dacă noua zonă merge (nu abia la următorul tick de 10 min).
STARTUP_PROBE_SECONDS = GRACE_AFTER_DEPLOY_SECONDS + 30
# Cât durează, aproximativ, de la cererea de rotire până când noul container răspunde.
REDEPLOY_SECONDS = 3 * 60

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


def _cycle_position() -> str:
    """Regiunea pe care o considerăm 'curentă' PENTRU ROTIRE — ultima pe care chiar am cerut-o
    noi (salvată în DB), nu ce raportează RAILWAY_REPLICA_REGION (cod intern Railway, ex. 'sfo'
    pentru us-west1 — nu se potrivește cu numele din REGION_CYCLE, deci nu poate fi folosit ca
    să alegem 'următoarea' regiune)."""
    state = _load_state()
    # O încercare eșuată nu ne-a mutat — rămânem unde eram ("from"), nu unde voiam să ajungem.
    to = state.get("to") if state.get("ok") else state.get("from")
    to = _LEGACY_NAMES.get(to, to)
    return to if to in REGION_CYCLE else REGION_CYCLE[-1]  # necunoscut → următoarea e REGION_CYCLE[0]


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


def _live_multi_region_config(token: str, service_id: str, env_id: str) -> Optional[Dict]:
    """multiRegionConfig-ul ultimului deploy, cum îl vede Railway (None dacă nu se poate citi)."""
    data = _graphql(
        """
        query($serviceId: String!, $environmentId: String!) {
          serviceInstance(serviceId: $serviceId, environmentId: $environmentId) {
            latestDeployment { status meta }
          }
        }
        """,
        {"serviceId": service_id, "environmentId": env_id},
        token,
    )
    dep = ((data.get("serviceInstance") or {}).get("latestDeployment") or {})
    meta = dep.get("meta") or {}
    cfg = ((meta.get("serviceManifest") or {}).get("deploy") or {}).get("multiRegionConfig")
    return {"deployment_status": dep.get("status"), "multiRegionConfig": cfg}


def _health_status() -> Dict:
    try:
        import scheduler
        return dict(scheduler._last_health)
    except Exception:
        return {}


def status() -> Dict:
    """Pentru diagnosticare: regiunea curentă, dacă rotirea e configurată, ultima încercare."""
    state = {}
    try:
        state = _load_state()
    except Exception:
        pass
    live = None
    if _token() and os.environ.get("RAILWAY_SERVICE_ID") and os.environ.get("RAILWAY_ENVIRONMENT_ID"):
        try:
            live = _live_multi_region_config(_token(), os.environ["RAILWAY_SERVICE_ID"],
                                             os.environ["RAILWAY_ENVIRONMENT_ID"])
        except Exception as e:
            live = {"error": str(e)[:300]}
    return {
        "current_region": current_region(),
        "railway_live": live,
        "last_call": dict(_last_call),
        "health": _health_status(),
        "configured": bool(_token()),
        "cycle": REGION_CYCLE,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "last": state,
    }


def _parse_utc(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def retry_eta() -> Dict:
    """Pentru cronometrul din frontend: în ce etapă e deblocarea TMview și când merită refăcută
    căutarea (retry_at, UTC). Estimări — ora exactă depinde de cât durează redeploy-ul Railway."""
    from datetime import timedelta
    now = datetime.utcnow()
    boot = datetime.utcfromtimestamp(_boot_time)
    health = _health_status()
    state: Dict = {}
    try:
        state = _load_state()
    except Exception:
        pass
    base = {"region": current_region(), "configured": bool(_token())}

    last_at = _parse_utc(state.get("at"))
    if state.get("ok") and last_at and last_at > boot:
        # Acest container a cerut deja mutarea — noul container (altă zonă) pornește curând.
        return {**base, "phase": "rotating", "to": state.get("to"),
                "retry_at": (last_at + timedelta(seconds=REDEPLOY_SECONDS)).isoformat()}

    checked_at = _parse_utc(health.get("at"))
    if checked_at is None or checked_at < boot:
        return {**base, "phase": "verifying",
                "retry_at": max(now, boot + timedelta(seconds=STARTUP_PROBE_SECONDS + 30)).isoformat()}

    if health.get("blocked"):
        # Următoarea verificare periodică (la fix :00, :10, …) + a doua probă (20s) + redeploy.
        nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=10 - now.minute % 10)
        res = {**base, "phase": "blocked", "reason": health.get("blocked"),
               "retry_at": (nxt + timedelta(seconds=20 + REDEPLOY_SECONDS)).isoformat()}
        if state.get("ok") is False and last_at and last_at > checked_at - timedelta(minutes=1):
            res["rotation_error"] = state.get("error", "")[:200]
        return res

    return {**base, "phase": "ok", "checked_at": health.get("at")}


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


_last_call: Dict = {}  # ultima apelare (inclusiv sărită/eșuată) — pentru /api/region-status


def rotate_region(reason: str) -> Dict:
    """Wrapper: înregistrează rezultatul oricărei apelări, inclusiv excepțiile neprevăzute."""
    try:
        res = _rotate_region(reason)
    except Exception as e:  # nu lăsăm excepția să dispară într-un executor
        print(f"[REGION-ROTATE] Excepție neprevăzută: {type(e).__name__}: {e}")
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _last_call.clear()
    _last_call.update({"at": datetime.utcnow().isoformat(), "reason": reason, **res})
    return res


def _rotate_region(reason: str) -> Dict:
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

        existing: Dict = {}
        try:
            existing = (_live_multi_region_config(token, service_id, env_id) or {}).get("multiRegionConfig") or {}
        except Exception as e:
            print(f"[REGION-ROTATE] Nu am putut citi configurația curentă: {e}")
        # Regiunea reală are prioritate față de cea salvată de noi: întâi cea a containerului care
        # rulează, apoi configurația din Railway (lipsește cât timp ultimul deploy e încă în build).
        live_regions = [r for r, v in existing.items() if v and r in REGION_CYCLE]
        if current_region() in REGION_CYCLE:
            cur = current_region()
        elif len(live_regions) == 1:
            cur = live_regions[0]
        else:
            cur = _cycle_position()
        idx = REGION_CYCLE.index(cur)
        next_region = REGION_CYCLE[(idx + 1) % len(REGION_CYCLE)]

        print(f"[REGION-ROTATE] {reason} — trec din {current_region() or cur} pe {next_region}")
        try:
            # Scoatem (null) toate regiunile existente și punem 1 replică pe cea nouă — altfel
            # Railway ar adăuga regiunea nouă lângă cea veche (2 replici, una tot pe IP-ul blocat).
            mrc: Dict = {r: None for r in REGION_CYCLE}
            mrc.update({r: None for r in existing})
            mrc[next_region] = {"numReplicas": 1}
            update_q = """
                mutation($serviceId: String!, $environmentId: String!, $input: ServiceInstanceUpdateInput!) {
                  serviceInstanceUpdate(serviceId: $serviceId, environmentId: $environmentId, input: $input)
                }
                """
            try:
                _graphql(update_q, {"serviceId": service_id, "environmentId": env_id,
                                    "input": {"multiRegionConfig": mrc}}, token)
            except Exception as e:
                # O regiune veche invalidă (ex. "sfo") poate face Railway să respingă tot obiectul —
                # reîncercăm doar cu regiunile valide din ciclu.
                print(f"[REGION-ROTATE] multiRegionConfig complet respins ({e}) — reîncerc doar cu regiunile din ciclu")
                mrc = {r: None for r in REGION_CYCLE}
                mrc[next_region] = {"numReplicas": 1}
                _graphql(update_q, {"serviceId": service_id, "environmentId": env_id,
                                    "input": {"multiRegionConfig": mrc}}, token)
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
            _save_state({"at": datetime.utcnow().isoformat(), "from": current_region() or cur, "to": next_region,
                        "reason": reason, "ok": False, "error": str(e)})
            return {"ok": False, "error": str(e)}

        _save_state({"at": datetime.utcnow().isoformat(), "from": current_region() or cur, "to": next_region,
                    "reason": reason, "ok": True})
        print(f"[REGION-ROTATE] Redeploy pornit spre {next_region} — aplicația repornește în ~1-2 min.")
        return {"ok": True, "from": cur, "to": next_region}
    finally:
        _rotating = False
