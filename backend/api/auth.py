import os
import secrets
import time
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend import models
from backend.config import FRONTEND_URL, GOOGLE_REDIRECT_URI
from backend.database import get_db
from backend.services.google_auth import GoogleAuthError, get_admin_email, set_admin_email

router = APIRouter()

SCOPES = (
    "email profile "
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/drive"
)

# One-time `state` values protect the OAuth callback against forged requests (CSRF).
# In-memory is fine: the app runs as a single process and a state only lives 10 minutes.
_STATE_TTL = 600
_pending_states: dict = {}


def _new_state() -> str:
    now = time.time()
    for key in [k for k, t in _pending_states.items() if now - t > _STATE_TTL]:
        del _pending_states[key]
    state = secrets.token_urlsafe(24)
    _pending_states[state] = now
    return state


def _consume_state(state: str) -> bool:
    created = _pending_states.pop(state, None)
    return created is not None and time.time() - created <= _STATE_TTL


@router.get("/auth/google")
def google_login():
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID is not configured on the server.")

    params = urlencode({
        "client_id": client_id,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": _new_state(),
    })

    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@router.get("/auth/callback")
def google_callback(
    code: str = None,
    state: str = None,
    error: str = None,
    db: Session = Depends(get_db)
):
    if error:
        return RedirectResponse(f"{FRONTEND_URL}/settings.html?{urlencode({'auth_error': error})}")

    if not code or not state or not _consume_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired login attempt. Please try again.")

    token_response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code"
        },
        timeout=15
    )

    token_data = token_response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        raise HTTPException(status_code=400, detail="Login failed: Google did not return an access token.")

    user_response = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15
    )
    user_info = user_response.json()

    if "email" not in user_info:
        raise HTTPException(status_code=400, detail="Login failed: could not read your Google profile.")

    email = user_info["email"]
    refresh_token = token_data.get("refresh_token")

    user = db.query(models.User).filter(models.User.email == email).first()

    if not user:
        user = models.User(email=email, name=user_info.get("name"))
        db.add(user)

    user.access_token = access_token
    if refresh_token:
        user.refresh_token = refresh_token
    db.commit()

    # Jobs run as whoever logged in most recently.
    set_admin_email(db, email)

    return RedirectResponse(f"{FRONTEND_URL}/settings.html?{urlencode({'connected': email})}")


@router.get("/auth/status")
def auth_status(db: Session = Depends(get_db)):
    """Which Google account the app uses. Never returns tokens."""
    try:
        email = get_admin_email(db)
    except GoogleAuthError:
        return {"connected": False, "email": None, "name": None}

    user = db.query(models.User).filter(models.User.email == email).first()
    return {
        "connected": bool(user and (user.refresh_token or user.access_token)),
        "email": email,
        "name": user.name if user else None,
    }


@router.get("/auth/reconnect")
def reconnect_google():
    """
    Same flow as initial login — sends user back through
    Google's consent screen to get fresh access + refresh tokens.
    Used when existing tokens are expired/revoked beyond
    what auto-refresh can fix.
    """
    return google_login()
