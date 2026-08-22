from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas

router = APIRouter(prefix="/study-spaces", tags=["study-spaces"])


@router.post("", response_model=schemas.StudySpaceOut, status_code=201)
def add_course(
    payload: schemas.CourseCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    course = models.Course(user_id=current_user.id, **payload.model_dump())
    db.add(course)
    db.flush()

    study_space = models.StudySpace(user_id=current_user.id, course_id=course.id)
    db.add(study_space)
    db.commit()
    db.refresh(study_space)
    return study_space


@router.get("", response_model=list[schemas.StudySpaceOut])
def list_my_courses(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = (
        db.query(models.StudySpace)
        .options(joinedload(models.StudySpace.course))
        .filter(models.StudySpace.user_id == current_user.id)
    )
    if status_filter:
        query = query.filter(models.StudySpace.status == status_filter)

    return query.order_by(models.StudySpace.created_at.desc()).all()


@router.get("/{study_space_id}", response_model=schemas.StudySpaceOut)
def get_study_space(
    study_space_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    study_space = (
        db.query(models.StudySpace)
        .options(joinedload(models.StudySpace.course))
        .filter(models.StudySpace.id == study_space_id, models.StudySpace.user_id == current_user.id)
        .first()
    )
    if not study_space:
        raise HTTPException(status_code=404, detail="Study space not found.")
    return study_space


@router.patch("/{study_space_id}/progress", response_model=schemas.StudySpaceOut)
def update_progress(
    study_space_id: str,
    payload: schemas.StudySpaceProgressUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    study_space = (
        db.query(models.StudySpace)
        .filter(models.StudySpace.id == study_space_id, models.StudySpace.user_id == current_user.id)
        .first()
    )
    if not study_space:
        raise HTTPException(status_code=404, detail="Study space not found.")

    study_space.progress_percent = payload.progress_percent
    if payload.next_up is not None:
        study_space.next_up = payload.next_up

    db.commit()
    db.refresh(study_space)
    return study_space
