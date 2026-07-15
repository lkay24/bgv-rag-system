
import pandas as pd
import io
from fastapi import UploadFile, File
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List

from backend.database import get_db
from backend import models

router = APIRouter()



class RequiredDocsUpdate(BaseModel):
   
    required_documents: List[str]


class FolderConfigUpdate(BaseModel):
    folder_name_format: str
    destination_folder_name: str



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
    else:
        
        setting = models.Settings(key=key, value=value)
        db.add(setting)
    
    db.commit()
    return setting



@router.get("/settings")
def get_all_settings(db: Session = Depends(get_db)):
    
    required_docs = get_setting_value(db, 'required_documents', [])
    folder_format = get_setting_value(db, 'folder_name_format', '{candidate_id}-{name}-{date}')
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
    set_setting_value(db, 'required_documents', data.required_documents)
    
    return {
        "message": "Required documents updated successfully",
        "required_documents": data.required_documents
    }



@router.post("/settings/folder-config")
def update_folder_config(
    data: FolderConfigUpdate,
    db: Session = Depends(get_db)
):
    set_setting_value(db, 'folder_name_format', data.folder_name_format)
    set_setting_value(db, 'destination_folder_name', data.destination_folder_name)
    
    return {
        "message": "Folder configuration updated successfully",
        "folder_name_format": data.folder_name_format,
        "destination_folder_name": data.destination_folder_name
    }


@router.get("/settings/document-types")
def get_document_types():
    
    
    return {
        "identity": [
            "aadhar_card", "pan_card", "passport", 
            "driving_license", "voter_id"
        ],
        "address": [
            "aadhar_card", "passport", "electricity_bill", 
            "water_bill", "gas_bill", "bank_statement", 
            "rental_agreement", "telephone_bill"
        ],
        "education": [
            "10th_marksheet", "10th_certificate", "12th_marksheet",
            "12th_certificate", "degree_certificate", 
            "provisional_certificate", "semester_marksheet",
            "consolidated_marksheet", "college_id_card", "pg_degree"
        ],
        "employment": [
            "offer_letter", "appointment_letter", "experience_letter",
            "relieving_letter", "promotion_letter", "salary_revision_letter",
            "payslip", "form_16", "employee_id_card", "joining_letter"
        ],
        "financial": [
            "pan_card", "form_16", "itr", "salary_slip", 
            "bank_statement", "cancelled_cheque"
        ],
        "photo_personal": [
            "photograph", "signature", "resume", "biodata"
        ],
        "professional": [
            "aws_certificate", "azure_certificate", "pmp_certificate",
            "google_cloud_certificate", "skill_certificate"
        ],
        "compliance": [
            "nda", "non_compete_agreement", "background_consent",
            "police_verification", "drug_test"
        ],
        "international": [
            "visa", "work_permit", "immigration_document", 
            "overseas_certificate"
        ],
        "criminal": [
            "police_verification_certificate", "criminal_background_check"
        ]
    }

class SourceDriveUpdate(BaseModel):
    source_drive_folder_id: str


@router.post("/settings/source-drive")
def update_source_drive(data: SourceDriveUpdate, db: Session = Depends(get_db)):
    set_setting_value(db, 'source_drive_folder_id', data.source_drive_folder_id)
    
    return {
        "message": "Source Drive folder updated successfully",
        "source_drive_folder_id": data.source_drive_folder_id
    }


@router.post("/settings/required-documents/upload")
async def upload_required_documents_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Please upload an Excel file (.xlsx or .xls)")
    
    contents = await file.read()
    df = pd.read_excel(io.BytesIO(contents))
    
    
    if 'doc_type' in df.columns:
        doc_list = df['doc_type'].dropna().astype(str).str.strip().tolist()
    else:
        
        doc_list = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
    
    set_setting_value(db, 'required_documents', doc_list)
    
    return {
        "message": "Required documents list uploaded successfully",
        "required_documents": doc_list
    }