"""
app/routers/prefs.py - per-user settings: email reminders on/off, and which version of the
Terms & Privacy Policy the student agreed to.
"""
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, ratelimit

router = APIRouter(prefix="/prefs", tags=["prefs"])

# Change this date whenever you change the Terms text in index.html, so you can see who agreed to which version.
TERMS_VERSION = "2026-10-26"


def get_or_create_prefs(db: Session, user_id: str) -> models.UserPrefs:
    row = db.query(models.UserPrefs).filter(models.UserPrefs.user_id == user_id).first()
    if not row:
        row = models.UserPrefs(user_id=user_id, email_reminders=True)
        db.add(row)
        db.flush()
    return row


def record_terms_acceptance(db: Session, user_id: str) -> None:
    """Caller commits."""
    row = get_or_create_prefs(db, user_id)
    row.terms_version = TERMS_VERSION
    row.terms_accepted_at = datetime.utcnow()


class PrefsOut(BaseModel):
    email_reminders: bool
    terms_version: str | None = None
    terms_accepted_at: datetime | None = None
    current_terms_version: str = TERMS_VERSION


class PrefsUpdate(BaseModel):
    email_reminders: bool


def _out(row: models.UserPrefs) -> PrefsOut:
    return PrefsOut(
        email_reminders=bool(row.email_reminders),
        terms_version=row.terms_version,
        terms_accepted_at=row.terms_accepted_at,
    )


@router.get("", response_model=PrefsOut)
def get_prefs(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = get_or_create_prefs(db, user.id)
    db.commit()
    return _out(row)


@router.put("", response_model=PrefsOut, dependencies=[Depends(ratelimit.prefs_limit)])
def update_prefs(payload: PrefsUpdate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = get_or_create_prefs(db, user.id)
    row.email_reminders = payload.email_reminders
    db.commit()
    return _out(row)


@router.post("/accept-terms", response_model=PrefsOut, dependencies=[Depends(ratelimit.prefs_limit)])
def accept_terms(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    record_terms_acceptance(db, user.id)
    db.commit()
    return _out(get_or_create_prefs(db, user.id))
