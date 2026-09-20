def generate_quiz_questions(context_text: str, num_questions: int = 5) -> list[dict]:
    """
    Asks Gemini for multiple-choice questions grounded in the given text.
    Returns a list of dicts: {question, options: [4 strings], correct_index, topic}.
    Returns [] if generation or parsing fails.
    """
    if not client or not context_text.strip():
        return []

    prompt = (
        f"Based on the study material below, write {num_questions} multiple-choice "
        "quiz questions to test understanding. Each question must have exactly 4 "
        "options with exactly one correct answer, and a short 'topic' label (2-4 words) "
        "identifying which specific concept the question tests - this is used to track "
        "which topics the student is strong or weak in, so make topic labels specific "
        "and reusable (e.g. 'Agenda Setting Theory', not just 'Media Theories'). "
        "Respond with ONLY valid JSON in this exact shape, nothing else:\n"
        '[{"question": "...", "options": ["...", "...", "...", "..."], "correct_index": 0, "topic": "..."}]\n\n'
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
                q.setdefault("topic", "General")
                cleaned.append(q)
        return cleaned
    except Exception:
        return []
