
import os
import io
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
import pandas as pd

from sqlalchemy.orm import Session

from datetime import datetime

from backend.database import get_db

from backend import models

router = APIRouter()

@router.post("/candidates/upload")
async def upload_candidates(
    
    file: UploadFile = File(...),
    
    
    db: Session = Depends(get_db)
):
    
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(
            status_code=400,
            # This message goes back to the user
            detail="Please upload an Excel file (.xlsx or .xls)"
        )
    
    contents = await file.read()
    excel_data = io.BytesIO(contents)

    df = pd.read_excel(excel_data)
    required_columns = ['candidateId', 'name', 'email', 'phone']
    
    missing_columns = [
        col for col in required_columns 
        if col not in df.columns
    ]
    
    if missing_columns:
        raise HTTPException(
            status_code=400,
            detail=f"Missing columns in Excel: {missing_columns}. Required: {required_columns}"
        )
    
    
    batch = models.Batch(
        filename=file.filename,           
        total_candidates=len(df),         
        processed=0                        
    )
    db.add(batch)       
    db.commit()         
    db.refresh(batch)   
    
 
    CHUNK_SIZE = 1000
    total_added = 0
    total_skipped = 0
    
   
    for chunk_start in range(0, len(df), CHUNK_SIZE):
        
     
        chunk = df[chunk_start : chunk_start + CHUNK_SIZE]
        
        for _, row in chunk.iterrows():
            
           
            candidate_id = str(row['candidateId']).strip()
          
            existing = db.query(models.Candidate).filter(
                models.Candidate.candidate_id == candidate_id
            ).first()
            
            if existing:
               
                total_skipped += 1
                continue 
         
            new_candidate = models.Candidate(
                candidate_id=candidate_id,
                name=str(row['name']).strip(),
                email=str(row['email']).strip().lower(),
                phone=str(row['phone']).strip(),
                status='pending',    
                batch_id=batch.id   
            )
            
            db.add(new_candidate)
            total_added += 1
        
   
        db.commit()
        
       
        batch.processed = chunk_start + len(chunk)
        db.commit()
    
  
    return {
        "message": "Excel uploaded successfully!",
        "batch_id": batch.id,
        "filename": file.filename,
        "total_in_excel": len(df),
        "candidates_added": total_added,
        "candidates_skipped": total_skipped,
        "reason_skipped": "Duplicates — already existed in database"
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
    
  
    query = query.order_by(models.Candidate.candidate_id)
    
    candidates = query.all()
    
    required_setting = db.query(models.Settings).filter(
        models.Settings.key == 'required_documents'
    ).first()
    required_docs = required_setting.value if required_setting and required_setting.value else []
    total_required = len(required_docs) if required_docs else 1
    
    result_candidates = []
    
    for c in candidates:
        
        received_docs = db.query(models.Document).filter(
            models.Document.candidate_id == c.candidate_id
        ).all()
        received_types = set([doc.doc_type for doc in received_docs])
        
        if required_docs:
            matched_count = len(set(required_docs).intersection(received_types))
        else:
            matched_count = len(received_types)
        
        result_candidates.append({
            "candidate_id": c.candidate_id,
            "name": c.name,
            "email": c.email,
            "phone": c.phone,
            "status": c.status,
            "batch_id": c.batch_id,
            "created_at": c.created_at,
            "docs_received": matched_count,
            "docs_required": total_required
        })
    
    return {
        "total": len(candidates),
        "candidates": result_candidates
    }



@router.get("/candidates/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    
    
    total = db.query(models.Candidate).count()
    
    completed = db.query(models.Candidate).filter(
        models.Candidate.status == 'completed'
    ).count()
    
    partial = db.query(models.Candidate).filter(
        models.Candidate.status == 'partial'
    ).count()
    
    failed = db.query(models.Candidate).filter(
        models.Candidate.status == 'failed'
    ).count()
    
    pending = db.query(models.Candidate).filter(
        models.Candidate.status == 'pending'
    ).count()
    
    return {
        "total_candidates": total,
        "fully_verified": completed,
        "partial_documents": partial,
        "missing_documents": failed,
        "pending": pending
    }

from backend.agents.pipeline import process_candidate, process_batch


@router.post("/candidates/{candidate_id}/process")
def start_candidate_processing(
    candidate_id: str,
    admin_email: str,
    db: Session = Depends(get_db)
):
    """Start the full verification pipeline for one candidate"""
    
    result = process_candidate(candidate_id, admin_email, db)
    return result



from fastapi import BackgroundTasks
from backend.database import SessionLocal


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
        
       
        process_batch(batch_id, admin_email, db)
        
        batch.processing_status = 'completed'
        db.commit()
        
    except Exception as e:
        print(f"❌ Background batch processing error: {e}")
        batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
        if batch:
            batch.processing_status = 'failed'
            db.commit()
    finally:
        db.close()


@router.post("/candidates/batch/{batch_id}/process")
def start_batch_processing(
    batch_id: int,
    admin_email: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Starts verification for all candidates in a batch.
    Returns immediately — actual processing happens in background.
    User can check progress anytime via the status route below.
    """
    
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    
 
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
    
    candidates = db.query(models.Candidate).filter(
        models.Candidate.batch_id == batch_id
    ).all()
    
    status_counts = {
        'pending': 0,
        'processing': 0,
        'completed': 0,
        'partial': 0,
        'failed': 0
    }
    
    for c in candidates:
        if c.status in status_counts:
            status_counts[c.status] += 1
    
    total = len(candidates)
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
    
    batches = db.query(models.Batch).order_by(models.Batch.created_at.desc()).all()
    
    result = []
    
    for batch in batches:
        candidates = db.query(models.Candidate).filter(
            models.Candidate.batch_id == batch.id
        ).all()
        
        status_counts = {
            'pending': 0, 'processing': 0,
            'completed': 0, 'partial': 0, 'failed': 0
        }
        
        for c in candidates:
            if c.status in status_counts:
                status_counts[c.status] += 1
        
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


@router.get("/candidates/{candidate_id}/detail")
def get_candidate_detail(candidate_id: str, db: Session = Depends(get_db)):
    
    candidate = db.query(models.Candidate).filter(
        models.Candidate.candidate_id == candidate_id
    ).first()
    
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    
    documents = db.query(models.Document).filter(
        models.Document.candidate_id == candidate_id
    ).all()
    
    required_setting = db.query(models.Settings).filter(
        models.Settings.key == 'required_documents'
    ).first()
    
    required_docs = required_setting.value if required_setting and required_setting.value else []
    received_doc_types = [doc.doc_type for doc in documents]
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



@router.post("/candidates/{candidate_id}/retry")
def retry_candidate(
    candidate_id: str,
    admin_email: str,
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
    
    result = process_candidate(candidate_id, admin_email, db)
    return result