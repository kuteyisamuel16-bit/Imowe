import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from google import genai
from google.genai import types

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas
from app.config import settings

logger = logging.getLogger("imowe.ai_tutor")

router = APIRouter(prefix="/ai-tutor", tags=["ai-tutor"])

client = genai.Client(
    api_key=settings.GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=20000),
) if settings.GEMINI_API_KEY else None

GEMINI_MODEL = "gemini-3.6-flash"

SYSTEM_PROMPT_BASE = (
    "You are IMOWE's AI Tutor - a friendly, encouraging study assistant for "
    "university students. Reply like a normal person texting, not an essay: "
    "2-4 short sentences by default, plain conversational language, no headers, "
    "no numbered lists, no bold formatting unless it's a single key term. "
    "Only go longer, more structured, or more detailed if the student explicitly "
    "asks you to explain further, give examples, break something down, or quiz them. "
    "When in doubt, keep it brief and ask a quick follow-up question instead of "
    "over-explaining. Always finish your sentence or thought completely - never "
    "stop mid-word or mid-idea."
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


def _get_owned_material(material_id: str, db: Session, user: models.User) -> models.Material:
    material = (
        db.query(models.Material)
        .join(models.StudySpace, models.Material.study_space_id == models.StudySpace.id)
        .filter(models.Material.id == material_id, models.StudySpace.user_id == user.id)
        .first()
    )
    if not material:
        raise HTTPException(status_code=404, detail="Material not found.")
    return material


def _build_context(payload, db, current_user):
    """
    Grounding priority: a specific material (isolated - only that material's
    own extracted_text, never another material's) takes precedence over a
    course-level (study_space) grounding, which takes precedence over a
    general untethered conversation.
    """
    study_space = None
    material = None
    system_prompt = SYSTEM_PROMPT_BASE

    if payload.material_id:
        material = _get_owned_material(payload.material_id, db, current_user)
        study_space = material.study_space
        label = material.filename
        if material.extracted_text:
            system_prompt += (
                f" The student is asking about a specific material titled '{label}'. "
                "Answer ONLY using the content of this material below. If the answer isn't "
                "covered in it, say so honestly instead of guessing.\n\n"
                f"MATERIAL CONTENT:\n{material.extracted_text[:12000]}"
            )
        else:
            system_prompt += (
                f" The student is asking about a material titled '{label}', but it hasn't "
                "finished processing yet or has no extracted text - let them know."
            )
    elif payload.study_space_id:
        study_space = _get_owned_study_space(payload.study_space_id, db, current_user)
        system_prompt += (
            f" The student is currently studying '{study_space.course.title}'"
            f" ({study_space.course.subtitle or 'no subtitle set'})."
            " Keep explanations relevant to this course where possible."
        )

    user_msg = models.AIInteraction(
        user_id=current_user.id,
        study_space_id=study_space.id if study_space else None,
        material_id=material.id if material else None,
        role="user",
        content=payload.message,
    )
    db.add(user_msg)
    db.commit()

    history_query = db.query(models.AIInteraction).filter(models.AIInteraction.user_id == current_user.id)
    if material:
        # Strict isolation: only this material's own thread, nothing else.
        history_query = history_query.filter(models.AIInteraction.material_id == material.id)
    else:
        history_query = history_query.filter(
            models.AIInteraction.material_id.is_(None),
            models.AIInteraction.study_space_id == (study_space.id if study_space else None),
        )

    history = history_query.order_by(models.AIInteraction.created_at.desc()).limit(6).all()
    history.reverse()

    contents = [
        types.Content(
            role=("user" if m.role == "user" else "model"),
            parts=[types.Part(text=m.content)],
        )
        for m in history
    ]
    return study_space, material, system_prompt, contents


def _raise_for_gemini_error(e: Exception):
    if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
        logger.warning("Gemini daily quota exceeded")
        raise HTTPException(
            status_code=429,
            detail="AI Tutor has hit its daily usage limit. Please try again later, or ask the app owner to upgrade the Gemini API plan.",
        )
    logger.exception("Gemini call failed")
    raise HTTPException(status_code=502, detail=f"AI Tutor request failed: {e}")


@router.post("/chat", response_model=schemas.ChatMessageOut)
async def chat(
    payload: schemas.ChatMessageIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not client:
        raise HTTPException(status_code=503, detail="AI Tutor isn't configured yet - GEMINI_API_KEY is missing.")

    study_space, material, system_prompt, contents = _build_context(payload, db, current_user)

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=500),
            ),
            timeout=15,
        )
        reply_text = response.text
    except asyncio.TimeoutError:
        logger.exception("Gemini call timed out after 15s")
        raise HTTPException(status_code=504, detail="AI Tutor timed out reaching Gemini - check outbound network access from the server.")
    except Exception as e:
        _raise_for_gemini_error(e)

    assistant_msg = models.AIInteraction(
        user_id=current_user.id,
        study_space_id=study_space.id if study_space else None,
        material_id=material.id if material else None,
        role="assistant",
        content=reply_text,
    )
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)
    return assistant_msg


@router.post("/chat/stream")
async def chat_stream(
    payload: schemas.ChatMessageIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not client:
        raise HTTPException(status_code=503, detail="AI Tutor isn't configured yet - GEMINI_API_KEY is missing.")

    study_space, material, system_prompt, contents = _build_context(payload, db, current_user)

    async def event_generator():
        full_text = ""
        try:
            stream = await client.aio.models.generate_content_stream(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=500),
            )
            async for chunk in stream:
                if chunk.text:
                    full_text += chunk.text
                    yield f"data: {json.dumps({'delta': chunk.text})}\n\n"
        except Exception as e:
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                logger.warning("Gemini daily quota exceeded")
                yield f"data: {json.dumps({'error': 'AI Tutor has hit its daily usage limit. Please try again later.'})}\n\n"
            else:
                logger.exception("Gemini stream failed")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        assistant_msg = models.AIInteraction(
            user_id=current_user.id,
            study_space_id=study_space.id if study_space else None,
            material_id=material.id if material else None,
            role="assistant",
            content=full_text,
        )
        db.add(assistant_msg)
        db.commit()
        db.refresh(assistant_msg)
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/messages", response_model=list[schemas.ChatMessageOut])
def get_messages(
    study_space_id: str | None = None,
    material_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.AIInteraction).filter(models.AIInteraction.user_id == current_user.id)
    if material_id:
        query = query.filter(models.AIInteraction.material_id == material_id)
    else:
        query = query.filter(models.AIInteraction.material_id.is_(None), models.AIInteraction.study_space_id == study_space_id)
    return query.order_by(models.AIInteraction.created_at.asc()).all()
