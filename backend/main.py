from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.database import engine, Base
from backend import models

from backend.api.auth import router as auth_router
from backend.api.candidates import router as candidates_router
from backend.api.settings import router as settings_router

app = FastAPI(
    title="Background Verification System",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api")
app.include_router(candidates_router, prefix="/api")
app.include_router(settings_router, prefix="/api")


@app.on_event("startup")
def create_tables():
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created!")


@app.get("/")
def home():
    return {"message": "BGV System is running!"}


@app.get("/health")
def health_check():
    return {"status": "ok"}