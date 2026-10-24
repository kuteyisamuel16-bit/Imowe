import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from google import genai
from google.genai import types

from app.database import get_db, SessionLocal
from app.deps import get_current_user
from app import models, schemas, rag
from app.config import settings

logger = logging.getLogger("imowe.ai_tutor")

router = APIRouter(prefix="/ai-tutor", tags=["ai-tutor"])

client = genai.Client(
    api_key=settings.GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=20000),
) if settings.GEMINI_API_KEY else None

GEMINI_MODEL = "gemini-3.6-flash"
MAX_TITLE_LEN = 48

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


def _get_owned_thread(thread_id: str, db: Session, user: models.User) -> models.ChatThread:
    thread = (
        db.query(models.ChatThread)
        .filter(models.ChatThread.id == thread_id, models.ChatThread.user_id == user.id)
        .first()
    )
    if not thread:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return thread


def _make_title(message: str) -> str:
    text = " ".join(message.split())
    if not text:
        return "New chat"
    return text if len(text) <= MAX_TITLE_LEN else text[:MAX_TITLE_LEN].rstrip() + "…"


def _format_excerpts(hits: list[tuple[str, str]]) -> str:
    return "\n\n---\n\n".join(f"[{filename}]\n{content}" for filename, content in hits)


# ---------- Thread management (ChatGPT-style history) ----------

@router.get("/threads", response_model=list[schemas.ChatThreadOut])
def list_threads(
    study_space_id: str | None = None,
    material_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.ChatThread).filter(models.ChatThread.user_id == current_user.id)
    if material_id:
        query = query.filter(models.ChatThread.material_id == material_id)
    else:
        query = query.filter(models.ChatThread.material_id.is_(None), models.ChatThread.study_space_id == study_space_id)
    return query.order_by(models.ChatThread.updated_at.desc()).all()


@router.get("/threads/{thread_id}/messages", response_model=list[schemas.ChatMessageOut])
def get_thread_messages(
    thread_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_owned_thread(thread_id, db, current_user)
    msgs = (
        db.query(models.AIInteraction)
        .filter(models.AIInteraction.thread_id == thread_id)
        .order_by(models.AIInteraction.created_at.asc())
        .all()
    )
    ratings = {}
    if msgs:
        rows = (
            db.query(models.MessageFeedback)
            .filter(
                models.MessageFeedback.user_id == current_user.id,
                models.MessageFeedback.message_id.in_([m.id for m in msgs]),
            )
            .all()
        )
        ratings = {r.message_id: r.rating for r in rows}
    return [
        schemas.ChatMessageOut(
            id=m.id,
            thread_id=m.thread_id,
            role=m.role,
            content=m.content,
            created_at=m.created_at,
            feedback=ratings.get(m.id),
        )
        for m in msgs
    ]


@router.delete("/threads/{thread_id}", status_code=204)
def delete_thread(
    thread_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    thread = _get_owned_thread(thread_id, db, current_user)
    message_ids = db.query(models.AIInteraction.id).filter(models.AIInteraction.thread_id == thread.id)
    # feedback rows reference the messages, so they have to go first
    db.query(models.MessageFeedback).filter(
        models.MessageFeedback.message_id.in_(message_ids)
    ).delete(synchronize_session=False)
    db.query(models.AIInteraction).filter(models.AIInteraction.thread_id == thread.id).delete(synchronize_session=False)
    db.delete(thread)
    db.commit()
    return None


# ---------- Feedback (like / dislike) ----------

@router.put("/messages/{message_id}/feedback", response_model=schemas.FeedbackOut)
def set_feedback(
    message_id: str,
    payload: schemas.FeedbackIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    message = (
        db.query(models.AIInteraction)
        .filter(
            models.AIInteraction.id == message_id,
            models.AIInteraction.user_id == current_user.id,
            models.AIInteraction.role == "assistant",
        )
        .first()
    )
    if not message:
        raise HTTPException(status_code=404, detail="Message not found.")

    existing = (
        db.query(models.MessageFeedback)
        .filter(models.MessageFeedback.user_id == current_user.id, models.MessageFeedback.message_id == message_id)
        .first()
    )

    if payload.rating == "none":
        if existing:
            db.delete(existing)
            db.commit()
        return schemas.FeedbackOut(message_id=message_id, rating=None)

    if existing:
        existing.rating = payload.rating
        existing.reason = payload.reason
    else:
        db.add(models.MessageFeedback(
            user_id=current_user.id,
            message_id=message_id,
            rating=payload.rating,
            reason=payload.reason,
        ))
    db.commit()
    return schemas.FeedbackOut(message_id=message_id, rating=payload.rating)


# ---------- Chat ----------

def _build_context(payload, db, current_user):
    """
    Resolves (or lazily creates) the ChatThread this message belongs to,
    retrieves the most relevant chunks of the student's materials (Course
    Memory), builds the Gemini system prompt + recent contents, and saves the
    user's message.

    Returns (thread_id, system_prompt, contents, sources) - plain strings and
    lists, never ORM objects: touching an ORM object after this function's
    commit can raise DetachedInstanceError in the streaming endpoint, whose
    generator runs after the request's DB session has already closed.
    """
    study_space_id = None
    material_id = None
    material_label = None
    material_fallback_text = None
    course_line = None

    if payload.material_id:
        material = _get_owned_material(payload.material_id, db, current_user)
        material_id = material.id
        study_space_id = material.study_space_id
        material_label = material.filename
        material_fallback_text = material.extracted_text
    elif payload.study_space_id:
        study_space = _get_owned_study_space(payload.study_space_id, db, current_user)
        study_space_id = study_space.id
        course_line = (
            f" The student is currently studying '{study_space.course.title}'"
            f" ({study_space.course.subtitle or 'no subtitle set'})."
            " Keep explanations relevant to this course where possible."
        )

    if payload.thread_id:
        thread = _get_owned_thread(payload.thread_id, db, current_user)
    else:
        thread = models.ChatThread(
            user_id=current_user.id,
            study_space_id=study_space_id,
            material_id=material_id,
            title=_make_title(payload.message),
        )
        db.add(thread)
        db.commit()
        db.refresh(thread)
    thread_id = thread.id

    user_msg = models.AIInteraction(
        user_id=current_user.id,
        thread_id=thread_id,
        study_space_id=study_space_id,
        material_id=material_id,
        role="user",
        content=payload.message,
    )
    db.add(user_msg)
    db.commit()

    from app.activity import log_event
    log_event(db, current_user.id, study_space_id, "tutor_message")
    db.query(models.ChatThread).filter(models.ChatThread.id == thread_id).update({"updated_at": datetime.utcnow()})
    db.commit()

    history = (
        db.query(models.AIInteraction)
        .filter(models.AIInteraction.thread_id == thread_id)
        .order_by(models.AIInteraction.created_at.desc())
        .limit(12)
        .all()
    )
    history.reverse()

    # ----- Course Memory: retrieve the chunks relevant to this question -----
    # Short follow-ups ("can you give an example?") embed badly on their own, so
    # the search query is the student's last two messages together.
    hits: list[tuple[str, str]] = []
    if material_id or study_space_id:
        recent_user_msgs = [m.content for m in history if m.role == "user"][-2:]
        query = " ".join(recent_user_msgs)[:1000]
        hits = rag.retrieve(
            db,
            query,
            study_space_id=None if material_id else study_space_id,
            material_id=material_id,
        )
    sources = list(dict.fromkeys(filename for filename, _ in hits))

    # ----- system prompt -----
    system_prompt = SYSTEM_PROMPT_BASE
    if material_id:
        if hits:
            system_prompt += (
                f" The student is asking about a specific material titled '{material_label}'. "
                "Answer ONLY using the excerpts below from this material. If the answer isn't "
                "covered in them, say so honestly instead of guessing.\n\n"
                f"EXCERPTS:\n{_format_excerpts(hits)}"
            )
        elif material_fallback_text:
            # Not indexed (e.g. embeddings unavailable) - fall back to raw text.
            system_prompt += (
                f" The student is asking about a specific material titled '{material_label}'. "
                "Answer ONLY using the content of this material below. If the answer isn't "
                "covered in it, say so honestly instead of guessing.\n\n"
                f"MATERIAL CONTENT:\n{material_fallback_text[:12000]}"
            )
        else:
            system_prompt += (
                f" The student is asking about a material titled '{material_label}', but it hasn't "
                "finished processing yet or has no extracted text - let them know."
            )
    elif course_line:
        system_prompt += course_line
        if hits:
            system_prompt += (
                " Below are excerpts from the student's own uploaded course materials, most "
                "relevant first. Prefer them when answering, and mention which material an idea "
                "comes from when it feels natural. If the question isn't covered by them, answer "
                "from general knowledge but say it isn't in their materials.\n\n"
                f"EXCERPTS:\n{_format_excerpts(hits)}"
            )

    contents = [
        types.Content(
            role=("user" if m.role == "user" else "model"),
            parts=[types.Part(text=m.content)],
        )
        for m in history
    ]
    return thread_id, system_prompt, contents, sources


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

    thread_id, system_prompt, contents, _sources = await run_in_threadpool(
        _build_context, payload, db, current_user
    )
    user_id = current_user.id

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=900),
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
        user_id=user_id,
        thread_id=thread_id,
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

    # Retrieval makes network calls (embeddings), so keep it off the event loop.
    thread_id, system_prompt, contents, sources = await run_in_threadpool(
        _build_context, payload, db, current_user
    )
    user_id = current_user.id

    async def event_generator():
        yield f"data: {json.dumps({'thread_id': thread_id})}\n\n"
        if sources:
            yield f"data: {json.dumps({'sources': sources})}\n\n"

        full_text = ""
        try:
            stream = await client.aio.models.generate_content_stream(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=900),
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

        message_id = None
        write_db = SessionLocal()
        try:
            assistant_msg = models.AIInteraction(
                user_id=user_id,
                thread_id=thread_id,
                role="assistant",
                content=full_text,
            )
            write_db.add(assistant_msg)
            write_db.commit()
            message_id = assistant_msg.id  # lets the UI attach like/dislike to this reply
        finally:
            write_db.close()

        yield f"data: {json.dumps({'done': True, 'message_id': message_id})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
