import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.environ.get("MONITOR_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "monitor.db"))
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
