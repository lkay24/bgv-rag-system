import io
import re
from collections import Counter, defaultdict

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import models
from backend.agents.pipeline import process_batch, process_candidate
from backend.api.settings import get_setting_value
from backend.config import MAX_UPLOAD_BYTES
from backend.database import SessionLocal, get_db
from backend.services.gmail_service import get_gmail_service
from backend.services.google_auth import GoogleAuthError, get_admin_email

router = APIRouter()

REQUIRED_COLUMNS = ['candidateId', 'name', 'email', 'phone']
STATUSES = ['pending', 'processing', 'completed', 'partial', 'failed']
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _cell(value) -> str:
    """Excel cell -> clean string ('' for blanks, no trailing '.0' on whole numbers)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ''
    text = str(value).strip()
    if text.lower() in ('nan', 'none', 'nat'):
        return ''
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _status_counts(db: Session, batch_id: int = None) -> dict:
    query = db.query(models.Candidate.batch_id, models.Candidate.status, func.count())
    if batch_id is not None:
        query = query.filter(models.Candidate.batch_id == batch_id)
    counts = defaultdict(lambda: dict.fromkeys(STATUSES, 0))
    for bid, status, n in query.group_by(models.Candidate.batch_id, models.Candidate.status):
        if status in counts[bid]:
            counts[bid][status] = n
    return counts


@router.post("/candidates/upload")
def upload_candidates(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    if not (file.filename or '').lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(
            status_code=400,
            detail="Please upload an Excel file (.xlsx or .xls)"
        )

    contents = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"File is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

    try:
        df = pd.read_excel(io.BytesIO(contents), dtype=str)
    except Exception:
        raise HTTPException(status_code=400, detail="Could not read that file as an Excel workbook.")

    # Be forgiving about header case / stray spaces: " Email " -> email.
    by_lower = {str(c).strip().lower(): c for c in df.columns}
    missing_columns = [c for c in REQUIRED_COLUMNS if c.lower() not in by_lower]

    if missing_columns:
        raise HTTPException(
            status_code=400,
            detail=f"Missing columns in Excel: {missing_columns}. Required: {REQUIRED_COLUMNS}"
        )

    df = df.rename(columns={by_lower[c.lower()]: c for c in REQUIRED_COLUMNS})

    # Validate every row up front and report problems instead of failing mid-way.
    rows = []
    invalid = []
    seen_in_sheet = set()
    duplicates_in_sheet = 0

    for position, record in enumerate(df.to_dict('records')):
        excel_row = position + 2  # header is row 1
        candidate_id = _cell(record['candidateId'])
        name = _cell(record['name'])
        email = _cell(record['email']).lower()
        phone = _cell(record['phone'])

        if not candidate_id and not name and not email and not phone:
            continue  # fully blank row

        problem = None
        if not candidate_id:
            problem = "missing candidateId"
        elif not name:
            problem = "missing name"
        elif not _EMAIL_RE.match(email):
            problem = f"invalid email {email!r}" if email else "missing email"

        if problem:
            invalid.append({"row": excel_row, "candidate_id": candidate_id or None, "reason": problem})
            continue

        if candidate_id in seen_in_sheet:
            duplicates_in_sheet += 1
            continue
        seen_in_sheet.add(candidate_id)

        rows.append({"candidate_id": candidate_id, "name": name, "email": email, "phone": phone or None})

    if not rows and not invalid:
        raise HTTPException(status_code=400, detail="The Excel file has no candidate rows.")

    existing_ids = set()
    ids = [r["candidate_id"] for r in rows]
    for start in range(0, len(ids), 1000):
        found = db.query(models.Candidate.candidate_id).filter(
            models.Candidate.candidate_id.in_(ids[start:start + 1000])
        ).all()
        existing_ids.update(row[0] for row in found)

    new_rows = [r for r in rows if r["candidate_id"] not in existing_ids]
    skipped_existing = len(rows) - len(new_rows)

    if not new_rows:
        raise HTTPException(
            status_code=400,
            detail=(
                "No new candidates to add: "
                f"{skipped_existing} already exist, {len(invalid)} invalid row(s), "
                f"{duplicates_in_sheet} repeated in the sheet."
            )
        )

    batch = models.Batch(
        filename=file.filename,
        total_candidates=len(new_rows),
        processed=0
    )
    db.add(batch)
    db.flush()

    CHUNK_SIZE = 1000
    for start in range(0, len(new_rows), CHUNK_SIZE):
        db.add_all([
            models.Candidate(**row, status='pending', batch_id=batch.id)
            for row in new_rows[start:start + CHUNK_SIZE]
        ])
        db.flush()

    batch.processed = len(new_rows)
    db.commit()
    db.refresh(batch)

    return {
        "message": "Excel uploaded successfully!",
        "batch_id": batch.id,
        "filename": file.filename,
        "total_in_excel": len(df),
        "candidates_added": len(new_rows),
        "candidates_skipped": skipped_existing + duplicates_in_sheet,
        "reason_skipped": "Duplicates — already in the database or repeated in the sheet",
        "invalid_rows": invalid[:50],
        "invalid_count": len(invalid)
    }


@router.get("/candidates")
def get_candidates(
    status: str = None,
    batch_id: int = None,
    db: Session = Depends(get_db)
):
    query = db.query(models.Candidate)

    if status:
        query = query.filter(models.Candidate.status == status)

    if batch_id:
        query = query.filter(models.Candidate.batch_id == batch_id)

    candidates = query.order_by(models.Candidate.candidate_id).all()

    required_docs = get_setting_value(db, 'required_documents', []) or []
    required_set = set(required_docs)

    # One query for every candidate's documents instead of one per candidate.
    received = defaultdict(set)
    for candidate_id, doc_type in db.query(models.Document.candidate_id, models.Document.doc_type):
        received[candidate_id].add(doc_type)

    result_candidates = []

    for c in candidates:
        types = received.get(c.candidate_id, set())
        matched_count = len(required_set & types) if required_set else len(types)

        result_candidates.append({
            "candidate_id": c.candidate_id,
            "name": c.name,
            "email": c.email,
            "phone": c.phone,
            "status": c.status,
            "batch_id": c.batch_id,
            "created_at": c.created_at,
            "docs_received": matched_count,
            "docs_required": len(required_docs)
        })

    return {
        "total": len(candidates),
        "candidates": result_candidates
    }



@router.get("/candidates/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    counts = Counter(dict(
        db.query(models.Candidate.status, func.count()).group_by(models.Candidate.status).all()
    ))

    return {
        "total_candidates": sum(counts.values()),
        "fully_verified": counts['completed'],
        "partial_documents": counts['partial'],
        "missing_documents": counts['failed'],
        "pending": counts['pending'],
        "processing": counts['processing']
    }


def _verify_google_connection(db: Session, admin_email):
    """Fail fast (before any work is queued) if Gmail/Drive can't be reached."""
    try:
        email = get_admin_email(db, admin_email)
        get_gmail_service(email, db)
    except GoogleAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return email


@router.post("/candidates/{candidate_id}/process")
def start_candidate_processing(
    candidate_id: str,
    admin_email: str = None,
    db: Session = Depends(get_db)
):
    """Start the full verification pipeline for one candidate"""
    candidate = db.query(models.Candidate).filter(
        models.Candidate.candidate_id == candidate_id
    ).first()

    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    if candidate.status == 'processing':
        raise HTTPException(status_code=409, detail="This candidate is already being processed.")

    return process_candidate(candidate_id, admin_email, db)


def run_batch_in_background(batch_id: int, admin_email: str):
    """
    This function runs separately from the main request.
    WHY a new db session here? Because the original request's
    db session closes once we respond to the user — we need
    our own fresh session for this background work.
    """
    db = SessionLocal()

    try:
        batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
        batch.processing_status = 'processing'
        db.commit()

        result = process_batch(batch_id, admin_email, db)

        # An error here means it stopped early (e.g. Google credentials expired);
        # the remaining candidates stay pending so the batch can be resumed.
        batch.processing_status = 'failed' if result.get('error') else 'completed'
        db.commit()

    except Exception as e:
        print(f"❌ Background batch processing error: {e}")
        db.rollback()
        batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
        if batch:
            batch.processing_status = 'failed'
            db.commit()
    finally:
        db.close()


@router.post("/candidates/batch/{batch_id}/process")
def start_batch_processing(
    batch_id: int,
    background_tasks: BackgroundTasks,
    admin_email: str = None,
    db: Session = Depends(get_db)
):
    """
    Starts verification for all pending candidates in a batch.
    Returns immediately — actual processing happens in background.
    User can check progress anytime via the status route below.
    """

    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()

    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    if batch.processing_status == 'processing':
        raise HTTPException(status_code=409, detail="This batch is already being processed.")

    pending = db.query(models.Candidate).filter(
        models.Candidate.batch_id == batch_id,
        models.Candidate.status == 'pending'
    ).count()

    if not pending:
        raise HTTPException(status_code=400, detail="No pending candidates in this batch.")

    admin_email = _verify_google_connection(db, admin_email)

    # Mark it now, not when the task starts, so a double click can't launch two runs.
    batch.processing_status = 'processing'
    db.commit()

    background_tasks.add_task(run_batch_in_background, batch_id, admin_email)

    return {
        "message": "Batch processing started in background",
        "batch_id": batch_id,
        "status": "processing"
    }


@router.get("/candidates/batch/{batch_id}/status")
def get_batch_status(batch_id: int, db: Session = Depends(get_db)):

    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()

    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    status_counts = _status_counts(db, batch_id)[batch_id]

    total = sum(status_counts.values())
    finished = status_counts['completed'] + status_counts['partial'] + status_counts['failed']

    progress_percent = round((finished / total) * 100, 1) if total > 0 else 0

    return {
        "batch_id": batch_id,
        "filename": batch.filename,
        "processing_status": batch.processing_status,
        "total_candidates": total,
        "finished_count": finished,
        "progress_percent": progress_percent,
        "status_breakdown": status_counts
    }


@router.get("/batches")
def get_batch_history(db: Session = Depends(get_db)):

    batches = db.query(models.Batch).order_by(models.Batch.created_at.desc(), models.Batch.id.desc()).all()
    counts = _status_counts(db)

    result = []

    for batch in batches:
        status_counts = counts[batch.id] if batch.id in counts else dict.fromkeys(STATUSES, 0)

        result.append({
            "batch_id": batch.id,
            "filename": batch.filename,
            "total_candidates": batch.total_candidates,
            "processing_status": batch.processing_status,
            "created_at": batch.created_at,
            "status_breakdown": status_counts
        })

    return {
        "total_batches": len(result),
        "batches": result
    }


@router.get("/batches/{batch_id}")
def get_batch_detail(batch_id: int, db: Session = Depends(get_db)):

    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()

    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    candidates = db.query(models.Candidate).filter(
        models.Candidate.batch_id == batch_id
    ).order_by(models.Candidate.candidate_id).all()

    return {
        "batch_id": batch.id,
        "filename": batch.filename,
        "created_at": batch.created_at,
        "processing_status": batch.processing_status,
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                "name": c.name,
                "email": c.email,
                "status": c.status
            }
            for c in candidates
        ]
    }


@router.get("/candidates/review-queue")
def get_review_queue(db: Session = Depends(get_db)):

    failed_candidates = db.query(models.Candidate).filter(
        models.Candidate.status == 'failed'
    ).order_by(models.Candidate.candidate_id).all()

    return {
        "total_failed": len(failed_candidates),
        "candidates": [
            {
                "candidate_id": c.candidate_id,
                "name": c.name,
                "email": c.email,
                "phone": c.phone,
                "batch_id": c.batch_id,
                "updated_at": c.updated_at
            }
            for c in failed_candidates
        ]
    }


@router.get("/candidates/{candidate_id}/detail")
def get_candidate_detail(candidate_id: str, db: Session = Depends(get_db)):

    candidate = db.query(models.Candidate).filter(
        models.Candidate.candidate_id == candidate_id
    ).first()

    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    documents = db.query(models.Document).filter(
        models.Document.candidate_id == candidate_id
    ).order_by(models.Document.received_at).all()

    required_docs = get_setting_value(db, 'required_documents', []) or []
    received_doc_types = {doc.doc_type for doc in documents}
    missing_docs = [doc for doc in required_docs if doc not in received_doc_types]

    return {
        "candidate_id": candidate.candidate_id,
        "name": candidate.name,
        "email": candidate.email,
        "phone": candidate.phone,
        "status": candidate.status,
        "batch_id": candidate.batch_id,
        "created_at": candidate.created_at,
        "documents": [
            {
                "doc_type": doc.doc_type,
                "category": doc.doc_category,
                "confidence": doc.ai_confidence,
                "drive_path": doc.drive_path,
                "drive_file_id": doc.drive_file_id,
                "status": doc.status,
                "received_at": doc.received_at
            }
            for doc in documents
        ],
        "required_documents": required_docs,
        "missing_documents": missing_docs
    }


@router.post("/candidates/{candidate_id}/retry")
def retry_candidate(
    candidate_id: str,
    admin_email: str = None,
    db: Session = Depends(get_db)
):
    candidate = db.query(models.Candidate).filter(
        models.Candidate.candidate_id == candidate_id
    ).first()

    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    if candidate.status != 'failed':
        raise HTTPException(
            status_code=400,
            detail=f"Retry only allowed for failed candidates. Current status: {candidate.status}"
        )

    return process_candidate(candidate_id, admin_email, db)
