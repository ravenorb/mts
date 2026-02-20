from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from pathlib import Path

Base = declarative_base()

def _db_url() -> str:
    # default ./data/mts.db, override with MTS_DB_PATH
    p = os.environ.get("MTS_DB_PATH")
    if p:
        return f"sqlite:///{p}"
    Path("data").mkdir(parents=True, exist_ok=True)
    return "sqlite:///data/mts.db"

engine = create_engine(
    _db_url(),
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
