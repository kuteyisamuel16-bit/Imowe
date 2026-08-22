import os
import shutil
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas

router = APIRouter(prefix="/study-spaces/{study_space_id}/materials", tags=["materials"])

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


@router.post("", response_model=schemas.MaterialOut, status_code=201)
def upload_material(
    study_space_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
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
        status=models.MaterialStatus.uploaded,
    )
    db.add(material)
    db.commit()
    db.refresh(material)
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
