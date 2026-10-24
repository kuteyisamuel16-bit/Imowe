import os
import re
import json
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas, ai, rag, extract
from app.ratelimit import upload_limit
from app.activity import log_event

router = APIRouter(prefix="/study-spaces/{study_space_id}/materials", tags=["materials"])

UPLOAD_DIR = "uploaded_materials"
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB per file
MAX_STORED_CHARS = 50000  # keep DB rows sane; Course Memory chunks this text for retrieval


def _get_owned_study_space(study_space_id: str, db: Session, user: models.User) -> models.StudySpace:
    study_space = (
        db.query(models.StudySpace)
        .filter(models.StudySpace.id == study_space_id, models.StudySpace.user_id == user.id)
        .first()
    )
    if not study_space:
        raise HTTPException(status_code=404, detail="Study space not found.")
    return study_space


def _get_owned_material(study_space_id: str, material_id: str, db: Session, user: models.User) -> models.Material:
    study_space = _get_owned_study_space(study_space_id, db, user)
    material = (
        db.query(models.Material)
        .filter(models.Material.id == material_id, models.Material.study_space_id == study_space.id)
        .first()
    )
    if not material:
        raise HTTPException(status_code=404, detail="Material not found.")
    return material


@router.post("", response_model=schemas.MaterialOut, status_code=201, dependencies=[Depends(upload_limit)])
def upload_material(
    study_space_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    study_space = _get_owned_study_space(study_space_id, db, current_user)

    display_name = os.path.basename(file.filename or "").strip() or "Untitled"
    ext = os.path.splitext(display_name)[1].lower()
    if ext not in extract.SUPPORTED:
        raise HTTPException(status_code=400, detail=extract.unsupported_message(ext))

    safe_name = f"{uuid.uuid4()}_{re.sub(r'[^A-Za-z0-9._-]', '_', display_name)}"
    destination = os.path.join(UPLOAD_DIR, safe_name)
    total = 0
    with open(destination, "wb") as buffer:
        while chunk := file.file.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                break
            buffer.write(chunk)
    if total > MAX_UPLOAD_BYTES:
        os.remove(destination)
        raise HTTPException(status_code=400, detail=f"That file is too large. The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")

    material = models.Material(
        study_space_id=study_space.id,
        filename=display_name,
        file_path=destination,
        content_type=file.content_type,
        material_type=models.MaterialType.document,
        status=models.MaterialStatus.processing,
    )
    db.add(material)
    db.commit()
    db.refresh(material)

    try:
        text = extract.extract_text(destination, ext)
        topics = ai.extract_topics(text)
        material.extracted_text = text[:MAX_STORED_CHARS] if text else None
        material.extracted_topics = json.dumps(topics)
        material.status = models.MaterialStatus.processed if topics else models.MaterialStatus.uploaded
    except Exception:
        material.status = models.MaterialStatus.failed
    db.commit()
    db.refresh(material)

    # Course Memory: chunk + embed so the tutor can retrieve relevant parts.
    # Fails soft - the material still works (the tutor falls back to raw text).
    rag.index_material(db, material)
    db.refresh(material)

    log_event(db, current_user.id, study_space.id, "material_uploaded")
    return material


@router.post("/recording", response_model=schemas.MaterialOut, status_code=201, dependencies=[Depends(upload_limit)])
def create_recording(
    study_space_id: str,
    payload: schemas.RecordingCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    A lecture recording transcribed client-side (browser Speech Recognition).
    No audio file is stored - just the resulting text, saved as its own
    Material so it's isolated from whatever material it was recorded inside,
    while still linking back to it via linked_material_id.
    """
    study_space = _get_owned_study_space(study_space_id, db, current_user)

    if payload.linked_material_id:
        _get_owned_material(study_space_id, payload.linked_material_id, db, current_user)

    transcript = (payload.transcript or "").strip()
    material = models.Material(
        study_space_id=study_space.id,
        filename=payload.filename,
        file_path=None,
        content_type="text/plain",
        material_type=models.MaterialType.recording,
        status=models.MaterialStatus.processing,
        linked_material_id=payload.linked_material_id,
    )
    db.add(material)
    db.commit()
    db.refresh(material)

    try:
        topics = ai.extract_topics(transcript)
        material.extracted_text = transcript[:MAX_STORED_CHARS] if transcript else None
        material.extracted_topics = json.dumps(topics)
        material.status = models.MaterialStatus.processed if topics else models.MaterialStatus.uploaded
    except Exception:
        material.status = models.MaterialStatus.failed
    db.commit()
    db.refresh(material)

    rag.index_material(db, material)
    db.refresh(material)

    log_event(db, current_user.id, study_space.id, "lecture_recorded")
    return material


@router.get("", response_model=list[schemas.MaterialOut])
def list_materials(
    study_space_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    study_space = _get_owned_study_space(study_space_id, db, current_user)
    return (
        db.query(models.Material)
        .filter(models.Material.study_space_id == study_space.id)
        .order_by(models.Material.created_at.desc())
        .all()
    )


@router.get("/{material_id}", response_model=schemas.MaterialOut)
def get_material(
    study_space_id: str,
    material_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return _get_owned_material(study_space_id, material_id, db, current_user)
