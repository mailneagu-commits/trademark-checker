"""
Monitoring service: runs similarity search for each WatchItem,
detects new trademarks, and sends email alerts.
"""
import asyncio
import os
import smtplib
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict

from sqlalchemy.orm import Session

from agents.similarity_agent import SimilarityAgent

SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM     = os.environ.get("SMTP_FROM", SMTP_USER)

_similarity = SimilarityAgent(
    threshold_very_high=90.0,
    threshold_high=75.0,
    threshold_medium=60.0,
    threshold_small=35.0,
)


def _email_available() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD)


def _send_email(to: str, subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(SMTP_FROM, [to], msg.as_string())


def _build_html(watch_item, new_conflicts: List[Dict], new_similar: List[Dict]) -> str:
    def rows(marks, level_label, color):
        if not marks:
            return ""
        header = f"""
        <h3 style="color:{color};margin-top:24px">{level_label} ({len(marks)})</h3>
        <table style="border-collapse:collapse;width:100%;font-size:13px">
          <thead>
            <tr style="background:#f0f0f0">
              <th style="padding:6px 10px;text-align:left;border:1px solid #ddd">Marcă</th>
              <th style="padding:6px 10px;text-align:left;border:1px solid #ddd">Oficiu</th>
              <th style="padding:6px 10px;text-align:left;border:1px solid #ddd">Titular</th>
              <th style="padding:6px 10px;text-align:left;border:1px solid #ddd">Clase</th>
              <th style="padding:6px 10px;text-align:left;border:1px solid #ddd">Dată depunere</th>
              <th style="padding:6px 10px;text-align:left;border:1px solid #ddd">Status</th>
              <th style="padding:6px 10px;text-align:left;border:1px solid #ddd">Similaritate</th>
            </tr>
          </thead><tbody>"""
        body_rows = ""
        for m in marks:
            app_date = (m.get("applicationDate") or "")[:10]
            classes  = ", ".join(str(c) for c in (m.get("niceClass") or []))
            holder   = ", ".join(m.get("applicantName") or [])
            score    = m.get("_score", "")
            score_str = f"{score:.0f}%" if isinstance(score, float) else ""
            body_rows += f"""
            <tr>
              <td style="padding:5px 10px;border:1px solid #ddd"><strong>{m.get("tmName","")}</strong></td>
              <td style="padding:5px 10px;border:1px solid #ddd">{m.get("tmOffice","")}</td>
              <td style="padding:5px 10px;border:1px solid #ddd">{holder}</td>
              <td style="padding:5px 10px;border:1px solid #ddd">{classes}</td>
              <td style="padding:5px 10px;border:1px solid #ddd">{app_date}</td>
              <td style="padding:5px 10px;border:1px solid #ddd">{m.get("tradeMarkStatus","")}</td>
              <td style="padding:5px 10px;border:1px solid #ddd;color:{color}">{score_str}</td>
            </tr>"""
        return header + body_rows + "</tbody></table>"

    total = len(new_conflicts) + len(new_similar)
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:900px;margin:0 auto">
      <div style="background:#1a3c5e;color:white;padding:20px 24px;border-radius:6px 6px 0 0">
        <h2 style="margin:0">Alertă monitorizare mărci</h2>
        <p style="margin:6px 0 0;opacity:.85">
          {total} marcă/mărci noi detectate pentru <strong>{watch_item.trademark_name}</strong>
          {f'(titular: {watch_item.holder_name})' if watch_item.holder_name else ''}
        </p>
      </div>
      <div style="padding:20px 24px;border:1px solid #ddd;border-top:none;border-radius:0 0 6px 6px">
        <p><strong>Marcă monitorizată:</strong> {watch_item.trademark_name}</p>
        <p><strong>Clase NICE:</strong> {", ".join(watch_item.nice_classes or [])}</p>
        <p><strong>Teritorii:</strong> {", ".join(watch_item.offices or [])}</p>
        <p><strong>Data verificării:</strong> {datetime.utcnow().strftime("%d.%m.%Y %H:%M")} UTC</p>
        {rows(new_conflicts, "Conflicte (risc ridicat)", "#c0392b")}
        {rows(new_similar,   "Similare (risc mediu)",    "#e67e22")}
        <hr style="margin-top:32px;border:none;border-top:1px solid #eee">
        <p style="font-size:11px;color:#999">
          Generat automat de Trademark Checker Monitor.
          Pentru a dezactiva alertele pentru această marcă, accesați dashboard-ul aplicației.
        </p>
      </div>
    </div>"""
    return html


def _filter_by_nice(marks: List[Dict], nice_classes: List[str]) -> List[Dict]:
    """Păstrează doar mărcile cu cel puțin o clasă NICE comună."""
    if not nice_classes:
        return marks
    nc_ints = {int(c) for c in nice_classes if str(c).isdigit()}
    filtered = []
    for m in marks:
        tm_nc = set()
        for c in (m.get("niceClass") or []):
            try:
                tm_nc.add(int(c))
            except (TypeError, ValueError):
                pass
        if not tm_nc or tm_nc & nc_ints:
            filtered.append(m)
    return filtered


async def run_watch_item(watch_item, db: Session) -> Dict:
    """
    Runs a full similarity check for a single WatchItem.
    Sources: TMview API + EUIPO API + OSIM BOPI bulletin + EUIPO bulletin.
    Returns a summary dict.
    """
    from agents.search_agent import SearchAgent
    from agents.euipo_agent import search_euipo, euipo_available
    from monitor_models import SeenTrademark, AlertLog
    from scrapers.osim_bulletin import fetch_latest_osim
    from scrapers.euipo_bulletin import fetch_latest_euipo

    print(f"[MONITOR] Running check for '{watch_item.trademark_name}' (id={watch_item.id})")

    search_agent = SearchAgent()
    name         = watch_item.trademark_name
    classes      = watch_item.nice_classes or []
    offices      = watch_item.offices or ["RO", "EM"]

    # ── Sursă 1: TMview API ──────────────────────────────────────────────
    try:
        tmview_marks, _ = await search_agent.search(name, classes, offices)
    except Exception as e:
        print(f"[MONITOR] TMview error: {e}")
        tmview_marks = []

    # ── Sursă 2: EUIPO API direct ────────────────────────────────────────
    euipo_marks = []
    if euipo_available():
        try:
            euipo_marks = search_euipo(name, classes)
        except Exception as e:
            print(f"[MONITOR] EUIPO error: {e}")

    # ── Sursă 3: OSIM BOPI bulletin (dacă teritoriul RO e monitorizat) ──
    osim_bulletin_marks = []
    uses_ro = any(o.upper() in ("RO", "EU") for o in offices)
    if uses_ro:
        try:
            loop = asyncio.get_event_loop()
            raw_osim = await loop.run_in_executor(None, fetch_latest_osim)
            # Filtrăm după clase și după similaritate cu numele
            osim_bulletin_marks = _filter_by_nice(raw_osim, classes)
            print(f"[MONITOR] OSIM bulletin: {len(osim_bulletin_marks)} marks after class filter")
        except Exception as e:
            print(f"[MONITOR] OSIM bulletin error: {e}")

    # ── Sursă 4: EUIPO bulletin (dacă EM e monitorizat) ─────────────────
    euipo_bulletin_marks = []
    uses_em = any(o.upper() in ("EM", "EU") for o in offices)
    if uses_em:
        try:
            loop = asyncio.get_event_loop()
            raw_euipo = await loop.run_in_executor(None, fetch_latest_euipo)
            euipo_bulletin_marks = _filter_by_nice(raw_euipo, classes)
            print(f"[MONITOR] EUIPO bulletin: {len(euipo_bulletin_marks)} marks after class filter")
        except Exception as e:
            print(f"[MONITOR] EUIPO bulletin error: {e}")

    # Deduplicăm după ST13 (buletinele pot conține mărci deja în TMview)
    seen_st13_merge: set = set()
    all_marks: List[Dict] = []
    for m in tmview_marks + euipo_marks + osim_bulletin_marks + euipo_bulletin_marks:
        key = m.get("ST13") or m.get("applicationNumber") or ""
        if key and key in seen_st13_merge:
            continue
        if key:
            seen_st13_merge.add(key)
        all_marks.append(m)

    print(f"[MONITOR] Total unique marks to analyze: {len(all_marks)} "
          f"(tmview={len(tmview_marks)}, euipo={len(euipo_marks)}, "
          f"osim_buletin={len(osim_bulletin_marks)}, euipo_buletin={len(euipo_bulletin_marks)})")

    # --- similarity ---
    analysis     = _similarity.analyze(name, all_marks, classes, offices)
    conflicts    = analysis.get("conflicts", [])
    similar      = analysis.get("similar", [])

    # --- find new (not already seen) ---
    seen_st13s = {
        row.st13
        for row in db.query(SeenTrademark).filter(
            SeenTrademark.watch_item_id == watch_item.id
        ).all()
    }

    def _st13(m):
        return m.get("ST13") or m.get("applicationNumber") or ""

    new_conflicts = [m for m in conflicts if _st13(m) and _st13(m) not in seen_st13s]
    new_similar   = [m for m in similar   if _st13(m) and _st13(m) not in seen_st13s]

    # --- persist seen ---
    for m, level in [(m, "conflict") for m in new_conflicts] + [(m, "similar") for m in new_similar]:
        st13 = _st13(m)
        if not st13:
            continue
        db.add(SeenTrademark(
            watch_item_id    = watch_item.id,
            st13             = st13,
            tm_name          = m.get("tmName", ""),
            tm_office        = m.get("tmOffice", ""),
            similarity_level = level,
            application_date = (m.get("applicationDate") or "")[:10],
        ))

    # --- update last_checked_at ---
    watch_item.last_checked_at = datetime.utcnow()
    db.commit()

    # --- send email if new marks ---
    status    = "skipped"
    error_msg = ""
    total_new = len(new_conflicts) + len(new_similar)

    if total_new > 0:
        if _email_available():
            try:
                html    = _build_html(watch_item, new_conflicts, new_similar)
                subject = (
                    f"[Trademark Monitor] {total_new} marcă/mărci noi pentru '{name}'"
                )
                _send_email(watch_item.notification_email, subject, html)
                status = "sent"
                print(f"[MONITOR] Email sent to {watch_item.notification_email} — {total_new} new marks")
            except Exception as e:
                status    = "error"
                error_msg = traceback.format_exc()
                print(f"[MONITOR] Email error: {e}")
        else:
            status = "no_smtp"
            print("[MONITOR] SMTP not configured — skipping email")

    db.add(AlertLog(
        watch_item_id = watch_item.id,
        num_new_marks = total_new,
        email_to      = watch_item.notification_email,
        status        = status,
        error_msg     = error_msg,
    ))
    db.commit()

    return {
        "watch_item_id": watch_item.id,
        "trademark_name": name,
        "total_found": len(all_marks),
        "new_conflicts": len(new_conflicts),
        "new_similar": len(new_similar),
        "email_status": status,
        "new_conflict_marks": new_conflicts,
        "new_similar_marks": new_similar,
    }
