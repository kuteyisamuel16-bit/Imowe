import hashlib
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas, study_ai
from app.activity import log_event
from app.mastery import update_mastery_from_review
from app.ratelimit import summary_generate_limit, flashcard_generate_limit

logger = logging.getLogger("imowe.study_tools")

from app import entitlements

router = APIRouter(prefix="/study-spaces/{study_space_id}", tags=["study-tools"])

MAX_AI_CHARS = 30000  # how much course text is sent to Gemini in one go


def _get_owned_study_space(study_space_id: str, db: Session, user: models.User) -> models.StudySpace:
    study_space = (
        db.query(models.StudySpace)
        .filter(models.StudySpace.id == study_space_id, models.StudySpace.user_id == user.id)
        .first()
    )
    if not study_space:
        raise HTTPException(status_code=404, detail="Study space not found.")
    return study_space


def _fingerprint(db: Session, study_space_id: str) -> str:
    """Changes whenever a material with text is added, removed or altered."""
    rows = (
        db.query(models.Material.id, func.length(models.Material.extracted_text))
        .filter(models.Material.study_space_id == study_space_id, models.Material.extracted_text.isnot(None))
        .all()
    )
    parts = sorted(f"{mid}:{length}" for mid, length in rows if length)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _gather_course_text(db: Session, study_space_id: str) -> tuple[str, list[str]]:
    """Course text for Gemini, split fairly between materials, plus their topics."""
    materials = (
        db.query(models.Material)
        .filter(models.Material.study_space_id == study_space_id, models.Material.extracted_text.isnot(None))
        .order_by(models.Material.created_at.asc())
        .all()
    )
    materials = [m for m in materials if (m.extracted_text or "").strip()]
    if not materials:
        return "", []

    budget = MAX_AI_CHARS // len(materials)
    parts = []
    topics: dict[str, None] = {}
    for m in materials:
        parts.append(f"### {m.filename}\n{m.extracted_text.strip()[:budget]}")
        try:
            for t in json.loads(m.extracted_topics or "[]"):
                topics.setdefault(str(t).strip(), None)
        except Exception:
            pass
    return "\n\n".join(parts), list(topics)[:25]


NO_MATERIALS = "No processed materials yet - upload something in the Materials tab first."


# ---------------- Summary ----------------

@router.get("/summary", response_model=schemas.SummaryOut)
def get_summary(
    study_space_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    space = _get_owned_study_space(study_space_id, db, current_user)
    row = db.query(models.CourseSummary).filter(models.CourseSummary.study_space_id == space.id).first()
    if not row:
        return schemas.SummaryOut()
    return schemas.SummaryOut(
        summary=json.loads(row.content),
        updated_at=row.updated_at,
        stale=(_fingerprint(db, space.id) != row.source_hash),
    )


@router.post("/summary/generate", response_model=schemas.SummaryOut, dependencies=[Depends(summary_generate_limit), Depends(entitlements.gate("summary"))])
def generate_summary(
    study_space_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not study_ai.is_configured():
        raise HTTPException(status_code=503, detail="Summaries aren't configured yet - GEMINI_API_KEY is missing.")
    space = _get_owned_study_space(study_space_id, db, current_user)

    text, topics = _gather_course_text(db, space.id)
    if not text:
        raise HTTPException(status_code=400, detail=NO_MATERIALS)

    data = study_ai.generate_summary(text, space.course.title, topics)
    if not data:
        raise HTTPException(status_code=502, detail="Couldn't generate the summary right now - try again.")

    fingerprint = _fingerprint(db, space.id)
    row = db.query(models.CourseSummary).filter(models.CourseSummary.study_space_id == space.id).first()
    if row:
        row.content = json.dumps(data)
        row.source_hash = fingerprint
        row.updated_at = datetime.utcnow()
    else:
        row = models.CourseSummary(study_space_id=space.id, content=json.dumps(data), source_hash=fingerprint)
        db.add(row)
    db.commit()
    db.refresh(row)

    log_event(db, current_user.id, space.id, "summary_generated")
    return schemas.SummaryOut(summary=data, updated_at=row.updated_at, stale=False)


# ---------------- Flashcards ----------------

@router.get("/flashcards", response_model=list[schemas.FlashcardOut])
def list_flashcards(
    study_space_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    space = _get_owned_study_space(study_space_id, db, current_user)
    return (
        db.query(models.Flashcard)
        .filter(models.Flashcard.study_space_id == space.id, models.Flashcard.user_id == current_user.id)
        .order_by(models.Flashcard.created_at.asc(), models.Flashcard.id.asc())
        .all()
    )


@router.post(
    "/flashcards/generate",
    response_model=list[schemas.FlashcardOut],
    status_code=201,
    dependencies=[Depends(flashcard_generate_limit), Depends(entitlements.gate("flashcards"))],
)
def generate_flashcards(
    study_space_id: str,
    payload: schemas.FlashcardGenerateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Replaces this course's deck with a fresh one (review progress on the old cards resets)."""
    if not study_ai.is_configured():
        raise HTTPException(status_code=503, detail="Flashcards aren't configured yet - GEMINI_API_KEY is missing.")
    space = _get_owned_study_space(study_space_id, db, current_user)

    text, topics = _gather_course_text(db, space.id)
    if not text:
        raise HTTPException(status_code=400, detail=NO_MATERIALS)

    count = max(5, min(payload.count, 25))
    generated = study_ai.generate_flashcards(text, topics, count)
    if not generated:
        raise HTTPException(status_code=502, detail="Couldn't generate flashcards right now - try again.")

    db.query(models.Flashcard).filter(
        models.Flashcard.study_space_id == space.id, models.Flashcard.user_id == current_user.id
    ).delete(synchronize_session=False)

    now = datetime.utcnow()
    cards = []
    for i, g in enumerate(generated):
        # created_at is staggered by a microsecond so the deck keeps Gemini's order
        card = models.Flashcard(
            user_id=current_user.id,
            study_space_id=space.id,
            front=g["front"],
            back=g["back"],
            topic=g["topic"],
            created_at=now.replace(microsecond=min(i, 999999)),
        )
        db.add(card)
        cards.append(card)
    db.commit()
    for c in cards:
        db.refresh(c)

    log_event(db, current_user.id, space.id, "flashcards_generated")
    return cards


@router.post("/flashcards/{card_id}/review", response_model=schemas.FlashcardOut)
def review_flashcard(
    study_space_id: str,
    card_id: str,
    payload: schemas.FlashcardReviewIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    space = _get_owned_study_space(study_space_id, db, current_user)
    card = (
        db.query(models.Flashcard)
        .filter(
            models.Flashcard.id == card_id,
            models.Flashcard.study_space_id == space.id,
            models.Flashcard.user_id == current_user.id,
        )
        .first()
    )
    if not card:
        raise HTTPException(status_code=404, detail="Flashcard not found.")

    if payload.known:
        card.times_known = (card.times_known or 0) + 1
    else:
        card.times_missed = (card.times_missed or 0) + 1
    card.last_known = payload.known
    card.last_reviewed = datetime.utcnow()

    update_mastery_from_review(db, current_user.id, space.id, card.topic, payload.known)
    db.commit()
    db.refresh(card)
    return card


# ---------------- Notes ----------------

def _make_note_title(title: str | None, content: str) -> str:
    title = " ".join((title or "").split())
    if not title:
        first_line = " ".join(content.split())
        title = first_line[:60] + ("…" if len(first_line) > 60 else "")
    return title[:200] or "Untitled note"


def _get_owned_note(db: Session, space: models.StudySpace, note_id: str, user: models.User) -> models.Note:
    note = (
        db.query(models.Note)
        .filter(models.Note.id == note_id, models.Note.study_space_id == space.id, models.Note.user_id == user.id)
        .first()
    )
    if not note:
        raise HTTPException(status_code=404, detail="Note not found.")
    return note


@router.get("/notes", response_model=list[schemas.NoteOut])
def list_notes(
    study_space_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    space = _get_owned_study_space(study_space_id, db, current_user)
    return (
        db.query(models.Note)
        .filter(models.Note.study_space_id == space.id, models.Note.user_id == current_user.id)
        .order_by(models.Note.updated_at.desc())
        .all()
    )


@router.post("/notes", response_model=schemas.NoteOut, status_code=201)
def create_note(
    study_space_id: str,
    payload: schemas.NoteIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    space = _get_owned_study_space(study_space_id, db, current_user)
    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="A note can't be empty.")
    if len(content) > 20000:
        raise HTTPException(status_code=400, detail="That note is too long (20,000 characters max).")

    note = models.Note(
        user_id=current_user.id,
        study_space_id=space.id,
        title=_make_note_title(payload.title, content),
        content=content,
        source=payload.source,
        source_message_id=payload.source_message_id,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.put("/notes/{note_id}", response_model=schemas.NoteOut)
def update_note(
    study_space_id: str,
    note_id: str,
    payload: schemas.NoteUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    space = _get_owned_study_space(study_space_id, db, current_user)
    note = _get_owned_note(db, space, note_id, current_user)

    if payload.content is not None:
        content = payload.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="A note can't be empty.")
        if len(content) > 20000:
            raise HTTPException(status_code=400, detail="That note is too long (20,000 characters max).")
        note.content = content
    if payload.title is not None:
        note.title = _make_note_title(payload.title, note.content)
    db.commit()
    db.refresh(note)
    return note


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(
    study_space_id: str,
    note_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    space = _get_owned_study_space(study_space_id, db, current_user)
    note = _get_owned_note(db, space, note_id, current_user)
    db.delete(note)
    db.commit()
    return None
