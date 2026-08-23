import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas, ai

router = APIRouter(prefix="/study-spaces/{study_space_id}/quiz", tags=["quiz"])


def _get_owned_study_space(study_space_id: str, db: Session, user: models.User) -> models.StudySpace:
    study_space = (
        db.query(models.StudySpace)
        .filter(models.StudySpace.id == study_space_id, models.StudySpace.user_id == user.id)
        .first()
    )
    if not study_space:
        raise HTTPException(status_code=404, detail="Study space not found.")
    return study_space


@router.post("/generate", response_model=list[schemas.QuizQuestionOut], status_code=201)
def generate_quiz(
    study_space_id: str,
    payload: schemas.QuizGenerateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Study Tools -> Quiz -> 'Practice now'. Builds questions from whatever
    processed materials exist in this study space. Replaces any previously
    generated (unattempted) question set for this study space.
    """
    if not ai.is_configured():
        raise HTTPException(status_code=503, detail="Quiz generation isn't configured yet - GEMINI_API_KEY is missing.")

    study_space = _get_owned_study_space(study_space_id, db, current_user)

    materials = (
        db.query(models.Material)
        .filter(
            models.Material.study_space_id == study_space.id,
            models.Material.status == models.MaterialStatus.processed,
        )
        .all()
    )
    if not materials:
        raise HTTPException(
            status_code=400,
            detail="No processed materials yet - upload something in the Materials tab first.",
        )

    # Combine each material's extracted topics into one context blob.
    topic_lines = []
    for m in materials:
        if m.extracted_topics:
            try:
                topics = json.loads(m.extracted_topics)
                topic_lines.append(f"From {m.filename}: " + ", ".join(topics))
            except Exception:
                continue
    context_text = "\n".join(topic_lines) or study_space.course.title

    generated = ai.generate_quiz_questions(context_text, payload.num_questions)
    if not generated:
        raise HTTPException(status_code=502, detail="Couldn't generate questions right now - try again.")

    # Clear old unattempted questions for this study space, then insert fresh ones.
    db.query(models.QuizQuestion).filter(models.QuizQuestion.study_space_id == study_space.id).delete()

    questions = []
    for q in generated:
        question = models.QuizQuestion(
            study_space_id=study_space.id,
            question_text=q["question"],
            options=json.dumps(q["options"]),
            correct_index=q["correct_index"],
        )
        db.add(question)
        questions.append(question)
    db.commit()

    return [
        schemas.QuizQuestionOut(id=q.id, question_text=q.question_text, options=json.loads(q.options))
        for q in questions
    ]


@router.get("", response_model=list[schemas.QuizQuestionOut])
def get_quiz(
    study_space_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Reload the current question set (e.g. if the student left and came back)."""
    study_space = _get_owned_study_space(study_space_id, db, current_user)
    questions = (
        db.query(models.QuizQuestion)
        .filter(models.QuizQuestion.study_space_id == study_space.id)
        .order_by(models.QuizQuestion.created_at.asc())
        .all()
    )
    return [
        schemas.QuizQuestionOut(id=q.id, question_text=q.question_text, options=json.loads(q.options))
        for q in questions
    ]


@router.post("/submit", response_model=schemas.QuizSubmitResult)
def submit_quiz(
    study_space_id: str,
    payload: schemas.QuizSubmitRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Grades the attempt, reveals correct answers, and logs a QuizAttempt row."""
    study_space = _get_owned_study_space(study_space_id, db, current_user)

    answers_by_question = {a.question_id: a.selected_index for a in payload.answers}
    questions = (
        db.query(models.QuizQuestion)
        .filter(models.QuizQuestion.study_space_id == study_space.id)
        .all()
    )
    if not questions:
        raise HTTPException(status_code=400, detail="No quiz questions to grade - generate a quiz first.")

    review = []
    score = 0
    for q in questions:
        selected = answers_by_question.get(q.id)
        is_correct = selected == q.correct_index
        if is_correct:
            score += 1
        review.append(
            schemas.QuizReviewItem(
                question_id=q.id,
                question_text=q.question_text,
                options=json.loads(q.options),
                correct_index=q.correct_index,
                selected_index=selected,
                is_correct=is_correct,
            )
        )

    attempt = models.QuizAttempt(
        user_id=current_user.id,
        study_space_id=study_space.id,
        score=score,
        total=len(questions),
    )
    db.add(attempt)
    db.commit()

    return schemas.QuizSubmitResult(score=score, total=len(questions), review=review)
