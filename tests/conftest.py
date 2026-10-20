import os
import tempfile

import pytest

# Must be set before any backend module is imported.
_db_file = os.path.join(tempfile.mkdtemp(prefix="bgv-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"
os.environ.setdefault("GROQ_API_KEY", "test")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from backend.database import Base, engine, SessionLocal  # noqa: E402
from backend.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
