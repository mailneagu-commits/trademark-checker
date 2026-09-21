import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from paths import DATA_DIR

# Dacă DATABASE_URL e setat (ex: PostgreSQL pe Railway), îl folosim direct.
# Altfel, SQLite — pe DATA_DIR (volum persistent) dacă e configurat, altfel pe
# filesystem-ul efemer al containerului (implicit — se pierde la fiecare deploy).
_pg_url = os.environ.get("DATABASE_URL", "")
if _pg_url.startswith("postgres://"):
    # SQLAlchemy necesită "postgresql://" nu "postgres://"
    _pg_url = _pg_url.replace("postgres://", "postgresql://", 1)

if _pg_url:
    DATABASE_URL = _pg_url
    _engine_kwargs = {}
else:
    _default_db_path = (
        os.path.join(DATA_DIR, "monitor.db") if DATA_DIR
        else os.path.join(os.path.dirname(__file__), "..", "monitor.db")
    )
    DB_PATH = os.environ.get("MONITOR_DB_PATH", _default_db_path)
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    DATABASE_URL = f"sqlite:///{DB_PATH}"
    _engine_kwargs = {"connect_args": {"check_same_thread": False}}

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def db_info() -> dict:
    """Ce bază de date folosim și dacă supraviețuiește unui deploy. SQLite pe discul efemer al
    containerului se șterge la fiecare deploy — persistă doar PostgreSQL (DATABASE_URL) sau un
    volum Railway montat la DATA_DIR."""
    if _pg_url:
        return {"engine": "postgresql", "persistent": True}
    # DATA_DIR setat nu e suficient: trebuie să fie un volum montat, altfel tot disc efemer
    on_volume = bool(DATA_DIR) and DB_PATH.startswith(DATA_DIR) and os.path.ismount(DATA_DIR)
    return {"engine": "sqlite", "persistent": on_volume, "path": DB_PATH,
            "data_dir": DATA_DIR or None, "data_dir_is_mount": bool(DATA_DIR) and os.path.ismount(DATA_DIR)}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_columns(eng=None):
    """create_all nu adaugă coloane noi în tabele deja create — le adăugăm aici (SQLite/PostgreSQL),
    ca o bază de date existentă să continue să funcționeze după ce modelul primește câmpuri noi."""
    from sqlalchemy import inspect, text
    eng = eng or engine
    insp = inspect(eng)
    if "watch_items" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("watch_items")}
    for name in ("publication_date", "representative_name"):
        if name not in have:
            with eng.begin() as conn:
                conn.execute(text(f"ALTER TABLE watch_items ADD COLUMN {name} VARCHAR"))
            print(f"[DB] Coloană adăugată: watch_items.{name}")


def _ensure_columns_for(eng):
    _ensure_columns(eng)


def init_db():
    from monitor_models import WatchItem, SeenTrademark, AlertLog, BulletinMark, BulletinImage  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    print(f"[DB] Using: {'PostgreSQL' if _pg_url else 'SQLite'}")
