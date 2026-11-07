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


def init_db():
    Base.metadata.create_all(engine)
    if IS_PG:
        # Căutare „conține” rapidă pe sute de mii de rânduri: index trigram.
        with engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            for col in ("name_norm", "holder_norm", "rep_norm"):
                c.execute(text(f"CREATE INDEX IF NOT EXISTS ix_marks_{col}_trgm "
                               f"ON marks USING gin ({col} gin_trgm_ops)"))
