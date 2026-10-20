
import io
from datetime import datetime
from typing import List

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.config import MAX_UPLOAD_BYTES
from backend.database import get_db
from backend import models
from backend.services.drive_service import DEFAULT_FOLDER_FORMAT, validate_folder_format
from backend.taxonomy import ALL_DOC_TYPES, DOCUMENT_TYPES, normalize_doc_type

router = APIRouter()



class RequiredDocsUpdate(BaseModel):
   
    required_documents: List[str]


class FolderConfigUpdate(BaseModel):
    folder_name_format: str
    destination_folder_name: str



def clean_required_documents(values) -> list:
    """Normalise, de-duplicate and validate a required-documents list."""
    cleaned = []
    for value in values:
        doc_type = normalize_doc_type(value)
        if doc_type and doc_type not in cleaned:
            cleaned.append(doc_type)

    unknown = [d for d in cleaned if d not in ALL_DOC_TYPES]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown document type(s): {', '.join(unknown)}. "
                "The classifier can only recognise the types listed under /settings/document-types, "
                "so a candidate could never satisfy these."
            )
        )
    return cleaned


def get_setting_value(db: Session, key: str, default=None):
    setting = db.query(models.Settings).filter(
        models.Settings.key == key
    ).first()
    
    if setting:
        return setting.value
    return default


def set_setting_value(db: Session, key: str, value):
    setting = db.query(models.Settings).filter(
        models.Settings.key == key
    ).first()
    
    if setting:
        setting.value = value
        setting.updated_at = datetime.utcnow()
    else:
        setting = models.Settings(key=key, value=value)
        db.add(setting)
    
    db.commit()
    return setting



@router.get("/settings")
def get_all_settings(db: Session = Depends(get_db)):
    
    required_docs = get_setting_value(db, 'required_documents', [])
    folder_format = get_setting_value(db, 'folder_name_format', DEFAULT_FOLDER_FORMAT)
    destination_folder = get_setting_value(db, 'destination_folder_name', 'Candidate_Documents')
    source_drive_folder_id = get_setting_value(db, 'source_drive_folder_id', None)
    
    return {
        "required_documents": required_docs,
        "folder_name_format": folder_format,
        "destination_folder_name": destination_folder,
        "source_drive_folder_id": source_drive_folder_id
    }



@router.post("/settings/required-documents")
def update_required_documents(
    data: RequiredDocsUpdate,
    db: Session = Depends(get_db)
):
    required = clean_required_documents(data.required_documents)
    set_setting_value(db, 'required_documents', required)

    return {
        "message": "Required documents updated successfully",
        "required_documents": required
    }



@router.post("/settings/folder-config")
def update_folder_config(
    data: FolderConfigUpdate,
    db: Session = Depends(get_db)
):
    folder_format = data.folder_name_format.strip()
    destination = data.destination_folder_name.strip()

    error = validate_folder_format(folder_format)
    if error:
        raise HTTPException(status_code=400, detail=error)
    if not destination:
        raise HTTPException(status_code=400, detail="Destination folder name cannot be empty.")

    set_setting_value(db, 'folder_name_format', folder_format)
    set_setting_value(db, 'destination_folder_name', destination)

    return {
        "message": "Folder configuration updated successfully. It applies to candidates processed from now on.",
        "folder_name_format": folder_format,
        "destination_folder_name": destination
    }


@router.get("/settings/document-types")
def get_document_types():
    return DOCUMENT_TYPES


class SourceDriveUpdate(BaseModel):
    source_drive_folder_id: str


@router.post("/settings/source-drive")
def update_source_drive(data: SourceDriveUpdate, db: Session = Depends(get_db)):
    folder_id = data.source_drive_folder_id.strip()
    set_setting_value(db, 'source_drive_folder_id', folder_id or None)

    return {
        "message": "Source Drive folder updated successfully",
        "source_drive_folder_id": folder_id or None
    }


@router.post("/settings/required-documents/upload")
def upload_required_documents_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    if not (file.filename or '').lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Please upload an Excel file (.xlsx or .xls)")

    contents = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File is too large.")

    try:
        df = pd.read_excel(io.BytesIO(contents), dtype=str)
    except Exception:
        raise HTTPException(status_code=400, detail="Could not read that file as an Excel workbook.")

    if df.empty and not len(df.columns):
        raise HTTPException(status_code=400, detail="The Excel file is empty.")

    column = df['doc_type'] if 'doc_type' in df.columns else df.iloc[:, 0]
    values = column.dropna().astype(str).tolist()
    # A headerless sheet would have its first document swallowed as the column header.
    if 'doc_type' not in df.columns and normalize_doc_type(df.columns[0]) in ALL_DOC_TYPES:
        values.insert(0, str(df.columns[0]))
    required = clean_required_documents(values)

    set_setting_value(db, 'required_documents', required)

    return {
        "message": "Required documents list uploaded successfully",
        "required_documents": required
    }
