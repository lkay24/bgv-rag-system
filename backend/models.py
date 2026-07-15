from sqlalchemy import (
    Column, Integer, String,
    DateTime, Enum, ForeignKey, JSON
)
from sqlalchemy.orm import relationship
from datetime import datetime

from backend.database import Base


class Batch(Base):
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String, nullable=False)
    total_candidates = Column(Integer, default=0)
    processed = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    processing_status = Column(String, default='not_started')

    candidates = relationship("Candidate", back_populates="batch")


class Candidate(Base):
    __tablename__ = "candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    candidate_id = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    phone = Column(String)
    drive_folder_data = Column(JSON, nullable=True)

    status = Column(
        Enum(
            'pending',
            'processing',
            'completed',
            'partial',
            'failed',
            name='candidate_status'
        ),
        default='pending'
    )

    batch_id = Column(Integer, ForeignKey("batches.id"))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("Batch", back_populates="candidates")
    documents = relationship("Document", back_populates="candidate")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    candidate_id = Column(String, ForeignKey("candidates.candidate_id"))
    doc_category = Column(String)
    doc_type = Column(String)
    drive_file_id = Column(String)
    drive_path = Column(String)
    ai_confidence = Column(String)

    status = Column(
        Enum('verified', 'pending', 'failed', name='doc_status'),
        default='pending'
    )

    received_at = Column(DateTime, default=datetime.utcnow)

    candidate = relationship("Candidate", back_populates="documents")


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(JSON)
    updated_at = Column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    name = Column(String)
    access_token = Column(String)
    refresh_token = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)