"""Environment-driven settings shared across the backend."""
import os
import tempfile

from dotenv import load_dotenv

load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", BACKEND_URL).rstrip("/")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", f"{BACKEND_URL}/api/auth/callback")

# Origins allowed to call the API from a browser. The bundled frontend is served by
# the backend itself (same origin), so this only matters for Live Server / file:// use.
_default_origins = (
    "http://localhost:8000,http://127.0.0.1:8000,"
    "http://localhost:5500,http://127.0.0.1:5500,"
    "http://localhost:5501,http://127.0.0.1:5501"
)
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", _default_origins).split(",")
    if o.strip()
] + ([FRONTEND_URL] if FRONTEND_URL else [])

# Per-candidate scratch space for downloaded attachments.
WORK_ROOT = os.path.join(tempfile.gettempdir(), "bgv")

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024
