import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

SQL_DATA_PATH = os.getenv("SQL_DATA_PATH", "/data/sql/mts.db")
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
