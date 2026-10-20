import os

from dotenv import load_dotenv

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Copy .env.example to .env and set your PostgreSQL "
        "connection string (e.g. postgresql://user:password@localhost:5432/bgv)."
    )

_engine_args = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_args["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_args)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
