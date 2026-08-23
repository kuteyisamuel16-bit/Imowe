import os
import json
import shutil
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas, ai

router = APIRouter(prefix="/study-spaces/{study_space_id}/materials", tags=["materials"])

# Swap this for S3/GCS/Azure Blob storage before going to production.
UPLOAD_DIR = "uploaded_materials"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _get_owned_study_space(study_space_id: str, db: Session, user: models.User) -> models.StudySpace:
    study_space = (
        db.query(models.StudySpace)
        .filter(models.StudySpace.id == study_space_id, models.StudySpace.user_id == user.id)
        .first()
    )
    if not study_space:
        raise HTTPException(status_code=404, detail="Study space not found.")
    return study_space


def _extract_text(file_path: str, content_type: str | None) -> str:
    """Best-effort text extraction. PDFs use pypdf; everything else is read as
    plain text. Returns '' (not an error) if the file type isn't supported -
    processing just quietly does nothing useful for that file."""
    try:
        if content_type == "application/pdf" or file_path.lower().endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        else:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        return ""


@router.post("", response_model=schemas.MaterialOut, status_code=201)
def upload_material(
    study_space_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Section 3 of the plan: 'Upload -> Process -> Course Memory -> Tutor/Audio/
    Video/Practice.' Processing (text extraction + topic detection) runs
    synchronously here for simplicity - for large files or heavy load, move
    this into a background worker (e.g. Celery/RQ) instead.
    """
    study_space = _get_owned_study_space(study_space_id, db, current_user)

    safe_name = f"{uuid.uuid4()}_{file.filename}"
    destination = os.path.join(UPLOAD_DIR, safe_name)
    with open(destination, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    material = models.Material(
        study_space_id=study_space.id,
        filename=file.filename,
        file_path=destination,
        content_type=file.content_type,
        status=models.MaterialStatus.processing,
    )
    db.add(material)
    db.commit()
    db.refresh(material)

    # Process immediately (extract text -> topics). Never let this fail the
    # upload itself - if it errors, the material still exists, just unprocessed.
    try:
        text = _extract_text(destination, file.content_type)
        topics = ai.extract_topics(text)
        material.extracted_topics = json.dumps(topics)
        material.status = models.MaterialStatus.processed if topics else models.MaterialStatus.uploaded
    except Exception:
        material.status = models.MaterialStatus.failed
    db.commit()
    db.refresh(material)

    return material


@router.get("", response_model=list[schemas.MaterialOut])
def list_materials(
    study_space_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Screen 5 'Materials' tab."""
    study_space = _get_owned_study_space(study_space_id, db, current_user)
    return (
        db.query(models.Material)
        .filter(models.Material.study_space_id == study_space.id)
        .order_by(models.Material.created_at.desc())
        .all()
    )
