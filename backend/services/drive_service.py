import io
import string
import re
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from sqlalchemy.orm import Session

from backend.services.file_utils import drive_escape
from backend.services.google_auth import get_credentials
from backend.taxonomy import CATEGORIES

DEFAULT_FOLDER_FORMAT = '{candidate_id}-{name}-{date}'
FOLDER_FORMAT_FIELDS = ('candidate_id', 'name', 'date')


def get_drive_service(user_email: str, db: Session):
    return build('drive', 'v3', credentials=get_credentials(user_email, db), cache_discovery=False)


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
    query = f"name='{drive_escape(folder_name)}' and mimeType='application/vnd.google-apps.folder' and trashed=false"

    if parent_id:
        query += f" and '{drive_escape(parent_id)}' in parents"

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


def validate_folder_format(folder_format: str) -> str:
    """Return an error message if the format can't produce unique folder names, else ''."""
    fmt = (folder_format or '').strip()
    if not fmt:
        return 'Folder name format cannot be empty.'
    try:
        fields = {name for _, name, _, _ in string.Formatter().parse(fmt) if name}
    except ValueError as e:
        return f'Invalid folder name format: {e}'
    unknown = fields - set(FOLDER_FORMAT_FIELDS)
    if unknown:
        allowed = ', '.join('{' + f + '}' for f in FOLDER_FORMAT_FIELDS)
        return f"Unknown placeholder(s) {sorted(unknown)}. Allowed: {allowed}."
    if 'candidate_id' not in fields:
        return 'Folder name format must include {candidate_id} so every candidate gets a unique folder.'
    return ''


def build_candidate_folder_name(
    folder_format: str,
    candidate_id: str,
    candidate_name: str,
    today: datetime = None
) -> str:
    """Render the configured candidate folder name, falling back to the default format."""
    if validate_folder_format(folder_format):
        folder_format = DEFAULT_FOLDER_FORMAT

    today = today or datetime.now()
    name = re.sub(r'\s+', '_', (candidate_name or '').strip())
    rendered = folder_format.strip().format(
        candidate_id=candidate_id,
        name=name,
        date=today.strftime('%Y-%m-%d')
    )
    # '/' would read as a path separator in Drive listings.
    return rendered.replace('/', '-').strip() or str(candidate_id)


def create_candidate_folder_structure(
    service,
    candidate_id: str,
    candidate_name: str,
    parent_folder_id: str,
    folder_format: str = DEFAULT_FOLDER_FORMAT
) -> dict:
    """<parent>/<candidate folder>/<Category>/ ... using the configured folder name format."""
    candidate_folder_name = build_candidate_folder_name(folder_format, candidate_id, candidate_name)
    candidate_folder_id = get_or_create_folder(service, candidate_folder_name, parent_folder_id)

    category_folder_ids = {}
    category_folder_names = {}

    for category in CATEGORIES:
        display_name = category.title()
        category_folder_ids[category] = get_or_create_folder(service, display_name, candidate_folder_id)
        category_folder_names[category] = f"{candidate_folder_name}/{display_name}"

    print(f"✅ Created folder structure for {candidate_folder_name}")

    return {
        'candidate_folder_name': candidate_folder_name,
        'candidate_folder_id': candidate_folder_id,
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


def find_candidate_files_in_source_drive(
    service,
    candidate_email: str,
    source_folder_id: str,
    limit: int = 20
) -> list:
    """File ids of every PDF in the source folder whose name contains the candidate's email."""
    query = (
        f"'{drive_escape(source_folder_id)}' in parents "
        f"and name contains '{drive_escape(candidate_email)}' "
        f"and mimeType='application/pdf' "
        f"and trashed=false"
    )

    results = service.files().list(
        q=query,
        fields='files(id, name)',
        pageSize=limit
    ).execute()

    files = results.get('files', [])

    if files:
        print(f"✅ Found {len(files)} source document(s) for {candidate_email}")
    else:
        print(f"❌ No source document found for {candidate_email} in source folder")

    return [f['id'] for f in files]


def download_file_from_drive(service, file_id: str, save_path: str) -> str:
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