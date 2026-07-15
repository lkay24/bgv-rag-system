

import os
import base64
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from sqlalchemy.orm import Session
from backend import models


def get_gmail_service(user_email: str, db: Session):
    """
    Creates connection to Gmail API
    using saved access token
    """
    user = db.query(models.User).filter(
        models.User.email == user_email
    ).first()
    
    if not user:
        raise Exception(f"User {user_email} not found")
    
    if not user.access_token:
        raise Exception(f"No access token for {user_email}")
    
    creds = Credentials(
        token=user.access_token,
        refresh_token=user.refresh_token,
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token"
    )
    
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        user.access_token = creds.token
        db.commit()
        print(f"✅ Token refreshed for {user_email}")
    
    service = build('gmail', 'v1', credentials=creds)
    return service


def search_candidate_emails(service, candidate_email: str) -> list:
    """
    Search Gmail for emails from a specific candidate
    """
    query = f"from:{candidate_email} has:attachment"
    
    print(f"🔍 Searching Gmail for: {query}")
    
    results = service.users().messages().list(
        userId='me',
        q=query
    ).execute()
    
    messages = results.get('messages', [])
    print(f"📧 Found {len(messages)} emails from {candidate_email}")
    
    return messages


def download_attachments(service, message_id: str, save_folder: str) -> list:
    """
    Download all PDF attachments from one email
    """
    os.makedirs(save_folder, exist_ok=True)
    
    message = service.users().messages().get(
        userId='me',
        id=message_id,
        format='full'
    ).execute()
    
    downloaded_files = []
    
    payload = message.get('payload', {})
    parts = payload.get('parts', [])
    
    if not parts:
        parts = [payload]
    
    for part in parts:
        filename = part.get('filename', '')
        
        if filename and filename.lower().endswith('.pdf'):
            print(f"📎 Found attachment: {filename}")
            
            body = part.get('body', {})
            attachment_id = body.get('attachmentId')
            
            if attachment_id:
                attachment = service.users().messages().attachments().get(
                    userId='me',
                    messageId=message_id,
                    id=attachment_id
                ).execute()
                
                file_data = base64.urlsafe_b64decode(attachment['data'])
                
                file_path = os.path.join(save_folder, filename)
                
                with open(file_path, 'wb') as f:
                    f.write(file_data)
                
                downloaded_files.append(file_path)
                print(f"✅ Saved: {file_path}")
    
    return downloaded_files


def get_candidate_documents(service, candidate_email: str, candidate_id: str) -> list:
    """
    Find all emails from candidate and
    download all PDF attachments
    """
    emails = search_candidate_emails(service, candidate_email)
    
    if not emails:
        print(f"❌ No emails found from {candidate_email}")
        return []
    
    save_folder = f"/tmp/bgv/{candidate_id}"
    all_pdfs = []
    
    for email in emails:
        email_id = email['id']
        pdfs = download_attachments(service, email_id, save_folder)
        all_pdfs.extend(pdfs)
    
    print(f"📁 Total PDFs downloaded for {candidate_id}: {len(all_pdfs)}")
    return all_pdfs