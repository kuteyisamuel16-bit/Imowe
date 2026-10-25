"""
app/routers/deadlines.py - assignments, tests and exams with due dates.

Dates are plain calendar dates (and an optional HH:MM) in the student's own local time, so
"due Friday" means Friday wherever they are. The app works out "2 days left" on the device.
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas

router = APIRouter(prefix="/deadlines", tags=["deadlines"])

KINDS = {"assignment", "test", "exam", "other"}
MAX_OPEN = 200
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _check_space(db: Session, user: models.User, space_id: str | None):
    if not space_id:
        return None
    space = (
        db.query(models.StudySpace)
        .filter(models.StudySpace.id == space_id, models.StudySpace.user_id == user.id)
        .first()
    )
    if not space:
        raise HTTPException(status_code=404, detail="That course was not found.")
    return space.id


def _clean_title(value: str) -> str:
    title = (value or "").strip()[:200]
    if not title:
        raise HTTPException(status_code=422, detail="Give this deadline a title.")
    return title


def _clean_time(value: str | None):
    value = (value or "").strip()
    if not value:
        return None
    if not TIME_RE.match(value):
        raise HTTPException(status_code=422, detail="Time must look like 14:30.")
    return value


def _clean_kind(value: str | None) -> str:
    value = (value or "assignment").strip().lower()
    if value not in KINDS:
        raise HTTPException(status_code=422, detail="Type must be assignment, test, exam or other.")
    return value


def _out(db: Session, d: models.Deadline) -> schemas.DeadlineOut:
    course_title = None
    if d.study_space_id:
        space = db.query(models.StudySpace).filter(models.StudySpace.id == d.study_space_id).first()
        if space and space.course:
            course_title = space.course.title
    return schemas.DeadlineOut(
        id=d.id, title=d.title, kind=d.kind, due_date=d.due_date, due_time=d.due_time,
        notes=d.notes, study_space_id=d.study_space_id, course_title=course_title, done=bool(d.done),
    )


def _get_owned(db: Session, user: models.User, deadline_id: str) -> models.Deadline:
    d = db.query(models.Deadline).filter(models.Deadline.id == deadline_id, models.Deadline.user_id == user.id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Deadline not found.")
    return d


@router.get("", response_model=list[schemas.DeadlineOut])
def list_deadlines(
    status: str = Query("open", pattern="^(open|done|all)$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    q = db.query(models.Deadline).filter(models.Deadline.user_id == current_user.id)
    if status == "open":
        q = q.filter(models.Deadline.done.is_(False))
    elif status == "done":
        q = q.filter(models.Deadline.done.is_(True))
    rows = q.order_by(models.Deadline.due_date, models.Deadline.due_time.is_(None), models.Deadline.due_time).limit(500).all()
    return [_out(db, d) for d in rows]


@router.post("", response_model=schemas.DeadlineOut, status_code=201)
def create_deadline(
    payload: schemas.DeadlineIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    open_count = (
        db.query(models.Deadline)
        .filter(models.Deadline.user_id == current_user.id, models.Deadline.done.is_(False))
        .count()
    )
    if open_count >= MAX_OPEN:
        raise HTTPException(status_code=400, detail="You have a lot of open deadlines. Mark some done or delete them first.")
    d = models.Deadline(
        user_id=current_user.id,
        title=_clean_title(payload.title),
        kind=_clean_kind(payload.kind),
        due_date=payload.due_date,
        due_time=_clean_time(payload.due_time),
        notes=(payload.notes or "").strip()[:2000] or None,
        study_space_id=_check_space(db, current_user, payload.study_space_id),
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return _out(db, d)


@router.put("/{deadline_id}", response_model=schemas.DeadlineOut)
def update_deadline(
    deadline_id: str,
    payload: schemas.DeadlineUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    d = _get_owned(db, current_user, deadline_id)
    fields = payload.model_fields_set
    if "title" in fields:
        d.title = _clean_title(payload.title or "")
    if "kind" in fields:
        d.kind = _clean_kind(payload.kind)
    if "due_date" in fields and payload.due_date:
        d.due_date = payload.due_date
    if "due_time" in fields:
        d.due_time = _clean_time(payload.due_time)
    if "notes" in fields:
        d.notes = (payload.notes or "").strip()[:2000] or None
    if "study_space_id" in fields:
        d.study_space_id = _check_space(db, current_user, payload.study_space_id)
    if "done" in fields and payload.done is not None:
        d.done = payload.done
        d.done_at = datetime.utcnow() if payload.done else None
    db.commit()
    db.refresh(d)
    return _out(db, d)


@router.delete("/{deadline_id}", status_code=204)
def delete_deadline(
    deadline_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    d = _get_owned(db, current_user, deadline_id)
    db.delete(d)
    db.commit()
    return None
