import base64
import os
import re

from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from backend.services.file_utils import IMAGE_EXTENSIONS, candidate_workdir, image_to_pdf, safe_name
from backend.services.google_auth import get_credentials

_EMAIL_RE = re.compile(r"^[^\s@\"'()<>,;]+@[^\s@\"'()<>,;]+$")


def get_gmail_service(user_email: str, db: Session):
    return build('gmail', 'v1', credentials=get_credentials(user_email, db), cache_discovery=False)


def search_candidate_emails(service, candidate_email: str) -> list:
    # The address comes from an uploaded spreadsheet; never let it inject search operators.
    if not _EMAIL_RE.match(candidate_email or ""):
        print(f"⚠️ Skipping Gmail search for invalid address: {candidate_email!r}")
        return []

    query = f"from:{candidate_email} has:attachment"

    print(f"🔍 Searching Gmail for: {query}")

    messages = []
    page_token = None
    while True:
        results = service.users().messages().list(
            userId='me',
            q=query,
            pageToken=page_token
        ).execute()
        messages.extend(results.get('messages', []))
        page_token = results.get('nextPageToken')
        if not page_token:
            break

    print(f"📧 Found {len(messages)} emails from {candidate_email}")

    return messages


def _walk_parts(payload: dict):
    """Yield every leaf part of a (possibly nested multipart) message."""
    stack = [payload]
    while stack:
        part = stack.pop(0)
        children = part.get('parts')
        if children:
            stack = list(children) + stack
        else:
            yield part


def _is_supported(filename: str) -> bool:
    lower = filename.lower()
    return lower.endswith('.pdf') or lower.endswith(IMAGE_EXTENSIONS)


def download_attachments(
    service,
    message_id: str,
    save_folder: str
) -> list:
    """Download PDF / image attachments from one email. Images are converted to PDF."""
    os.makedirs(save_folder, exist_ok=True)

    message = service.users().messages().get(
        userId='me',
        id=message_id,
        format='full'
    ).execute()

    downloaded_files = []

    for index, part in enumerate(_walk_parts(message.get('payload', {}))):
        filename = part.get('filename', '')

        if not filename or not _is_supported(filename):
            continue

        print(f"📎 Found attachment: {filename}")

        body = part.get('body', {})
        attachment_id = body.get('attachmentId')

        if attachment_id:
            attachment = service.users().messages().attachments().get(
                userId='me',
                messageId=message_id,
                id=attachment_id
            ).execute()
            data = attachment['data']
        elif body.get('data'):
            data = body['data']
        else:
            continue

        file_data = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))

        # Filenames come from the sender: strip any path and prefix with the message id
        # so two emails attaching "aadhar.pdf" don't overwrite each other.
        local_name = f"{message_id}_{index}_{safe_name(filename)}"
        file_path = os.path.join(save_folder, local_name)

        with open(file_path, 'wb') as f:
            f.write(file_data)

        if not filename.lower().endswith('.pdf'):
            pdf_path = os.path.splitext(file_path)[0] + '.pdf'
            try:
                image_to_pdf(file_path, pdf_path)
            except Exception as e:
                print(f"⚠️ Could not convert image {filename} to PDF: {e}")
                continue
            file_path = pdf_path

        downloaded_files.append(file_path)
        print(f"✅ Saved: {file_path}")

    return downloaded_files


def get_candidate_documents(
    service,
    candidate_email: str,
    candidate_id: str,
    save_folder: str = None
) -> list:
    emails = search_candidate_emails(service, candidate_email)

    if not emails:
        print(f"❌ No emails found from {candidate_email}")
        return []

    if save_folder is None:
        save_folder = candidate_workdir(candidate_id)

    all_pdfs = []

    for email in emails:
        pdfs = download_attachments(service, email['id'], save_folder)
        all_pdfs.extend(pdfs)

    print(f"📁 Total PDFs downloaded for {candidate_id}: {len(all_pdfs)}")

    return all_pdfs
