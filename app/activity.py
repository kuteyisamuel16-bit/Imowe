import logging
from sqlalchemy.orm import Session

from app import models

logger = logging.getLogger("imowe.activity")


def log_event(db: Session, user_id: str, study_space_id: str | None, event_type: str):
    """Best-effort activity logging - failures here never bubble up and
    break whatever real action (chat, quiz, upload...) triggered them."""
    try:
        event = models.StudyEvent(user_id=user_id, study_space_id=study_space_id, event_type=event_type)
        db.add(event)
        db.commit()
    except Exception:
        logger.exception("Failed to log study event (non-fatal)")
        db.rollback()
