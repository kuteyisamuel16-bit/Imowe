import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import edge_tts

from app.database import get_db
from app.deps import get_current_user
from app import models, ai

logger = logging.getLogger("imowe.narration")

router = APIRouter(prefix="/narration", tags=["narration"])

DEFAULT_VOICE = "en-US-AriaNeural"
MAX_CHARS = 4000


class NarrationIn(BaseModel):
    text: str
    voice: str | None = None


async def _speak(text: str, voice: str = DEFAULT_VOICE):
    async def audio_stream():
        try:
            communicate = edge_tts.Communicate(text[:MAX_CHARS], voice)
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    yield chunk["data"]
        except Exception:
            logger.exception("Edge TTS generation failed")
            return

    return StreamingResponse(
        audio_stream(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/speak")
async def speak(
    payload: NarrationIn,
    current_user: models.User = Depends(get_current_user),
):
    """
    Free text-to-speech via edge-tts (no API key, no billing required).
    Reads exactly what's given - no summarization. Streams MP3 audio
    directly, no files saved anywhere.
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    return await _speak(text, payload.voice or DEFAULT_VOICE)


@router.get("/material/{material_id}")
async def speak_material_summary(
    material_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Summarizes a material (with examples where useful) via Gemini, then
    reads THAT aloud - not a word-for-word reading of the source text.
    """
    material = (
        db.query(models.Material)
        .join(models.StudySpace, models.Material.study_space_id == models.StudySpace.id)
        .filter(models.Material.id == material_id, models.StudySpace.user_id == current_user.id)
        .first()
    )
    if not material:
        raise HTTPException(status_code=404, detail="Material not found.")
    if not material.extracted_text:
        raise HTTPException(status_code=400, detail="This material has no extracted text yet.")

    import asyncio
    script = await asyncio.to_thread(ai.generate_narration_script, material.extracted_text, material.filename)
    if not script:
        raise HTTPException(status_code=502, detail="Could not generate a summary for narration.")

    return await _speak(script)
