"""
app/routers/chapters.py - chapter-by-chapter summaries of one long material.

Long documents are split where the document itself changes chapter (lines like "Chapter 3",
"Unit 2", "4.1 Introduction"); if it has no clear headings it is cut into equal parts.
Gemini then summarises a few sections at a time (in parallel), and the result is saved.
"""
import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas, study_ai
from app.ratelimit import summary_generate_limit

logger = logging.getLogger("imowe.chapters")

router = APIRouter(prefix="/study-spaces/{study_space_id}/materials/{material_id}/chapters", tags=["chapters"])

MIN_CHARS = 6000          # shorter than this and a normal read-through is easier
MAX_SECTIONS = 12
MIN_SECTION = 800         # a "chapter" shorter than this is merged into the one before
TARGET_PART = 8000        # size of each part when there are no headings
PROMPT_CAP = 6000         # most text of one section sent to Gemini (start + end are kept)
BATCH = 4                 # sections per Gemini call

HEADING = re.compile(
    r"^\s*(?:(?i:chapter|unit|module|lesson|part|topic|week)\s+(?:\d+|[ivxlc]+)\b[^\n]{0,90}"
    r"|\d{1,2}(?:\.\d{1,2})?[.)]?\s+[A-Z][^\n]{3,80})\s*$"
)


def split_sections(text: str) -> list[tuple[str, str]]:
    """Returns [(heading_hint, text), ...]."""
    text = (text or "").replace("\r", "")
    lines = text.split("\n")
    marks = []  # (char offset, heading)
    pos = 0
    for line in lines:
        stripped = line.strip()
        if 3 <= len(stripped) <= 100 and HEADING.match(line) and not stripped.endswith((".", ",", ";", ":")):
            marks.append((pos, stripped))
        pos += len(line) + 1

    sections: list[tuple[str, str]] = []
    if len(marks) >= 2:
        bounds = [m[0] for m in marks] + [len(text)]
        if marks[0][0] > MIN_SECTION:
            sections.append(("", text[: marks[0][0]]))
        for i, (start, heading) in enumerate(marks):
            sections.append((heading, text[start: bounds[i + 1]]))
        merged: list[tuple[str, str]] = []
        for hint, body in sections:
            if merged and len(body.strip()) < MIN_SECTION:
                merged[-1] = (merged[-1][0], merged[-1][1] + "\n" + body)
            else:
                merged.append((hint, body))
        sections = merged

    if len(sections) < 2:
        flat = " ".join(text.split())
        n = min(MAX_SECTIONS, max(2, math.ceil(len(flat) / TARGET_PART)))
        step = len(flat) / n
        sections, start = [], 0
        for i in range(1, n + 1):
            end = len(flat) if i == n else int(step * i)
            if i < n:  # end on a sentence if one is close by
                cut = flat.rfind(". ", end - 600, end + 600)
                end = cut + 1 if cut > start else end
            sections.append(("", flat[start:end].strip()))
            start = end

    while len(sections) > MAX_SECTIONS:  # merge the two shortest neighbours until it fits
        i = min(range(len(sections) - 1), key=lambda k: len(sections[k][1]) + len(sections[k + 1][1]))
        sections[i:i + 2] = [(sections[i][0], sections[i][1] + "\n" + sections[i + 1][1])]
    return [(h, b.strip()) for h, b in sections if b.strip()]


def _for_prompt(body: str) -> str:
    body = " ".join(body.split())
    if len(body) <= PROMPT_CAP:
        return body
    return body[: PROMPT_CAP - 1500] + " [...] " + body[-1500:]


def _get_material(db: Session, user: models.User, study_space_id: str, material_id: str) -> models.Material:
    space = (
        db.query(models.StudySpace)
        .filter(models.StudySpace.id == study_space_id, models.StudySpace.user_id == user.id)
        .first()
    )
    material = (
        db.query(models.Material)
        .filter(models.Material.id == material_id, models.Material.study_space_id == study_space_id)
        .first()
        if space else None
    )
    if not material:
        raise HTTPException(status_code=404, detail="Material not found.")
    return material


def _out(row: models.MaterialChapterSummary | None, material: models.Material) -> schemas.ChaptersOut:
    if not row:
        return schemas.ChaptersOut()
    return schemas.ChaptersOut(
        chapters=json.loads(row.content),
        stale=(row.source_len != len(material.extracted_text or "")),
        updated_at=row.updated_at,
    )


@router.get("", response_model=schemas.ChaptersOut)
def get_chapters(
    study_space_id: str,
    material_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    material = _get_material(db, current_user, study_space_id, material_id)
    row = db.query(models.MaterialChapterSummary).filter(models.MaterialChapterSummary.material_id == material.id).first()
    return _out(row, material)


@router.post("/generate", response_model=schemas.ChaptersOut, dependencies=[Depends(summary_generate_limit)])
def generate_chapters(
    study_space_id: str,
    material_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not study_ai.is_configured():
        raise HTTPException(status_code=503, detail="Summaries aren't configured yet - GEMINI_API_KEY is missing.")
    material = _get_material(db, current_user, study_space_id, material_id)
    text = (material.extracted_text or "").strip()
    if len(text) < MIN_CHARS:
        raise HTTPException(status_code=400, detail="This material is short enough to read in one go. Chapter summaries are for longer documents.")

    space = db.query(models.StudySpace).filter(models.StudySpace.id == study_space_id).first()
    course_title = space.course.title if space and space.course else None

    sections = split_sections(text)
    batches = [sections[i:i + BATCH] for i in range(0, len(sections), BATCH)]
    prepared = [[(hint, _for_prompt(body)) for hint, body in b] for b in batches]

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda b: study_ai.generate_chapter_batch(b, course_title), prepared))

    chapters, missing = [], 0
    for batch, res in zip(batches, results):
        for i, (hint, _body) in enumerate(batch):
            item = res[i] if res and i < len(res) else None
            if item:
                chapters.append(item)
            else:
                missing += 1
    if not chapters or missing > len(sections) // 2:
        raise HTTPException(status_code=502, detail="Couldn't write the chapter summaries right now. Please try again.")

    row = db.query(models.MaterialChapterSummary).filter(models.MaterialChapterSummary.material_id == material.id).first()
    payload = json.dumps(chapters)
    if row:
        row.content, row.source_len = payload, len(material.extracted_text or "")
    else:
        row = models.MaterialChapterSummary(
            material_id=material.id, study_space_id=study_space_id,
            content=payload, source_len=len(material.extracted_text or ""),
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return _out(row, material)
