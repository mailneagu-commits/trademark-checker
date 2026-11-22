# Parola ProWatch, ProMonitor și ProSearch

Caddy stă în fața fiecărei aplicații și cere utilizator + parolă (HTTP Basic Auth). Aplicația (uvicorn)
ascultă doar intern, pe portul 8001. Aceeași parolă pe ambele servicii Railway:

| Serviciu Railway | Domenii | Config | Fără parolă (healthcheck) |
|---|---|---|---|
| web | promonitor / prosearch.protectmark.ro, web-production-c898f.up.railway.app | deploy/ | /api/session-status |
| prowatch | prowatch.protectmark.ro, prowatch-production-a625.up.railway.app | registru/deploy/ | /api/health |

Setări în panoul Railway (NU în fișiere — railway.json / config-as-code nu mai e citit din 1.12.2026):
- Settings → Deploy → Custom Start Command = `bash deploy/start-web.sh` (pe AMBELE servicii).
  ATENȚIE: dacă acest câmp se schimbă / se golește, aplicația pornește FĂRĂ Caddy, deci fără parolă.
- Healthcheck Path: web `/api/session-status`, prowatch `/api/health`.
- Builder Railpack (railpack.json instalează caddy; nixpacks.toml rămâne pentru Nixpacks); Python din .python-version (3.11).

- Utilizator: `protectmark`. Parola NU e în git — doar hash-ul ei bcrypt, în variabila Railway
  `BASIC_AUTH_HASH` (pe ambele servicii → Variables).
- Excepție fără parolă: doar calea de healthcheck din tabel (fără date).
- Apeluri între aplicații pe rețeaua privată (nu trec prin parolă):
  prowatch: `MONITOR_API = http://web.railway.internal:8001`; web: `PROWATCH_API = http://prowatch.railway.internal:8001`.
- Blocate (403): .env, .git, .htpasswd, *.sql, *.bak, *.log, *.zip, *.old, *.swp, *.ini, *.cfg, *.conf, *.yml, *.toml.
- Antete: HSTS 1 an, X-Robots-Tag noindex/nofollow/noarchive, X-Frame-Options SAMEORIGIN
  (ProMonitor își încarcă tabelul zilnic într-un iframe propriu), nosniff, Referrer-Policy same-origin.

## 1. Schimbarea parolei
Pe Mac, în Terminal (cere parola nouă fără s-o afișeze și scrie hash-ul):

    python3 -c "import bcrypt,getpass; print(bcrypt.hashpw(getpass.getpass('Parola noua: ').encode(), bcrypt.gensalt(12)).decode())"

Apoi Railway → serviciile „web” ȘI „prowatch” → Variables → `BASIC_AUTH_HASH` = hash-ul afișat → Deploy.

## 2. Dezactivare de urgență
Railway → serviciul dorit („web” sau „prowatch”) → Variables → ștergeți `BASIC_AUTH_HASH` → redeploy automat →
site-ul merge FĂRĂ parolă (în log apare „[AUTH] ATENȚIE: BASIC_AUTH_HASH nesetat”).
Reactivare: puneți la loc variabila.

## 3. Al doilea utilizator
În `deploy/auth_on` și `registru/deploy/auth_on` adăugați o linie sub cea existentă:

    ana {$BASIC_AUTH_HASH_ANA}

apoi variabila Railway `BASIC_AUTH_HASH_ANA` (hash generat ca la punctul 1) și push în git.
