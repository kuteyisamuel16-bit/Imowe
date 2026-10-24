"""
app/study_ai.py - Gemini helpers for the study tools (course summary, flashcards).

Uses its own client with a longer timeout than app.ai: these calls read a lot of
text and write a lot, so they can legitimately take longer than the 20s used for chat.
"""
import json
import logging
import re

from google import genai
from google.genai import types

from app.config import settings
from app.ai import GEMINI_MODEL

logger = logging.getLogger("imowe.study_ai")

_client = genai.Client(
    api_key=settings.GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=60000),
) if settings.GEMINI_API_KEY else None


def is_configured() -> bool:
    return _client is not None


def parse_json(raw: str | None):
    """Parse model output as JSON, tolerating code fences or stray text around it."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    match = re.search(r"[\[{].*[\]}]", raw, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None


def _ask_json(prompt: str):
    response = _client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", max_output_tokens=4500),
    )
    return parse_json(response.text)


def _strs(value, limit: int, max_len: int = 400) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text[:max_len])
        if len(out) >= limit:
            break
    return out


def clean_summary(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    overview = str(data.get("overview", "")).strip()[:1500]
    key_points = _strs(data.get("key_points"), 12)

    sections = []
    for sec in (data.get("sections") if isinstance(data.get("sections"), list) else [])[:8]:
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title", "")).strip()[:150]
        points = _strs(sec.get("points"), 8)
        if title and points:
            sections.append({"title": title, "points": points})

    terms = []
    for t in (data.get("key_terms") if isinstance(data.get("key_terms"), list) else [])[:12]:
        if not isinstance(t, dict):
            continue
        term = str(t.get("term", "")).strip()[:100]
        meaning = str(t.get("meaning", "")).strip()[:400]
        if term and meaning:
            terms.append({"term": term, "meaning": meaning})

    if not overview and not key_points:
        return None
    return {"overview": overview, "key_points": key_points, "sections": sections, "key_terms": terms}


def clean_flashcards(data, limit: int) -> list[dict]:
    if isinstance(data, dict):  # some replies wrap the list: {"flashcards": [...]}
        data = next((v for v in data.values() if isinstance(v, list)), [])
    if not isinstance(data, list):
        return []
    cards = []
    for item in data:
        if not isinstance(item, dict):
            continue
        front = str(item.get("front", "")).strip()[:400]
        back = str(item.get("back", "")).strip()[:800]
        topic = str(item.get("topic", "")).strip()[:150] or "General"
        if front and back:
            cards.append({"front": front, "back": back, "topic": topic})
        if len(cards) >= limit:
            break
    return cards


def generate_summary(material_text: str, course_title: str, topics: list[str]) -> dict | None:
    if not _client or not material_text.strip():
        return None
    topic_hint = f"Topics already identified in these materials: {', '.join(topics)}.\n" if topics else ""
    prompt = (
        f"You are preparing a study summary of the course '{course_title}' for a university student, "
        "based ONLY on the material below. Do not add facts that are not in the material.\n"
        f"{topic_hint}"
        "Respond with ONLY valid JSON in exactly this shape:\n"
        '{"overview": "2-4 sentences saying what the material is about and why it matters", '
        '"key_points": ["5-10 short, self-contained takeaways"], '
        '"sections": [{"title": "a main theme", "points": ["2-5 short points"]}], '
        '"key_terms": [{"term": "...", "meaning": "a plain-language definition"}]}\n'
        "Use 2-6 sections and up to 10 key terms. Write clearly, as if explaining to a first-year student.\n\n"
        f"MATERIAL:\n{material_text}"
    )
    try:
        return clean_summary(_ask_json(prompt))
    except Exception:
        logger.exception("Summary generation failed")
        return None


def generate_flashcards(material_text: str, topics: list[str], count: int) -> list[dict]:
    if not _client or not material_text.strip():
        return []
    topic_hint = (
        "Give each card a 'topic'; where one fits, use EXACTLY one of these existing topic names: "
        f"{', '.join(topics)}.\n" if topics else "Give each card a short 'topic' label (2-4 words).\n"
    )
    prompt = (
        f"Create {count} flashcards for a university student, based ONLY on the material below. "
        "Each card tests one idea: the 'front' is a clear question or a term, and the 'back' is a short, "
        "accurate answer (1-3 sentences). Cover the material broadly; do not repeat ideas.\n"
        f"{topic_hint}"
        "Respond with ONLY valid JSON: "
        '[{"front": "...", "back": "...", "topic": "..."}]\n\n'
        f"MATERIAL:\n{material_text}"
    )
    try:
        return clean_flashcards(_ask_json(prompt), count)
    except Exception:
        logger.exception("Flashcard generation failed")
        return []


def read_file_with_ai(data: bytes, mime_type: str) -> str:
    """Transcribe the text in a scanned PDF or an image (printed or handwritten). Returns '' on failure."""
    if not _client or not data:
        return ""
    prompt = (
        "Transcribe ALL the text in this file exactly as written, in reading order. "
        "Keep headings and list structure as plain text. If there is a diagram, chart or "
        "picture that carries information, describe it briefly in [square brackets]. "
        "Output only the transcription - no introduction and no commentary."
    )
    try:
        response = _client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Part.from_bytes(data=data, mime_type=mime_type), prompt],
            config=types.GenerateContentConfig(max_output_tokens=16000),
        )
        return (response.text or "").strip()
    except Exception:
        logger.exception("AI file reading failed")
        return ""


def clean_video_script(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    scenes = []
    for sc in (data.get("scenes") if isinstance(data.get("scenes"), list) else [])[:8]:
        if not isinstance(sc, dict):
            continue
        heading = str(sc.get("heading", "")).strip()[:90]
        narration = str(sc.get("narration", "")).strip()[:900]
        if not heading or not narration:
            continue
        scenes.append({"heading": heading, "bullets": _strs(sc.get("bullets"), 4, 130), "narration": narration})
    if not scenes:
        return None
    title = str(data.get("title", "")).strip()[:120] or scenes[0]["heading"]
    return {"title": title, "scenes": scenes}


def generate_video_script(answer_text: str, question: str, course_title: str | None) -> dict | None:
    """Turn a tutor answer into a short scene-by-scene lesson (slides + narration)."""
    if not _client or not answer_text.strip():
        return None
    where = f"for the course '{course_title}' " if course_title else ""
    asked = f"The student asked: {question.strip()[:400]}\n" if (question or "").strip() else ""
    prompt = (
        f"You are a friendly tutor making a short narrated slide lesson {where}for a university student. "
        "Base it ONLY on the explanation below; do not add new facts.\n"
        f"{asked}"
        "Split it into 4-6 scenes that build on each other: start by framing the idea, end with a quick recap. "
        "Each scene has a short 'heading', 2-4 short on-screen 'bullets' (a few words each), and a 'narration' "
        "of 2-4 spoken sentences written to be read aloud: natural, warm, no markdown, no lists, no symbols.\n"
        "Write in the same language as the explanation. Respond with ONLY valid JSON: "
        '{"title": "...", "scenes": [{"heading": "...", "bullets": ["..."], "narration": "..."}]}\n\n'
        f"EXPLANATION:\n{answer_text[:6000]}"
    )
    try:
        return clean_video_script(_ask_json(prompt))
    except Exception:
        logger.exception("Video script generation failed")
        return None
