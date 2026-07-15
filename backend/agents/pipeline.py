import os
import shutil
from datetime import datetime
from sqlalchemy.orm import Session

from backend.services.gmail_service import get_gmail_service, get_candidate_documents
from backend.services.drive_service import (
    get_drive_service, get_main_bgv_folder, create_candidate_folder_structure,
    upload_file_to_drive, find_candidate_pdf_in_source_drive, download_file_from_drive,
    delete_file_from_drive
)
from backend.api.settings import get_setting_value
from backend.agents.classifier import process_pdf, get_suggested_filename, detect_and_split_documents
from backend import models


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

    candidate.status = 'processing'
    db.commit()

    print(f"📋 Processing: {candidate.name} ({candidate.email})")

    try:
        gmail_service = get_gmail_service(admin_email, db)
        drive_service = get_drive_service(admin_email, db)
        print("✅ Connected to Gmail and Drive")

    except Exception as e:
        candidate.status = 'failed'
        db.commit()
        return {
            "success": False,
            "error": f"Could not connect to Gmail/Drive: {str(e)}"
        }

    try:
        pdf_paths = get_candidate_documents(
            gmail_service,
            candidate.email,
            candidate_id
        )

        if not pdf_paths:
            print(f"📭 No Gmail documents for {candidate.email}, checking source Drive folder...")

            source_folder_id = get_setting_value(db, 'source_drive_folder_id', None)

            if source_folder_id:
                file_id = find_candidate_pdf_in_source_drive(
                    drive_service,
                    candidate.email,
                    source_folder_id
                )

                if file_id:
                    os.makedirs(f"/tmp/bgv/{candidate_id}", exist_ok=True)
                    local_path = f"/tmp/bgv/{candidate_id}/source_document.pdf"
                    download_file_from_drive(drive_service, file_id, local_path)
                    pdf_paths = [local_path]

        if not pdf_paths:
            print(f"❌ No PDFs found for {candidate.email} in Gmail or source Drive")
            candidate.status = 'failed'
            db.commit()
            return {
                "success": False,
                "candidate_id": candidate_id,
                "error": "No documents found in Gmail or source Drive folder"
            }

        print(f"📥 Found {len(pdf_paths)} PDFs")
        print("PDF paths in order:", pdf_paths)

    except Exception as e:
        candidate.status = 'failed'
        db.commit()
        return {
            "success": False,
            "error": f"Gmail error: {str(e)}"
        }

    try:
        if candidate.drive_folder_data:
            folder_structure = candidate.drive_folder_data
            print(f"📁 Using cached Drive folder structure")
        else:
            destination_folder_name = get_setting_value(db, 'destination_folder_name', 'Candidate_Documents')

            main_folder_id = get_main_bgv_folder(drive_service, destination_folder_name)

            folder_structure = create_candidate_folder_structure(
                drive_service,
                candidate_id,
                candidate.name,
                main_folder_id
            )

            candidate.drive_folder_data = folder_structure
            db.commit()

            print(f"📁 Drive folders created and cached")

    except Exception as e:
        candidate.status = 'failed'
        db.commit()
        return {
            "success": False,
            "error": f"Drive folder error: {str(e)}"
        }

    processed_docs = []
    all_documents_to_process = []

    for pdf_path in pdf_paths:
        candidate_temp_dir = f"/tmp/bgv/{candidate_id}/split_{os.path.basename(pdf_path).replace('.pdf', '')}"

        try:
            split_results = detect_and_split_documents(pdf_path, candidate_temp_dir)
            all_documents_to_process.extend(split_results)
        except Exception as e:
            print(f"❌ Error splitting {pdf_path}: {e}")
            processed_docs.append({
                "pdf": pdf_path,
                "status": "failed",
                "error": f"Splitting error: {str(e)}"
            })

    for doc_info in all_documents_to_process:
        try:
            pdf_path = doc_info["path"]
            print(f"\n📄 Processing: {os.path.basename(pdf_path)}")

            category = doc_info.get('category', 'unknown')
            doc_type = doc_info.get('doc_type', 'unknown')
            confidence = doc_info.get('confidence', 'low')

            if doc_type == 'unknown' or category == 'unknown':
                print(f"⏭️ Skipping unclassified page (not a recognized document type)")
                processed_docs.append({
                    "doc_type": "unknown",
                    "status": "skipped",
                    "reason": "could not classify this page as a known document type"
                })
                continue

            suggested_filename = get_suggested_filename(doc_type)

            existing_doc = db.query(models.Document).filter(
                models.Document.candidate_id == candidate_id,
                models.Document.doc_type == doc_type
            ).first()

            if existing_doc:
                confidence_rank = {"low": 1, "medium": 2, "high": 3}
                old_rank = confidence_rank.get(existing_doc.ai_confidence, 0)
                new_rank = confidence_rank.get(confidence, 0)

                if new_rank <= old_rank:
                    print(f"⚠️ Document {doc_type} already exists for {candidate_id} "
                          f"with equal/better confidence ({existing_doc.ai_confidence}), skipping")
                    processed_docs.append({
                        "doc_type": doc_type,
                        "status": "skipped",
                        "reason": "duplicate (existing confidence is equal or better)"
                    })
                    continue
                else:
                    print(f"🔄 Replacing {doc_type} for {candidate_id}: "
                          f"{existing_doc.ai_confidence} → {confidence} confidence")

                    delete_file_from_drive(drive_service, existing_doc.drive_file_id)

                    db.delete(existing_doc)
                    db.commit()

            category_folder_id = folder_structure['category_folders'].get(
                category,
                folder_structure['category_folders'].get('photo_personal')
            )
            category_folder_name = folder_structure['category_folder_names'].get(
                category,
                folder_structure['category_folder_names'].get('photo_personal')
            )

            if not category_folder_id:
                print(f"⚠️ No folder found for category: {category}")
                continue

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
            print(f"❌ Error processing {pdf_path}: {e}")
            processed_docs.append({
                "pdf": pdf_path,
                "status": "failed",
                "error": str(e)
            })

    required_setting = db.query(models.Settings).filter(
        models.Settings.key == 'required_documents'
    ).first()

    received_docs = db.query(models.Document).filter(
        models.Document.candidate_id == candidate_id
    ).all()

    received_types = set([doc.doc_type for doc in received_docs])

    if required_setting and required_setting.value:
        required_types = set(required_setting.value)

        if required_types.issubset(received_types):
            candidate.status = 'completed'
        elif len(received_types.intersection(required_types)) == 0:
            candidate.status = 'failed'
        else:
            candidate.status = 'partial'
    else:
        if received_types:
            candidate.status = 'partial'
        else:
            candidate.status = 'failed'

    db.commit()

    print(f"\n✅ Pipeline complete for {candidate_id}")
    print(f"📊 Status: {candidate.status}")

    temp_folder = f"/tmp/bgv/{candidate_id}"
    if os.path.exists(temp_folder):
        shutil.rmtree(temp_folder)
        print(f"🗑️ Cleaned up temp files")

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
    ).all()

    if not candidates:
        return {
            "success": False,
            "error": "No pending candidates found in this batch"
        }

    print(f"📋 Found {len(candidates)} candidates to process")

    results = []

    for candidate in candidates:
        result = process_candidate(
            candidate.candidate_id,
            admin_email,
            db
        )
        results.append(result)

    successful = len([r for r in results if r.get('success')])
    failed = len([r for r in results if not r.get('success')])

    return {
        "success": True,
        "batch_id": batch_id,
        "total_processed": len(results),
        "successful": successful,
        "failed": failed,
        "results": results
    }