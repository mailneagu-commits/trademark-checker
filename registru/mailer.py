"""
Notificare pe email la fiecare solicitare primită pe site. Se configurează din variabilele
de mediu (Railway): SMTP_HOST, SMTP_PORT (465 = SSL, 587 = STARTTLS), SMTP_USER,
SMTP_PASSWORD, SMTP_FROM (implicit SMTP_USER) și LEAD_EMAIL (destinatarul).
Fără SMTP_USER / SMTP_PASSWORD solicitările se salvează doar în baza de date.
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


def available() -> bool:
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def _send(subject: str, html: str, reply_to: str = "") -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("SMTP_FROM", user)
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
