import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas

logger = logging.getLogger("imowe.study_spaces")

router = APIRouter(prefix="/study-spaces", tags=["study-spaces"])


def _get_owned_study_space(study_space_id: str, db: Session, user: models.User) -> models.StudySpace:
    study_space = (
        db.query(models.StudySpace)
        .options(joinedload(models.StudySpace.course))
        .filter(models.StudySpace.id == study_space_id, models.StudySpace.user_id == user.id)
        .first()
    )
    if not study_space:
        raise HTTPException(status_code=404, detail="Study space not found.")
    return study_space


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
    return _get_owned_study_space(study_space_id, db, current_user)


@router.patch("/{study_space_id}/progress", response_model=schemas.StudySpaceOut)
def update_progress(
    study_space_id: str,
    payload: schemas.StudySpaceProgressUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    study_space = _get_owned_study_space(study_space_id, db, current_user)

    study_space.progress_percent = payload.progress_percent
    if payload.next_up is not None:
        study_space.next_up = payload.next_up

    db.commit()
    db.refresh(study_space)
    return study_space


@router.patch("/{study_space_id}/status", response_model=schemas.StudySpaceOut)
def update_status(
    study_space_id: str,
    payload: schemas.StudySpaceStatusUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Move a course between Current / Completed / Archived."""
    study_space = _get_owned_study_space(study_space_id, db, current_user)
    study_space.status = models.StudySpaceStatus(payload.status)
    db.commit()
    db.refresh(study_space)
    return study_space


def purge_study_space(db: Session, study_space: models.StudySpace) -> list[str]:
    """
    Permanently remove a Study Space and everything that hangs off it, in an
    order that respects the foreign keys. Returns the uploaded-file paths so
    the caller can delete them from disk after the commit succeeds.
    """
    sid = study_space.id
    course_id = study_space.course_id

    material_rows = db.query(models.Material.id, models.Material.file_path).filter(
        models.Material.study_space_id == sid
    ).all()
    material_ids = [r[0] for r in material_rows]
    file_paths = [r[1] for r in material_rows if r[1]]

    thread_ids = [
        r[0] for r in db.query(models.ChatThread.id).filter(
            or_(
                models.ChatThread.study_space_id == sid,
                models.ChatThread.material_id.in_(material_ids) if material_ids else False,
            )
        ).all()
    ]

    # Assistant replies carry only a thread_id (no study_space_id), so match on
    # the thread as well as the space and the materials.
    msg_conditions = [models.AIInteraction.study_space_id == sid]
    if thread_ids:
        msg_conditions.append(models.AIInteraction.thread_id.in_(thread_ids))
    if material_ids:
        msg_conditions.append(models.AIInteraction.material_id.in_(material_ids))
    message_ids = [r[0] for r in db.query(models.AIInteraction.id).filter(or_(*msg_conditions)).all()]

    if message_ids:
        db.query(models.MessageFeedback).filter(
            models.MessageFeedback.message_id.in_(message_ids)
        ).delete(synchronize_session=False)
        db.query(models.AIInteraction).filter(
            models.AIInteraction.id.in_(message_ids)
        ).delete(synchronize_session=False)
    if thread_ids:
        db.query(models.ChatThread).filter(
            models.ChatThread.id.in_(thread_ids)
        ).delete(synchronize_session=False)

    db.query(models.MaterialChunk).filter(models.MaterialChunk.study_space_id == sid).delete(synchronize_session=False)

    # recordings point at the material they were made in - unlink before deleting
    db.query(models.Material).filter(models.Material.study_space_id == sid).update(
        {"linked_material_id": None}, synchronize_session=False
    )
    db.query(models.Material).filter(models.Material.study_space_id == sid).delete(synchronize_session=False)

    for model in (
        models.QuizAttemptAnswer,
        models.QuizAttempt,
        models.QuizQuestion,
        models.MasteryRecord,
        models.StudyEvent,
        models.Flashcard,
        models.Note,
        models.CourseSummary,
        models.MaterialChapterSummary,
    ):
        db.query(model).filter(model.study_space_id == sid).delete(synchronize_session=False)

    # deadlines are the student's own, so they stay and just lose the course link
    db.query(models.Deadline).filter(models.Deadline.study_space_id == sid).update(
        {"study_space_id": None}, synchronize_session=False
    )

    db.query(models.StudySpace).filter(models.StudySpace.id == sid).delete(synchronize_session=False)

    # the course goes too, unless some other Study Space still uses it
    still_used = db.query(models.StudySpace.id).filter(models.StudySpace.course_id == course_id).first()
    if not still_used:
        db.query(models.Course).filter(models.Course.id == course_id).delete(synchronize_session=False)

    return file_paths


@router.delete("/{study_space_id}", status_code=204)
def delete_study_space(
    study_space_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Delete a course and all of its materials, chats, quizzes and progress. Irreversible."""
    study_space = _get_owned_study_space(study_space_id, db, current_user)
    try:
        file_paths = purge_study_space(db, study_space)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Deleting study space %s failed", study_space_id)
        raise HTTPException(status_code=500, detail="Couldn't delete this course. Nothing was changed.")

    # files go only after the database commit succeeded
    for path in file_paths:
        try:
            os.remove(path)
        except OSError:
            pass
    return None
