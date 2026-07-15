import os
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from datetime import datetime
from sqlalchemy.orm import Session
from backend import models


def get_drive_service(user_email: str, db: Session):
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
        print(f"✅ Drive token refreshed for {user_email}")

    service = build('drive', 'v3', credentials=creds)

    return service


def create_folder(service, folder_name: str, parent_id: str = None) -> str:
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }

    if parent_id:
        file_metadata['parents'] = [parent_id]

    folder = service.files().create(
        body=file_metadata,
        fields='id, name'
    ).execute()

    print(f"📁 Created folder: {folder_name} (ID: {folder['id']})")

    return folder['id']


def find_folder(service, folder_name: str, parent_id: str = None) -> str:
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"

    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(
        q=query,
        fields='files(id, name)'
    ).execute()

    files = results.get('files', [])

    if files:
        print(f"✅ Found existing folder: {folder_name}")
        return files[0]['id']

    return None


def get_or_create_folder(service, folder_name: str, parent_id: str = None) -> str:
    existing_id = find_folder(service, folder_name, parent_id)

    if existing_id:
        return existing_id

    return create_folder(service, folder_name, parent_id)


def create_candidate_folder_structure(
    service,
    candidate_id: str,
    candidate_name: str,
    parent_folder_id: str
) -> dict:
    clean_name = candidate_name.split()[0].lower()

    categories = [
        'Identity',
        'Address',
        'Education',
        'Employment',
        'Financial',
        'Professional',
        'Compliance',
        'Photo_Personal',
        'International'
    ]

    category_folder_ids = {}
    category_folder_names = {}

    for category in categories:
        folder_name = f"{candidate_id}_{clean_name}_{category.lower()}"

        folder_id = get_or_create_folder(
            service,
            folder_name,
            parent_folder_id
        )

        category_folder_ids[category.lower()] = folder_id
        category_folder_names[category.lower()] = folder_name

    print(f"✅ Created folder structure for {candidate_id}_{clean_name}")

    return {
        'candidate_folder_name': f"{candidate_id}_{clean_name}",
        'category_folders': category_folder_ids,
        'category_folder_names': category_folder_names
    }


def upload_file_to_drive(
    service,
    local_file_path: str,
    filename: str,
    folder_id: str
) -> dict:
    file_metadata = {
        'name': filename,
        'parents': [folder_id]
    }

    media = MediaFileUpload(
        local_file_path,
        mimetype='application/pdf',
        resumable=True
    )

    uploaded_file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id, name, webViewLink'
    ).execute()

    print(f"✅ Uploaded: {filename} to Drive (ID: {uploaded_file['id']})")

    return {
        'file_id': uploaded_file['id'],
        'filename': uploaded_file['name'],
        'web_link': uploaded_file.get('webViewLink', '')
    }


def get_main_bgv_folder(service, folder_name: str = 'Candidate_Documents') -> str:
    folder_id = get_or_create_folder(
        service,
        folder_name
    )

    return folder_id


def find_candidate_pdf_in_source_drive(
    service,
    candidate_email: str,
    source_folder_id: str
) -> str:
    query = (
        f"'{source_folder_id}' in parents "
        f"and (name contains '{candidate_email}') "
        f"and mimeType='application/pdf' "
        f"and trashed=false"
    )

    results = service.files().list(
        q=query,
        fields='files(id, name)'
    ).execute()

    files = results.get('files', [])

    if files:
        print(f"✅ Found source document for {candidate_email}: {files[0]['name']}")
        return files[0]['id']

    print(f"❌ No source document found for {candidate_email} in source folder")
    return None


def download_file_from_drive(service, file_id: str, save_path: str) -> str:
    from googleapiclient.http import MediaIoBaseDownload
    import io

    request = service.files().get_media(fileId=file_id)

    file_data = io.BytesIO()
    downloader = MediaIoBaseDownload(file_data, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()

    file_data.seek(0)

    with open(save_path, 'wb') as f:
        f.write(file_data.read())

    print(f"✅ Downloaded source file to: {save_path}")
    return save_path


def delete_file_from_drive(service, file_id: str) -> bool:
    try:
        service.files().delete(fileId=file_id).execute()
        print(f"🗑️ Deleted old file from Drive (ID: {file_id})")
        return True
    except Exception as e:
        print(f"⚠️ Could not delete old file from Drive: {e}")
        return False