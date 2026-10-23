from datetime import datetime, timedelta
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models
from app.mastery import WEAK_THRESHOLD

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _legacy_topic_breakdown(db: Session, user_id: str) -> list[dict]:
    """Fallback for students whose quizzes pre-date MasteryRecord."""
    rows = (
        db.query(models.QuizAttemptAnswer)
        .filter(models.QuizAttemptAnswer.user_id == user_id, models.QuizAttemptAnswer.topic.isnot(None))
        .all()
    )
    stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for r in rows:
        stats[r.topic]["total"] += 1
        if r.is_correct:
            stats[r.topic]["correct"] += 1
    return [
        {
            "topic": topic,
            "score_percent": round((s["correct"] / s["total"]) * 100) if s["total"] else 0,
            "attempts": s["total"],
        }
        for topic, s in stats.items()
    ]


def _topic_breakdown(db: Session, user_id: str) -> list[dict]:
    """Per-topic mastery across all of the student's courses."""
    records = db.query(models.MasteryRecord).filter(models.MasteryRecord.user_id == user_id).all()
    if not records:
        return _legacy_topic_breakdown(db, user_id)

    # Same topic can exist in several courses - blend them, weighted by questions seen.
    agg = defaultdict(lambda: {"weighted": 0.0, "seen": 0})
    for r in records:
        weight = max(r.questions_seen or 0, 1)
        agg[r.topic]["weighted"] += (r.mastery_percent or 0.0) * weight
        agg[r.topic]["seen"] += weight

    return [
        {
            "topic": topic,
            "score_percent": round(a["weighted"] / a["seen"]),
            "attempts": a["seen"],
        }
        for topic, a in agg.items()
    ]


@router.get("/overview")
def get_overview(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    courses_count = db.query(models.StudySpace).filter(models.StudySpace.user_id == current_user.id).count()

    attempts = db.query(models.QuizAttempt).filter(models.QuizAttempt.user_id == current_user.id).all()
    avg_score = (
        round(sum((a.score / a.total * 100) for a in attempts if a.total) / len(attempts))
        if attempts else 0
    )

    events = db.query(models.StudyEvent).filter(models.StudyEvent.user_id == current_user.id).all()
    weeks_active = len({e.created_at.isocalendar()[:2] for e in events})

    topics = _topic_breakdown(db, current_user.id)
    strengths = sorted([t for t in topics if t["score_percent"] >= 75], key=lambda t: -t["score_percent"])[:3]
    needs_improvement = sorted([t for t in topics if t["score_percent"] < 75], key=lambda t: t["score_percent"])[:3]

    grade = "—"
    if attempts:
        if avg_score >= 90: grade = "A"
        elif avg_score >= 80: grade = "B+"
        elif avg_score >= 70: grade = "B"
        elif avg_score >= 60: grade = "C"
        else: grade = "D"

    return {
        "courses_count": courses_count,
        "avg_quiz_score": avg_score,
        "weeks_active": weeks_active,
        "grade": grade,
        "strengths": strengths,
        "needs_improvement": needs_improvement,
        "has_data": bool(attempts or events),
    }


@router.get("/performance")
def get_performance(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    attempts = (
        db.query(models.QuizAttempt)
        .filter(models.QuizAttempt.user_id == current_user.id)
        .order_by(models.QuizAttempt.created_at.asc())
        .all()
    )
    return {
        "attempts": [
            {
                "date": a.created_at.isoformat(),
                "score_percent": round((a.score / a.total) * 100) if a.total else 0,
                "score": a.score,
                "total": a.total,
            }
            for a in attempts
        ]
    }


@router.get("/topics")
def get_topics(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    topics = _topic_breakdown(db, current_user.id)
    topics.sort(key=lambda t: t["score_percent"], reverse=True)
    return {"topics": topics}


@router.get("/habits")
def get_habits(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    events = (
        db.query(models.StudyEvent)
        .filter(models.StudyEvent.user_id == current_user.id)
        .order_by(models.StudyEvent.created_at.asc())
        .all()
    )
    if not events:
        return {"total_sessions": 0, "current_streak_days": 0, "longest_streak_days": 0, "by_type": {}, "estimated_minutes": 0}

    days = sorted({e.created_at.date() for e in events})
    longest = current = 1
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days == 1:
            current += 1
            longest = max(longest, current)
        else:
            current = 1

    day_set = set(days)
    today = datetime.utcnow().date()
    current_streak = 0
    d = today
    while d in day_set:
        current_streak += 1
        d -= timedelta(days=1)

    by_type = defaultdict(int)
    for e in events:
        by_type[e.event_type] += 1

    return {
        "total_sessions": len(days),
        "current_streak_days": current_streak,
        "longest_streak_days": longest,
        "by_type": dict(by_type),
        "estimated_minutes": len(events) * 2,  # rough: ~2 min of engagement per logged action
    }


@router.get("/recommendations")
def get_recommendations(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """
    Powers Home > Today's Plan. Returns up to 3 concrete next actions:
      1. Weakest topics (mastery below threshold)
      2. Courses with processed materials but no quiz taken yet
      3. A streak nudge if the student hasn't studied today
    """
    recs: list[dict] = []

    spaces = (
        db.query(models.StudySpace)
        .filter(
            models.StudySpace.user_id == current_user.id,
            models.StudySpace.status == models.StudySpaceStatus.current,
        )
        .all()
    )
    space_by_id = {s.id: s for s in spaces}

    # 1) Weak topics, weakest first
    weak = (
        db.query(models.MasteryRecord)
        .filter(
            models.MasteryRecord.user_id == current_user.id,
            models.MasteryRecord.mastery_percent < WEAK_THRESHOLD,
        )
        .order_by(models.MasteryRecord.mastery_percent.asc())
        .all()
    )
    for r in weak:
        space = space_by_id.get(r.study_space_id)
        if not space:
            continue
        recs.append({
            "type": "review_topic",
            "title": f"Review {r.topic}",
            "detail": f"{space.course.title} · mastery {round(r.mastery_percent)}%",
            "study_space_id": space.id,
        })
        if len(recs) >= 3:
            return {"recommendations": recs}

    # 2) Courses with ready materials but no quiz attempts
    attempted_space_ids = {
        row[0]
        for row in db.query(models.QuizAttempt.study_space_id)
        .filter(models.QuizAttempt.user_id == current_user.id)
        .distinct()
        .all()
    }
    for space in spaces:
        if space.id in attempted_space_ids:
            continue
        has_ready_material = (
            db.query(models.Material.id)
            .filter(
                models.Material.study_space_id == space.id,
                models.Material.status == models.MaterialStatus.processed,
            )
            .first()
        )
        if has_ready_material:
            recs.append({
                "type": "first_quiz",
                "title": f"Take your first quiz in {space.course.title}",
                "detail": "Quizzes show IMOWE what you already know.",
                "study_space_id": space.id,
            })
            if len(recs) >= 3:
                return {"recommendations": recs}

    # 3) Streak nudge
    today = datetime.utcnow().date()
    studied_today = (
        db.query(models.StudyEvent.id)
        .filter(
            models.StudyEvent.user_id == current_user.id,
            models.StudyEvent.created_at >= datetime(today.year, today.month, today.day),
        )
        .first()
    )
    if not studied_today and spaces:
        recs.append({
            "type": "streak",
            "title": "Do a quick study session today",
            "detail": "Even 10 minutes keeps your streak alive.",
            "study_space_id": spaces[0].id,
        })

    return {"recommendations": recs[:3]}
