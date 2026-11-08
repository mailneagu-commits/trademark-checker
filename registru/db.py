"""
Baza de date a registrului public — PostgreSQL pe Railway (DATABASE_URL), SQLite local.
Separată de baza aplicației interne de monitorizare: site-ul public nu are acces la
portofoliul clienților.
"""
import os
import re
import unicodedata
from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, Integer, JSON, LargeBinary, String, Text,
                        create_engine, text)
from sqlalchemy.orm import declarative_base, sessionmaker

_url = os.environ.get("DATABASE_URL", "")
if _url.startswith("postgres://"):
    _url = _url.replace("postgres://", "postgresql://", 1)
if not _url:
    _url = "sqlite:///" + os.environ.get("REGISTRU_DB_PATH",
                                         os.path.join(os.path.dirname(__file__), "registru.db"))

IS_PG = _url.startswith("postgresql")
engine = create_engine(_url, pool_pre_ping=True,
                       **({} if IS_PG else {"connect_args": {"check_same_thread": False}}))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def norm(s) -> str:
    """Text comparabil pentru căutare: fără diacritice, litere mici, spații simple."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&@+ ]+", " ", s)).strip()


class Mark(Base):
    """O marcă OSIM, consolidată din toate aparițiile ei în BOPI (cerere publicată, admisă,
    înregistrată, respinsă). Cheia e nr. de depozit compact: „M202609829”."""
    __tablename__ = "marks"

    app_number       = Column(String, primary_key=True)
    name             = Column(Text, default="")
    name_norm        = Column(Text, default="", index=True)
    mark_type        = Column(String, default="")       # Verbală / Figurativă / Combinată
    status           = Column(String, default="", index=True)
    status_date      = Column(String, default="")       # YYYY-MM-DD — buletinul care a dat starea
    filing_date      = Column(String, default="", index=True)
    publication_date = Column(String, default="", index=True)
    reg_number       = Column(String, default="", index=True)
    reg_date         = Column(String, default="", index=True)
    expiry_date      = Column(String, default="", index=True)   # calculată: depozit + 10 ani / ultima reînnoire
    tmview_checked   = Column(String, default="")              # data căutării denumirii în TMview (mărci reînnoite vechi)
    holder           = Column(Text, default="")
    holder_norm      = Column(Text, default="", index=True)
    holder_address   = Column(Text, default="")
    representative   = Column(Text, default="")
    rep_norm         = Column(Text, default="", index=True)
    representative_address = Column(Text, default="")
    classes          = Column(JSON, default=list)
    goods            = Column(JSON, default=dict)       # {"35": "Publicitate, ..."}
    vienna           = Column(Text, default="")
    colors           = Column(Text, default="")
    disclaimer       = Column(Text, default="")       # declarația de neinvocare a unui drept exclusiv
    has_image        = Column(Boolean, default=False)
    events           = Column(JSON, default=list)       # [{"date", "bopi", "chapter"}]
    updated_at       = Column(DateTime, default=datetime.utcnow)


class MarkImage(Base):
    __tablename__ = "mark_images"

    app_number   = Column(String, primary_key=True)
    content_type = Column(String, default="image/webp")
    data         = Column(LargeBinary, nullable=False)


class SourceFile(Base):
    """Un PDF BOPI procesat (zilnic / lunar / cereri admise) — ca reluarea să nu-l repete."""
    __tablename__ = "source_files"

    url           = Column(String, primary_key=True)
    kind          = Column(String, default="")          # daily / monthly / admise
    bulletin_date = Column(String, default="", index=True)
    label         = Column(String, default="")
    status        = Column(String, default="")          # ok / missing / error
    marks         = Column(Integer, default=0)
    error         = Column(Text, default="")
    processed_at  = Column(DateTime, default=datetime.utcnow)


class MarkChange(Base):
    """Un rând din tabelele buletinului lunar (transfer, reînnoire, radiere, licență, limitare,
    modificarea reprezentării). Se păstrează și pentru mărcile care nu sunt încă în bază (mai vechi
    de 2022), ca să poată fi aplicate când va fi adăugată arhiva."""
    __tablename__ = "mark_changes"

    id            = Column(Integer, primary_key=True)
    kind          = Column(String, default="", index=True)
    app_number    = Column(String, default="", index=True)
    reg_number    = Column(String, default="", index=True)
    old_value     = Column(Text, default="")
    new_value     = Column(Text, default="")
    expiry        = Column(String, default="")
    title         = Column(String, default="")
    bulletin_date = Column(String, default="", index=True)
    label         = Column(String, default="")
    applied       = Column(Boolean, default=False)


class Lead(Base):
    """O solicitare trimisă de un vizitator: informații despre mărci bifate / cercetare."""
    __tablename__ = "leads"

    id         = Column(Integer, primary_key=True)
    kind       = Column(String, default="info")         # info / cercetare
    name       = Column(String, default="")
    email      = Column(String, default="")
    phone      = Column(String, default="")
    message    = Column(Text, default="")
    marks      = Column(JSON, default=list)             # nr. de depozit bifate
    created_at = Column(DateTime, default=datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _fix_disclaimers():
    """Corectează mărcile încărcate înainte ca declarația art. 22 să fie separată de denumire."""
    from bopi_parser import split_disclaimer
    db = SessionLocal()
    try:
        n = 0
        q = db.query(Mark).filter(Mark.name.like("%conformitate%") | Mark.name.like("%Conform %") |
                                  Mark.name.like("%Potrivit%"))
        for m in q:
            name, disc = split_disclaimer(m.name)
            if disc:
                m.name, m.name_norm = name, norm(name)
                m.disclaimer = (m.disclaimer + " " + disc).strip() if m.disclaimer else disc
                n += 1
        db.commit()
        if n:
            print(f"[DB] Declarații separate de denumire: {n}")
    finally:
        db.close()


def _ensure_columns():
    from sqlalchemy import inspect
    have = {c["name"] for c in inspect(engine).get_columns("marks")}
    if "tmview_checked" not in have:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE marks ADD COLUMN tmview_checked VARCHAR DEFAULT ''"))
    if "expiry_date" not in have:
        with engine.begin() as c:
            c.execute(text("ALTER TABLE marks ADD COLUMN expiry_date VARCHAR DEFAULT ''"))
            c.execute(text("CREATE INDEX IF NOT EXISTS ix_marks_expiry_date ON marks (expiry_date)"))
        from ingest import expiry_of
        db = SessionLocal()
        try:
            for m in db.query(Mark).filter(Mark.status == "Înregistrată").yield_per(1000):
                m.expiry_date = expiry_of(m)
            db.commit()
        finally:
            db.close()
        print("[DB] Coloana marks.expiry_date adăugată și completată")


def _reprocess_old_changes():
    """Reînnoirile citite înainte să păstrăm clasele (old_value gol) — tabelele lunare respective
    se reprocesează o dată, ca mărcile reînnoite fără marcă în bază să intre cu clasele lor."""
    db = SessionLocal()
    try:
        stale = db.query(MarkChange.label).filter(
            ((MarkChange.kind == "renewal") & (MarkChange.old_value == "")) |
            ((MarkChange.kind == "limitation") & (MarkChange.new_value == ""))).distinct().all()
        labels = [l for (l,) in stale]
        if labels:
            n = db.query(SourceFile).filter(SourceFile.kind == "changes", SourceFile.label.in_(labels))\
                .delete(synchronize_session=False)
            db.commit()
            print(f"[DB] Tabele de modificări de reprocesat (clase la reînnoiri): {n}")
    finally:
        db.close()


def _reprocess_for_history():
    """O singură dată: buletinele citite înainte ca evenimentele să rețină titularul, denumirea și
    clasele de la data lor se recitesc, ca istoricul mărcilor să fie complet."""
    marker = "migration:history-v1"
    db = SessionLocal()
    try:
        if db.get(SourceFile, marker):
            return
        n = db.query(SourceFile).filter(SourceFile.kind.in_(("daily", "admise", "monthly", "changes")),
                                        SourceFile.status == "ok").delete(synchronize_session=False)
        db.add(SourceFile(url=marker, kind="migration", status="ok", label="istoric v1"))
        db.commit()
        print(f"[DB] Buletine de recitit pentru istoric: {n}")
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(engine)
    _ensure_columns()
    _fix_disclaimers()
    _reprocess_old_changes()
    _reprocess_for_history()
    if IS_PG:
        # Căutare „conține” rapidă pe sute de mii de rânduri: index trigram.
        with engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            for col in ("name_norm", "holder_norm", "rep_norm"):
                c.execute(text(f"CREATE INDEX IF NOT EXISTS ix_marks_{col}_trgm "
                               f"ON marks USING gin ({col} gin_trgm_ops)"))
