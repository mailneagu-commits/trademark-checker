#!/usr/bin/env bash
# Pornire serviciul ProWatch: uvicorn intern pe 8001 + Caddy (parolă) pe $PORT.
set -uo pipefail
cd "$(dirname "$0")/.."
APP_PORT=8001
if [ -f /proc/net/if_inet6 ]; then
  APP_HOST="::"; export UPSTREAM="[::1]:$APP_PORT"      # „::” — și rețeaua privată Railway (IPv6)
else
  APP_HOST="0.0.0.0"; export UPSTREAM="127.0.0.1:$APP_PORT"
fi
if [ -n "${BASIC_AUTH_HASH:-}" ]; then export AUTH_MODE=on; else export AUTH_MODE=off
  echo "[AUTH] ATENȚIE: BASIC_AUTH_HASH nesetat — site-ul rulează FĂRĂ parolă"; fi
uvicorn app:app --host "$APP_HOST" --port "$APP_PORT" &
APP_PID=$!
cd deploy && caddy run --config Caddyfile --adapter caddyfile &
CADDY_PID=$!
wait -n
echo "[AUTH] un proces s-a oprit — repornesc containerul"
kill "$APP_PID" "$CADDY_PID" 2>/dev/null
exit 1
