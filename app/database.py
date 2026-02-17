import os
import json
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

SETTINGS_PATH = Path(os.getenv("MTS_RUNTIME_SETTINGS_PATH", "/data/config/runtime_settings.json"))


def _load_runtime_settings() -> dict:
    try:
        if SETTINGS_PATH.exists():
            return json.loads(SETTINGS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {}


runtime_settings = _load_runtime_settings()
configured_sql_path = (runtime_settings.get("SQL_DATA_PATH") or os.getenv("SQL_DATA_PATH") or "").strip()
default_sql_path = "/data/sql/mts.db"
repo_sql_path = str(Path(__file__).resolve().parents[1] / "data/sql/mts.db")

if configured_sql_path:
    SQL_DATA_PATH = configured_sql_path
elif Path(default_sql_path).exists():
    SQL_DATA_PATH = default_sql_path
elif Path(repo_sql_path).exists():
    SQL_DATA_PATH = repo_sql_path
else:
    SQL_DATA_PATH = default_sql_path

os.makedirs(os.path.dirname(SQL_DATA_PATH), exist_ok=True)

DATABASE_URL = f"sqlite:///{SQL_DATA_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
