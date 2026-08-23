"""
Shared Gemini client and helpers, used by ai_tutor, materials (topic
extraction) and quiz (question generation).
"""
import json
from google import genai
from google.genai import types

from app.config import settings
client = genai.Client(
    api_key=settings.GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=20000),  # 20s, in ms - fail fast instead of hanging
) if settings.GEMINI_API_KEY else None

GEMINI_MODEL = "gemini-2.5-flash"


def is_configured() -> bool:
    return client is not None


def extract_topics(text: str) -> list[str]:
    """
    Given raw material text, ask Gemini for a short list of key topics.
    Returns a plain list of strings; empty list if AI Tutor isn't configured
    or the model's response can't be parsed.
    """
    if not client or not text.strip():
        return []

    prompt = (
        "Read the following study material and list the 5-8 most important "
        "topics or concepts it covers. Respond with ONLY a JSON array of short "
        "strings, nothing else, e.g. [\"Topic one\", \"Topic two\"].\n\n"
        f"MATERIAL:\n{text[:12000]}"
    )
    try:
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").replace("json", "", 1).strip()
        topics = json.loads(raw)
        if isinstance(topics, list):
            return [str(t) for t in topics][:8]
    except Exception:
        pass
    return []


def generate_quiz_questions(context_text: str, num_questions: int = 5) -> list[dict]:
    """
    Asks Gemini for multiple-choice questions grounded in the given text.
    Returns a list of dicts: {question, options: [4 strings], correct_index}.
    Returns [] if generation or parsing fails.
    """
    if not client or not context_text.strip():
        return []

    prompt = (
        f"Based on the study material below, write {num_questions} multiple-choice "
        "quiz questions to test understanding. Each question must have exactly 4 "
        "options with exactly one correct answer. Respond with ONLY valid JSON in "
        "this exact shape, nothing else:\n"
        '[{"question": "...", "options": ["...", "...", "...", "..."], "correct_index": 0}]\n\n'
        f"MATERIAL:\n{context_text[:12000]}"
    )
    try:
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").replace("json", "", 1).strip()
        questions = json.loads(raw)
        cleaned = []
        for q in questions:
            if (
                isinstance(q, dict)
                and "question" in q
                and "options" in q
                and len(q["options"]) == 4
                and "correct_index" in q
                and 0 <= q["correct_index"] <= 3
            ):
                cleaned.append(q)
        return cleaned
    except Exception:
        return []
