
import os
import requests


from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

# Our database connection function
from backend.database import get_db

# Our Candidate/User models
from backend import models


router = APIRouter()


@router.get("/auth/google")
def google_login():
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    

    google_auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={client_id}"
        "&redirect_uri=http://localhost:8000/api/auth/callback"
        "&response_type=code"
      
        "&scope=email profile https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/drive"
        "&access_type=offline"
        "&prompt=consent"
    )
    
    
    return RedirectResponse(google_auth_url)


@router.get("/auth/callback")
def google_callback(code: str, db: Session = Depends(get_db)):
 
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    
    
    token_response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": "http://localhost:8000/api/auth/callback",
            "grant_type": "authorization_code"
        }
    )
    
    token_data = token_response.json()
    access_token = token_data.get("access_token")
    
    if not access_token:
        raise HTTPException(status_code=400, detail="Login failed!")
    
    
    user_response = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    
    user_info = user_response.json()
    
    existing_user = db.query(models.User).filter(
        models.User.email == user_info["email"]
    ).first()
    
    refresh_token = token_data.get("refresh_token")

    if not existing_user:
        new_user = models.User(
            email=user_info["email"],
            name=user_info["name"],
            access_token=access_token,
            refresh_token=refresh_token  
        )
        db.add(new_user)
        db.commit()
    else:
        existing_user.access_token = access_token
        
        if refresh_token:
            existing_user.refresh_token = refresh_token
        db.commit()
    
    
    return {
        "message": "Login successful!",
        "user": user_info["email"],
        "name": user_info["name"]
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