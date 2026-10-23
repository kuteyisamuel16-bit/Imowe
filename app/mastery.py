"""app/mastery.py - turns quiz results into a per-topic mastery estimate."""
from datetime import datetime

from sqlalchemy.orm import Session

from app import models

# How much the newest attempt counts vs. history (0-1). Higher = reacts faster.
NEW_ATTEMPT_WEIGHT = 0.4
WEAK_THRESHOLD = 70.0


def update_mastery(db: Session, user_id: str, study_space_id: str, questions, answers_by_question) -> None:
    """Call after grading a quiz, before the final commit."""
    # Tally this attempt per topic
    tally: dict[str, list[int]] = {}  # topic -> [correct, total]
    for q in questions:
        topic = q.topic or "General"
        t = tally.setdefault(topic, [0, 0])
        t[1] += 1
        if answers_by_question.get(q.id) == q.correct_index:
            t[0] += 1

    now = datetime.utcnow()
    for topic, (correct, total) in tally.items():
        attempt_pct = (correct / total) * 100
        rec = (
            db.query(models.MasteryRecord)
            .filter_by(user_id=user_id, study_space_id=study_space_id, topic=topic)
            .first()
        )
        if rec is None:
            db.add(models.MasteryRecord(
                user_id=user_id,
                study_space_id=study_space_id,
                topic=topic,
                attempts=1,
                questions_seen=total,
                questions_correct=correct,
                mastery_percent=attempt_pct,
                last_seen=now,
            ))
        else:
            rec.attempts += 1
            rec.questions_seen += total
            rec.questions_correct += correct
            rec.mastery_percent = (
                rec.mastery_percent * (1 - NEW_ATTEMPT_WEIGHT) + attempt_pct * NEW_ATTEMPT_WEIGHT
            )
            rec.last_seen = now


def weak_topics(db: Session, user_id: str, study_space_id: str, limit: int = 3) -> list[str]:
    rows = (
        db.query(models.MasteryRecord)
        .filter(
            models.MasteryRecord.user_id == user_id,
            models.MasteryRecord.study_space_id == study_space_id,
            models.MasteryRecord.mastery_percent < WEAK_THRESHOLD,
        )
        .order_by(models.MasteryRecord.mastery_percent.asc())
        .limit(limit)
        .all()
    )
    return [r.topic for r in rows]
