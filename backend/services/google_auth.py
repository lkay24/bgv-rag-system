"""Shared Google credential handling for the Gmail and Drive clients."""
import os
import time
from datetime import datetime

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from backend import models

# Access tokens live ~1h and we don't persist their expiry, so remember when we last
# refreshed per user and refresh again well before the hour is up.
_REFRESH_EVERY = 45 * 60
_last_refresh: dict = {}


class GoogleAuthError(Exception):
    """Raised when stored Google credentials are missing or no longer valid."""


def get_credentials(user_email: str, db: Session) -> Credentials:
    user = db.query(models.User).filter(models.User.email == user_email).first()

    if not user:
        raise GoogleAuthError(f"User {user_email} not found. Please log in with Google first.")

    if not user.access_token and not user.refresh_token:
        raise GoogleAuthError(f"No Google tokens stored for {user_email}. Please reconnect.")

    creds = Credentials(
        token=user.access_token,
        refresh_token=user.refresh_token,
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )

    stale = time.time() - _last_refresh.get(user_email, 0) > _REFRESH_EVERY
    if user.refresh_token and (creds.expired or stale or not user.access_token):
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise GoogleAuthError(
                f"Google rejected the stored credentials for {user_email} ({e}). Please reconnect."
            ) from e
        user.access_token = creds.token
        db.commit()
        _last_refresh[user_email] = time.time()
        print(f"✅ Google token refreshed for {user_email}")

    return creds


def get_admin_email(db: Session, requested: str = None) -> str:
    """Account used for Gmail/Drive access: the one asked for, else the last one that logged in."""
    if requested:
        return requested

    setting = db.query(models.Settings).filter(models.Settings.key == 'admin_email').first()
    if setting and setting.value:
        return setting.value

    user = db.query(models.User).order_by(models.User.id.desc()).first()
    if user:
        return user.email

    raise GoogleAuthError("No Google account is connected yet. Log in with Google from the Settings page.")


def set_admin_email(db: Session, email: str) -> None:
    setting = db.query(models.Settings).filter(models.Settings.key == 'admin_email').first()
    if setting:
        setting.value = email
        setting.updated_at = datetime.utcnow()
    else:
        db.add(models.Settings(key='admin_email', value=email))
    db.commit()
