import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import edge_tts

from app.deps import get_current_user
from app import models

logger = logging.getLogger("imowe.narration")

router = APIRouter(prefix="/narration", tags=["narration"])

DEFAULT_VOICE = "en-US-AriaNeural"
MAX_CHARS = 4000


class NarrationIn(BaseModel):
    text: str
    voice: str | None = None


@router.post("/speak")
async def speak(
    payload: NarrationIn,
    current_user: models.User = Depends(get_current_user),
):
    """
    Free text-to-speech via edge-tts (no API key, no billing required).
    Streams MP3 audio directly - no files saved anywhere.
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    text = text[:MAX_CHARS]
    voice = payload.voice or DEFAULT_VOICE

    async def audio_stream():
        try:
            communicate = edge_tts.Communicate(text, voice)
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
