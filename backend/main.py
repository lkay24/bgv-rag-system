import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import CORS_ORIGINS
from backend.database import engine, Base, SessionLocal
from backend import models

from backend.api.auth import router as auth_router
from backend.api.candidates import router as candidates_router
from backend.api.settings import router as settings_router

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


def recover_interrupted_work():
    """A crash or restart mid-batch leaves rows stuck in 'processing' forever; release them."""
    db = SessionLocal()
    try:
        stuck_candidates = db.query(models.Candidate).filter(
            models.Candidate.status == 'processing'
        ).update({"status": "pending"})
        stuck_batches = db.query(models.Batch).filter(
            models.Batch.processing_status == 'processing'
        ).update({"processing_status": "failed"})
        db.commit()
        if stuck_candidates or stuck_batches:
            print(f"♻️ Released {stuck_candidates} candidate(s) and {stuck_batches} batch(es) "
                  f"left in 'processing' by a previous run")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    recover_interrupted_work()
    print("✅ Database ready")
    yield


app = FastAPI(
    title="Background Verification System",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api")
app.include_router(candidates_router, prefix="/api")
app.include_router(settings_router, prefix="/api")


@app.get("/health")
def health_check():
    return {"status": "ok"}


# The UI is plain static files; serve them from the same origin as the API so no CORS or
# hard-coded host is needed. Mounted last so it never shadows an API route.
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
