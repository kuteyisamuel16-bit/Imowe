from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from google import genai
from google.genai import types

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas
from app.config import settings

router = APIRouter(prefix="/ai-tutor", tags=["ai-tutor"])

# If GEMINI_API_KEY isn't set, the client stays None and chat() returns a
# clear 503 instead of crashing - lets the rest of the app run without it.
client = genai.Client(
    api_key=settings.GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=20000),  # 20s, in ms - fail fast instead of hanging
) if settings.GEMINI_API_KEY else None

GEMINI_MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT_BASE = (
    "You are IMOWE's AI Tutor - a friendly, encouraging study assistant for "
    "university students. Explain concepts clearly, use simple language and "
    "examples, and check understanding rather than just lecturing. Keep answers "
    "focused and not overly long unless the student asks for depth."
)


def _get_owned_study_space(study_space_id: str, db: Session, user: models.User) -> models.StudySpace:
    study_space = (
        db.query(models.StudySpace)
        .filter(models.StudySpace.id == study_space_id, models.StudySpace.user_id == user.id)
        .first()
    )
    if not study_space:
        raise HTTPException(status_code=404, detail="Study space not found.")
    return study_space


@router.post("/chat", response_model=schemas.ChatMessageOut)
async def chat(
    payload: schemas.ChatMessageIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Screen 7 - AI Tutor (Chat). Sends the student's message to Gemini, grounded
    in the course context if a study_space_id is given, and stores both sides
    of the conversation as AIInteraction rows (this is the 'evidence of
    understanding' feed for the future Academic Intelligence stage).
    """
    if not client:
        raise HTTPException(
            status_code=503,
            detail="AI Tutor isn't configured yet - GEMINI_API_KEY is missing.",
        )

    study_space = None
    system_prompt = SYSTEM_PROMPT_BASE
    if payload.study_space_id:
        study_space = _get_owned_study_space(payload.study_space_id, db, current_user)
        system_prompt += (
            f" The student is currently studying '{study_space.course.title}'"
            f" ({study_space.course.subtitle or 'no subtitle set'})."
            " Keep explanations relevant to this course where possible."
        )

    user_msg = models.AIInteraction(
        user_id=current_user.id,
        study_space_id=study_space.id if study_space else None,
        role="user",
        content=payload.message,
    )
    db.add(user_msg)
    db.commit()

    # Pull the last 10 messages in this thread for conversational context.
    history = (
        db.query(models.AIInteraction)
        .filter(
            models.AIInteraction.user_id == current_user.id,
            models.AIInteraction.study_space_id == (study_space.id if study_space else None),
        )
        .order_by(models.AIInteraction.created_at.desc())
        .limit(10)
        .all()
    )
    history.reverse()

    # Gemini uses role "model" for the assistant side, not "assistant".
    contents = [
        types.Content(
            role=("user" if m.role == "user" else "model"),
            parts=[types.Part(text=m.content)],
        )
        for m in history
    ]

    try:
        import asyncio

try:
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=1000,
            ),
        ),
        timeout=15,
    )
except asyncio.TimeoutError:
    raise HTTPException(status_code=504, detail="AI Tutor timed out reaching Gemini — check outbound network access from the server.")
        reply_text = response.text
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI Tutor request failed: {e}")

    assistant_msg = models.AIInteraction(
        user_id=current_user.id,
        study_space_id=study_space.id if study_space else None,
        role="assistant",
        content=reply_text,
    )
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)
    return assistant_msg


@router.get("/messages", response_model=list[schemas.ChatMessageOut])
def get_messages(
    study_space_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Loads the existing conversation when the AI Tutor screen opens."""
    query = (
        db.query(models.AIInteraction)
        .filter(
            models.AIInteraction.user_id == current_user.id,
            models.AIInteraction.study_space_id == study_space_id,
        )
        .order_by(models.AIInteraction.created_at.asc())
    )
    return query.all()
