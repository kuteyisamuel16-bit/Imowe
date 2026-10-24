"""
app/routers/video_lessons.py - "slides + voice" video lessons.

POST /video-lessons/generate turns one tutor answer into scenes (heading, bullets, narration).
The app plays the scenes as animated slides and gets each scene's audio from /narration/speak.
A lesson is saved per answer, so tapping "Make video lesson" again returns it instantly.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas, study_ai
from app.ratelimit import video_generate_limit

logger = logging.getLogger("imowe.video")

router = APIRouter(prefix="/video-lessons", tags=["video-lessons"])


def _out(lesson: models.VideoLesson, cached: bool) -> schemas.VideoLessonOut:
    try:
        scenes = json.loads(lesson.content)
    except Exception:
        scenes = []
    return schemas.VideoLessonOut(
        id=lesson.id, title=lesson.title, scenes=scenes, created_at=lesson.created_at, cached=cached
    )


@router.post("/generate", response_model=schemas.VideoLessonOut, dependencies=[Depends(video_generate_limit)])
def generate_lesson(
    payload: schemas.VideoGenerateIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    answer_text = (payload.text or "").strip()
    question = (payload.question or "").strip()
    course_title = None
    message_id = None

    if payload.message_id:
        message = (
            db.query(models.AIInteraction)
            .filter(
                models.AIInteraction.id == payload.message_id,
                models.AIInteraction.user_id == current_user.id,
                models.AIInteraction.role == "assistant",
            )
            .first()
        )
        if not message:
            raise HTTPException(status_code=404, detail="That answer was not found.")
        message_id = message.id

        existing = (
            db.query(models.VideoLesson)
            .filter(models.VideoLesson.user_id == current_user.id, models.VideoLesson.message_id == message_id)
            .order_by(models.VideoLesson.created_at.desc())
            .first()
        )
        if existing:
            return _out(existing, cached=True)

        answer_text = message.content or ""  # use what is stored, not what the browser sent
        if message.thread_id and not question:
            q = (
                db.query(models.AIInteraction.content)
                .filter(
                    models.AIInteraction.thread_id == message.thread_id,
                    models.AIInteraction.role == "user",
                    models.AIInteraction.created_at <= message.created_at,
                )
                .order_by(models.AIInteraction.created_at.desc())
                .first()
            )
            question = q[0] if q else ""
        if message.study_space_id:
            space = db.query(models.StudySpace).filter(models.StudySpace.id == message.study_space_id).first()
            course_title = space.course.title if space and space.course else None

    if len(answer_text) < 40:
        raise HTTPException(status_code=400, detail="This answer is too short to turn into a lesson.")
    if not study_ai.is_configured():
        raise HTTPException(status_code=503, detail="Video lessons are not available right now.")

    script = study_ai.generate_video_script(answer_text, question, course_title)
    if not script:
        raise HTTPException(status_code=502, detail="Couldn't write the lesson this time. Please try again.")

    lesson = models.VideoLesson(
        user_id=current_user.id,
        message_id=message_id,
        title=script["title"],
        content=json.dumps(script["scenes"]),
    )
    db.add(lesson)
    db.commit()
    db.refresh(lesson)
    return _out(lesson, cached=False)
