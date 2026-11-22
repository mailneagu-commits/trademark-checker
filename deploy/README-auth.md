# Parola ProMonitor / ProSearch (serviciul Railway „web”)

Caddy (deploy/Caddyfile) stă în fața aplicației și cere utilizator + parolă (HTTP Basic Auth) pe
promonitor.protectmark.ro, prosearch.protectmark.ro și web-production-c898f.up.railway.app.
Aplicația (uvicorn) ascultă doar intern, pe portul 8001.

- Utilizator: `protectmark`. Parola NU e în git — doar hash-ul ei bcrypt, în variabila Railway
  `BASIC_AUTH_HASH` (serviciul „web” → Variables).
- Excepție fără parolă: doar `/api/session-status` (verificarea de sănătate Railway, fără date).
- ProWatch apelează aplicația pe rețeaua privată: `MONITOR_API = http://web.railway.internal:8001`
  (variabila serviciului ProWatch) — nu trece prin parolă.
- Blocate (403): .env, .git, .htpasswd, *.sql, *.bak, *.log, *.zip, *.old, *.swp, *.ini, *.cfg, *.conf, *.yml, *.toml.
- Antete: HSTS 1 an, X-Robots-Tag noindex/nofollow/noarchive, X-Frame-Options SAMEORIGIN
  (ProMonitor își încarcă tabelul zilnic într-un iframe propriu), nosniff, Referrer-Policy same-origin.

## 1. Schimbarea parolei
Pe Mac, în Terminal (cere parola nouă fără s-o afișeze și scrie hash-ul):

    python3 -c "import bcrypt,getpass; print(bcrypt.hashpw(getpass.getpass('Parola noua: ').encode(), bcrypt.gensalt(12)).decode())"

Apoi Railway → serviciul „web” → Variables → `BASIC_AUTH_HASH` = hash-ul afișat → Railway redeployează singur.

## 2. Dezactivare de urgență
Railway → serviciul „web” → Variables → ștergeți `BASIC_AUTH_HASH` → redeploy automat →
site-ul merge FĂRĂ parolă (în log apare „[AUTH] ATENȚIE: BASIC_AUTH_HASH nesetat”).
Reactivare: puneți la loc variabila.

## 3. Al doilea utilizator
În `deploy/auth_on` adăugați o linie sub cea existentă:

    ana {$BASIC_AUTH_HASH_ANA}

apoi variabila Railway `BASIC_AUTH_HASH_ANA` (hash generat ca la punctul 1) și push în git.
