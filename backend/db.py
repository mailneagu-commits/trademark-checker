import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Dacă DATABASE_URL e setat (ex: PostgreSQL pe Railway), îl folosim direct.
# Altfel, SQLite local.
_pg_url = os.environ.get("DATABASE_URL", "")
if _pg_url.startswith("postgres://"):
    # SQLAlchemy necesită "postgresql://" nu "postgres://"
    _pg_url = _pg_url.replace("postgres://", "postgresql://", 1)

if _pg_url:
    DATABASE_URL = _pg_url
    _engine_kwargs = {}
else:
    DB_PATH = os.environ.get(
        "MONITOR_DB_PATH",
        os.path.join(os.path.dirname(__file__), "..", "monitor.db"),
    )
    DATABASE_URL = f"sqlite:///{DB_PATH}"
    _engine_kwargs = {"connect_args": {"check_same_thread": False}}

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from monitor_models import WatchItem, SeenTrademark, AlertLog  # noqa: F401
    Base.metadata.create_all(bind=engine)
    print(f"[DB] Using: {'PostgreSQL' if _pg_url else 'SQLite'}")
