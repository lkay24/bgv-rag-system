import os
import shutil
from sqlalchemy.orm import Session

from backend.services.gmail_service import get_gmail_service, get_candidate_documents
from backend.services.drive_service import (
    get_drive_service, get_main_bgv_folder, create_candidate_folder_structure,
    upload_file_to_drive, find_candidate_files_in_source_drive, download_file_from_drive,
    delete_file_from_drive, DEFAULT_FOLDER_FORMAT
)
from backend.services.file_utils import candidate_workdir
from backend.services.google_auth import GoogleAuthError, get_admin_email
from backend.api.settings import get_setting_value
from backend.agents.classifier import get_suggested_filename, detect_and_split_documents, CONFIDENCE_RANK
from backend import models


def compute_candidate_status(db: Session, candidate_id: str) -> str:
    """completed / partial / failed, based on the documents on file and the required list."""
    received_types = {
        doc.doc_type for doc in db.query(models.Document).filter(
            models.Document.candidate_id == candidate_id
        ).all()
    }
    required_types = set(get_setting_value(db, 'required_documents', []) or [])

    if required_types:
        if required_types.issubset(received_types):
            return 'completed'
        if not required_types & received_types:
            return 'failed'
        return 'partial'

    return 'partial' if received_types else 'failed'


def _folder_for(folder_structure: dict, category: str):
    """Drive folder id + display path for a category, falling back to photo_personal."""
    folders = folder_structure['category_folders']
    names = folder_structure['category_folder_names']
    key = category if category in folders else 'photo_personal'
    return folders.get(key), names.get(key)


def process_candidate(
    candidate_id: str,
    admin_email: str,
    db: Session
) -> dict:

    print(f"\n{'='*50}")
    print(f"🚀 Starting pipeline for candidate: {candidate_id}")
    print(f"{'='*50}")

    candidate = db.query(models.Candidate).filter(
        models.Candidate.candidate_id == candidate_id
    ).first()

    if not candidate:
        return {
            "success": False,
            "error": f"Candidate {candidate_id} not found in database"
        }

    previous_status = candidate.status
    candidate.status = 'processing'
    db.commit()

    print(f"📋 Processing: {candidate.name}")

    workdir = candidate_workdir(candidate_id)

    try:
        return _run_pipeline(candidate, admin_email, db, workdir, previous_status)
    except Exception as e:
        # Never leave a candidate stuck in 'processing' if something unexpected blows up.
        db.rollback()
        print(f"❌ Unexpected pipeline error for {candidate_id}: {e}")
        candidate.status = 'failed'
        db.commit()
        return {"success": False, "candidate_id": candidate_id, "error": f"Unexpected error: {e}"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_pipeline(candidate, admin_email, db, workdir, previous_status) -> dict:
    candidate_id = candidate.candidate_id

    try:
        admin_email = get_admin_email(db, admin_email)
        gmail_service = get_gmail_service(admin_email, db)
        drive_service = get_drive_service(admin_email, db)
        print("✅ Connected to Gmail and Drive")

    except Exception as e:
        # A connection problem says nothing about the candidate, so don't push them
        # into the failed/review queue - put them back where they were.
        candidate.status = previous_status if previous_status != 'processing' else 'pending'
        db.commit()
        return {
            "success": False,
            "candidate_id": candidate_id,
            "auth_error": isinstance(e, GoogleAuthError),
            "error": f"Could not connect to Gmail/Drive: {str(e)}"
        }

    try:
        os.makedirs(workdir, exist_ok=True)
        pdf_paths = get_candidate_documents(
            gmail_service,
            candidate.email,
            candidate_id,
            workdir
        )

        if not pdf_paths:
            print(f"📭 No Gmail documents for {candidate.email}, checking source Drive folder...")

            source_folder_id = get_setting_value(db, 'source_drive_folder_id', None)

            if source_folder_id:
                file_ids = find_candidate_files_in_source_drive(
                    drive_service,
                    candidate.email,
                    source_folder_id
                )

                for n, file_id in enumerate(file_ids):
                    local_path = os.path.join(workdir, f"source_document_{n}.pdf")
                    download_file_from_drive(drive_service, file_id, local_path)
                    pdf_paths.append(local_path)

        if not pdf_paths:
            print(f"❌ No PDFs found for {candidate.email} in Gmail or source Drive")
            # Documents from an earlier run still count: re-checking must not downgrade
            # a candidate just because nothing new arrived.
            candidate.status = compute_candidate_status(db, candidate_id)
            db.commit()
            if candidate.status == 'failed':
                return {
                    "success": False,
                    "candidate_id": candidate_id,
                    "error": "No documents found in Gmail or source Drive folder"
                }
            return {
                "success": True,
                "candidate_id": candidate_id,
                "candidate_name": candidate.name,
                "status": candidate.status,
                "documents_processed": [],
                "total_docs": 0,
                "note": "No new documents found; status unchanged."
            }

        print(f"📥 Found {len(pdf_paths)} PDFs")

    except Exception as e:
        candidate.status = 'failed'
        db.commit()
        return {
            "success": False,
            "candidate_id": candidate_id,
            "error": f"Gmail error: {str(e)}"
        }

    try:
        if candidate.drive_folder_data:
            folder_structure = candidate.drive_folder_data
            print(f"📁 Using cached Drive folder structure")
        else:
            destination_folder_name = get_setting_value(db, 'destination_folder_name', 'Candidate_Documents')
            folder_format = get_setting_value(db, 'folder_name_format', DEFAULT_FOLDER_FORMAT)

            main_folder_id = get_main_bgv_folder(drive_service, destination_folder_name)

            folder_structure = create_candidate_folder_structure(
                drive_service,
                candidate_id,
                candidate.name,
                main_folder_id,
                folder_format
            )

            candidate.drive_folder_data = folder_structure
            db.commit()

            print(f"📁 Drive folders created and cached")

    except Exception as e:
        candidate.status = 'failed'
        db.commit()
        return {
            "success": False,
            "candidate_id": candidate_id,
            "error": f"Drive folder error: {str(e)}"
        }

    processed_docs = []
    all_documents_to_process = []

    for n, pdf_path in enumerate(pdf_paths):
        split_dir = os.path.join(workdir, f"split_{n}")

        try:
            split_results = detect_and_split_documents(pdf_path, split_dir)
            all_documents_to_process.extend(split_results)
        except Exception as e:
            print(f"❌ Error splitting {pdf_path}: {e}")
            processed_docs.append({
                "pdf": os.path.basename(pdf_path),
                "status": "failed",
                "error": f"Splitting error: {str(e)}"
            })

    for doc_info in all_documents_to_process:
        pdf_path = doc_info["path"]
        try:
            print(f"\n📄 Processing: {os.path.basename(pdf_path)}")

            category = doc_info.get('category', 'unknown')
            doc_type = doc_info.get('doc_type', 'unknown')
            confidence = doc_info.get('confidence', 'low')

            if doc_type == 'unknown' or category == 'unknown':
                print(f"⏭️ Skipping unclassified document (not a recognized document type)")
                processed_docs.append({
                    "doc_type": "unknown",
                    "status": "skipped",
                    "reason": "could not classify this document as a known document type"
                })
                continue

            suggested_filename = get_suggested_filename(doc_type)

            existing_doc = db.query(models.Document).filter(
                models.Document.candidate_id == candidate_id,
                models.Document.doc_type == doc_type
            ).first()

            if existing_doc:
                old_rank = CONFIDENCE_RANK.get(existing_doc.ai_confidence, -1)
                new_rank = CONFIDENCE_RANK.get(confidence, -1)

                if new_rank <= old_rank:
                    print(f"⚠️ Document {doc_type} already exists for {candidate_id} "
                          f"with equal/better confidence ({existing_doc.ai_confidence}), skipping")
                    processed_docs.append({
                        "doc_type": doc_type,
                        "status": "skipped",
                        "reason": "duplicate (existing confidence is equal or better)"
                    })
                    continue

                print(f"🔄 Replacing {doc_type} for {candidate_id}: "
                      f"{existing_doc.ai_confidence} → {confidence} confidence")

            category_folder_id, category_folder_name = _folder_for(folder_structure, category)

            if not category_folder_id:
                print(f"⚠️ No folder found for category: {category}")
                processed_docs.append({
                    "doc_type": doc_type,
                    "status": "failed",
                    "error": f"No Drive folder for category {category}"
                })
                continue

            # Upload first and only then retire the old copy, so a failed upload can
            # never leave the candidate with no file at all.
            upload_result = upload_file_to_drive(
                drive_service,
                pdf_path,
                suggested_filename,
                category_folder_id
            )

            new_doc = models.Document(
                candidate_id=candidate_id,
                doc_category=category,
                doc_type=doc_type,
                drive_file_id=upload_result['file_id'],
                drive_path=f"{category_folder_name}/{suggested_filename}",
                ai_confidence=confidence,
                status='verified' if confidence != 'low' else 'pending'
            )
            db.add(new_doc)

            if existing_doc:
                old_file_id = existing_doc.drive_file_id
                db.delete(existing_doc)
                db.commit()
                delete_file_from_drive(drive_service, old_file_id)
            else:
                db.commit()

            processed_docs.append({
                "doc_type": doc_type,
                "category": category,
                "confidence": confidence,
                "drive_path": new_doc.drive_path,
                "status": "uploaded"
            })

            print(f"✅ Uploaded {suggested_filename} to {category} folder")

        except Exception as e:
            db.rollback()
            print(f"❌ Error processing {pdf_path}: {e}")
            processed_docs.append({
                "pdf": os.path.basename(pdf_path),
                "status": "failed",
                "error": str(e)
            })

    candidate.status = compute_candidate_status(db, candidate_id)
    db.commit()

    print(f"\n✅ Pipeline complete for {candidate_id}")
    print(f"📊 Status: {candidate.status}")

    return {
        "success": True,
        "candidate_id": candidate_id,
        "candidate_name": candidate.name,
        "status": candidate.status,
        "documents_processed": processed_docs,
        "total_docs": len(processed_docs)
    }


def process_batch(
    batch_id: int,
    admin_email: str,
    db: Session
) -> dict:

    print(f"\n🚀 Starting batch processing for batch {batch_id}")

    candidates = db.query(models.Candidate).filter(
        models.Candidate.batch_id == batch_id,
        models.Candidate.status == 'pending'
    ).order_by(models.Candidate.id).all()

    if not candidates:
        return {
            "success": False,
            "error": "No pending candidates found in this batch"
        }

    print(f"📋 Found {len(candidates)} candidates to process")

    results = []
    aborted = None

    for candidate in candidates:
        result = process_candidate(
            candidate.candidate_id,
            admin_email,
            db
        )
        results.append(result)

        # Expired/revoked Google credentials will fail every remaining candidate the same
        # way - stop now and leave them pending so the batch can be resumed after reconnecting.
        if result.get('auth_error'):
            aborted = result['error']
            print(f"⛔ Aborting batch {batch_id}: {aborted}")
            break

    successful = len([r for r in results if r.get('success')])
    failed = len([r for r in results if not r.get('success')])

    return {
        "success": aborted is None,
        "error": aborted,
        "batch_id": batch_id,
        "total_processed": len(results),
        "successful": successful,
        "failed": failed,
        "results": results
    }
