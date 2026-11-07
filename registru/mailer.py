"""
Notificare pe email la fiecare solicitare primită pe site. Railway blochează conexiunile SMTP
(porturile 25/465/587) pe planurile Hobby, așa că trimiterea merge prin API-ul HTTP al unui
serviciu de email, după variabila setată în Railway:

  BREVO_API_KEY   — Brevo (brevo.com), plan gratuit 300 emailuri/zi
  RESEND_API_KEY  — Resend (resend.com), plan gratuit 100 emailuri/zi
  SMTP_*          — SMTP clasic (doar pe planurile Railway care permit SMTP)

MAIL_FROM = expeditorul (implicit SMTP_USER / noreply@protectmark.ro), LEAD_EMAIL = destinatarul.
Fără niciuna, solicitările se salvează doar în baza de date.
"""
import os
import smtplib
import threading
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

LEAD_EMAIL = os.environ.get("LEAD_EMAIL", "mailneagu@gmail.com")
# Rezultatul ultimei trimiteri — pentru /api/mail-status (fără parole).
last: dict = {"ok_at": None, "error": None, "error_at": None}


def _env(name: str, default: str = "") -> str:
    # Valorile lipite în Railway pot avea spații / rânduri noi la capete.
    return (os.environ.get(name) or "").strip() or default


def settings() -> dict:
    user = _env("SMTP_USER")
    domain = user.split("@")[-1] if "@" in user else ""
    # Serverul de email al găzduirii cyberfolks pentru protectmark.ro (mail.protectmark.ro duce la
    # serverul web, unde portul 465 nu răspunde din Railway).
    default_host = ("cloud341.c-f.ro" if domain == "protectmark.ro" else
                    f"mail.{domain}" if domain else "smtp.gmail.com")
    return {"host": _env("SMTP_HOST") or _env("SMTP_SERVER") or default_host,
            "port": int(_env("SMTP_PORT", "587") or 587), "user": user}


def provider() -> str:
    if _env("BREVO_API_KEY"):
        return "brevo"
    if _env("RESEND_API_KEY"):
        return "resend"
    if _env("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        return "smtp"
    return ""


def available() -> bool:
    return bool(provider())


def _sender() -> str:
    return _env("MAIL_FROM") or _env("SMTP_FROM") or _env("SMTP_USER") or "noreply@protectmark.ro"


def _send_api(subject: str, html: str, reply_to: str = "") -> None:
    import requests
    sender = _sender()
    if provider() == "brevo":
        body = {"sender": {"name": "ProWatch", "email": sender}, "to": [{"email": LEAD_EMAIL}],
                "subject": subject, "htmlContent": html}
        if reply_to:
            body["replyTo"] = {"email": reply_to}
        r = requests.post("https://api.brevo.com/v3/smtp/email", json=body, timeout=30,
                          headers={"api-key": _env("BREVO_API_KEY"), "accept": "application/json"})
    else:
        body = {"from": f"ProWatch <{sender}>", "to": [LEAD_EMAIL], "subject": subject, "html": html}
        if reply_to:
            body["reply_to"] = reply_to
        r = requests.post("https://api.resend.com/emails", json=body, timeout=30,
                          headers={"Authorization": f"Bearer {_env('RESEND_API_KEY')}"})
    if r.status_code >= 300:
        raise RuntimeError(f"{provider()} HTTP {r.status_code}: {r.text[:300]}")


def _send(subject: str, html: str, reply_to: str = "") -> None:
    if provider() in ("brevo", "resend"):
        _send_api(subject, html, reply_to)
        return
    cfg = settings()
    host, port, user = cfg["host"], cfg["port"], cfg["user"]
    password = os.environ["SMTP_PASSWORD"].strip("\r\n")
    sender = _env("SMTP_FROM", user)
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("ProWatch", sender))
    msg["To"] = LEAD_EMAIL
    if reply_to:
        msg["Reply-To"] = reply_to
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.sendmail(sender, [LEAD_EMAIL], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.sendmail(sender, [LEAD_EMAIL], msg.as_string())


def notify_lead(kind: str, name: str, email: str, phone: str, message: str, marks: list) -> None:
    """Trimite notificarea în fundal — vizitatorul nu așteaptă serverul de email."""
    if not available():
        return
    title = "Solicitare cercetare de disponibilitate" if kind == "cercetare" else "Solicitare informații despre mărci"
    rows = [("Nume", name), ("Email", email), ("Telefon", phone or "—"),
            ("Mărci selectate", ", ".join(marks) or "—"), ("Mesaj", message or "—")]
    html = (f"<h2 style='font-family:Arial;color:#2b2a28'>{escape(title)}</h2>"
            "<table style='font-family:Arial;font-size:14px;border-collapse:collapse'>"
            + "".join(f"<tr><td style='padding:6px 12px 6px 0;color:#6b6966;vertical-align:top'><b>{k}</b></td>"
                      f"<td style='padding:6px 0;white-space:pre-wrap'>{escape(str(v))}</td></tr>" for k, v in rows)
            + "</table><p style='font-family:Arial;font-size:12px;color:#999'>Trimis de pe www.prowatch.ro</p>")

    def run():
        from datetime import datetime
        try:
            _send(f"[ProWatch] {title} — {name}", html, reply_to=email)
            last.update(ok_at=datetime.utcnow().isoformat() + "Z", error=None)
        except Exception as e:
            last.update(error=f"{type(e).__name__}: {e}", error_at=datetime.utcnow().isoformat() + "Z")
            print(f"[mail] Eroare la trimiterea notificării: {e}")
    threading.Thread(target=run, daemon=True).start()


def keepalive() -> None:
    """Brevo dezactivează o cheie API nefolosită 90 de zile. O citire săptămânală a statisticilor
    de trimitere (permisă de dreptul „Transactional email”) o ține activă, fără să trimită nimic."""
    if provider() != "brevo":
        return
    import requests
    try:
        r = requests.get("https://api.brevo.com/v3/smtp/statistics/aggregatedReport", timeout=30,
                         headers={"api-key": _env("BREVO_API_KEY"), "accept": "application/json"})
        print(f"[mail] Brevo keepalive: HTTP {r.status_code}")
    except Exception as e:
        print(f"[mail] Brevo keepalive: {type(e).__name__}: {e}")
