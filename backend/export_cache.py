"""Cache persistent (în baza de date) pentru ce aduce exportul de la servicii externe:
detaliile TMview ale mărcilor, traducerile Google și imaginile. Un export repetat al aceleiași
căutări (Word, apoi PDF, apoi Excel) nu mai refăcea nimic din memorie după o repornire —
acum le ia de aici. Orice eroare de DB e tratată ca „nu e în cache”: exportul merge oricum."""
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional, Tuple

from db import SessionLocal, engine


def _upsert(model, rows: list, key: str) -> None:
    """INSERT … ON CONFLICT DO UPDATE — pagina (/api/tm-detail) și pregătirea exportului din fundal
    pot salva aceeași marcă în același timp; un INSERT simplu ar da conflict și ar pierde tot lotul."""
    if not rows:
        return
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(model.__table__).values(rows)
    cols = {c: stmt.excluded[c] for c in rows[0] if c != key}
    stmt = stmt.on_conflict_do_update(index_elements=[key], set_=cols) if cols else stmt.on_conflict_do_nothing()
    with engine.begin() as conn:
        conn.execute(stmt)

DETAIL_TTL = timedelta(days=7)     # detaliile (status, reprezentant…) se pot schimba
# Crește la orice schimbare în felul în care se construiește un detaliu (ex. țara titularului) —
# detaliile salvate cu o versiune mai veche sunt tratate ca lipsă și aduse din nou.
DETAIL_VERSION = 2
IMAGE_TTL  = timedelta(days=180)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Detalii TMview ────────────────────────────────────────────────────────────

def get_details(st13s: Iterable[str]) -> Dict[str, dict]:
    from monitor_models import DetailCache
    keys = [s for s in set(st13s) if s]
    if not keys:
        return {}
    db = SessionLocal()
    try:
        since = datetime.utcnow() - DETAIL_TTL
        rows = db.query(DetailCache).filter(DetailCache.st13.in_(keys), DetailCache.fetched_at >= since).all()
        return {r.st13: r.data for r in rows if r.data and r.data.get("_cv") == DETAIL_VERSION}
    except Exception as e:
        print(f"[EXPORT-CACHE] get_details: {e}")
        return {}
    finally:
        db.close()


def save_details(details: Dict[str, dict]) -> None:
    from monitor_models import DetailCache
    if not details:
        return
    try:
        now = datetime.utcnow()
        _upsert(DetailCache, [{"st13": k, "data": {**v, "_cv": DETAIL_VERSION}, "fetched_at": now}
                              for k, v in details.items()], "st13")
        # curățăm detaliile expirate, ca tabelul să nu crească la nesfârșit
        with engine.begin() as conn:
            conn.execute(DetailCache.__table__.delete().where(DetailCache.fetched_at < now - DETAIL_TTL))
    except Exception as e:
        print(f"[EXPORT-CACHE] save_details: {e}")


# ── Traduceri ─────────────────────────────────────────────────────────────────

def get_translations(texts: Iterable[str]) -> Dict[str, str]:
    from monitor_models import TranslationCache
    by_hash = {_text_hash(t): t for t in set(texts) if t}
    if not by_hash:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(TranslationCache).filter(TranslationCache.text_hash.in_(list(by_hash))).all()
        return {by_hash[r.text_hash]: r.translated for r in rows}
    except Exception as e:
        print(f"[EXPORT-CACHE] get_translations: {e}")
        return {}
    finally:
        db.close()


def save_translations(pairs: Dict[str, str]) -> None:
    from monitor_models import TranslationCache
    if not pairs:
        return
    try:
        now = datetime.utcnow()
        _upsert(TranslationCache, [{"text_hash": _text_hash(t), "translated": tr, "saved_at": now}
                                   for t, tr in pairs.items()], "text_hash")
    except Exception as e:
        print(f"[EXPORT-CACHE] save_translations: {e}")


# ── Imagini ───────────────────────────────────────────────────────────────────

def get_image(url: str) -> Optional[Tuple[bytes, str]]:
    from monitor_models import ImageCache
    db = SessionLocal()
    try:
        row = db.get(ImageCache, url)
        if row and row.saved_at >= datetime.utcnow() - IMAGE_TTL:
            return row.data, row.content_type or ""
        return None
    except Exception as e:
        print(f"[EXPORT-CACHE] get_image: {e}")
        return None
    finally:
        db.close()


def save_image(url: str, data: bytes, content_type: str) -> None:
    from monitor_models import ImageCache
    try:
        _upsert(ImageCache, [{"url": url, "data": data, "content_type": content_type,
                              "saved_at": datetime.utcnow()}], "url")
    except Exception as e:
        print(f"[EXPORT-CACHE] save_image: {e}")
