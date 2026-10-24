"""
app/rag.py - Course Memory (retrieval-augmented generation).

  index_material()  split a material's text into chunks, embed them, store them
  retrieve()        embed a question and return the most relevant chunks

Embeddings are stored as JSON in the material_chunks table and compared in
Python, which is plenty fast for a student's course (hundreds of chunks) and
needs no pgvector. If you later outgrow it, swap retrieve() for a pgvector query.
"""
import json
import logging
import math
import time

from google.genai import types
from sqlalchemy.orm import Session

from app import models, ai

logger = logging.getLogger("imowe.rag")

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
CHUNK_SIZE = 1000      # characters per chunk
CHUNK_OVERLAP = 150    # characters shared between neighbouring chunks
EMBED_BATCH = 50
TOP_K = 5
MAX_AUTO_INDEX = 10    # materials indexed lazily per question, at most
FAIL_COOLDOWN = 300    # seconds to stop retrying after an embedding failure

_last_failure = 0.0


def chunk_text(text: str) -> list[str]:
    """Split text into ~CHUNK_SIZE pieces, preferring to end on a sentence."""
    text = " ".join((text or "").split())
    chunks: list[str] = []
    n = len(text)
    start = 0
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        if end < n:
            cut = text.rfind(". ", start + CHUNK_SIZE // 2, end)
            if cut != -1:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed_texts(texts: list[str], task_type: str) -> list[list[float]]:
    """Embed texts with Gemini. Raises on failure; callers decide how to cope."""
    if not ai.client or not texts:
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        resp = ai.client.models.embed_content(
            model=EMBED_MODEL,
            contents=batch,
            config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=EMBED_DIM),
        )
        out.extend(_normalize(list(e.values)) for e in resp.embeddings)
    return out


def index_material(db: Session, material: models.Material) -> int:
    """(Re)build the chunks for one material. Fails soft: returns 0 on any error."""
    global _last_failure
    text = (material.extracted_text or "").strip()
    if not text or not ai.client:
        return 0
    try:
        chunks = chunk_text(text)
        vectors = embed_texts(chunks, "RETRIEVAL_DOCUMENT")
        if len(vectors) != len(chunks):
            return 0

        db.query(models.MaterialChunk).filter(models.MaterialChunk.material_id == material.id).delete(
            synchronize_session=False
        )
        for i, (content, vec) in enumerate(zip(chunks, vectors)):
            db.add(models.MaterialChunk(
                material_id=material.id,
                study_space_id=material.study_space_id,
                chunk_index=i,
                content=content,
                embedding=json.dumps([round(x, 5) for x in vec]),
            ))
        db.commit()
        return len(chunks)
    except Exception:
        db.rollback()
        _last_failure = time.time()
        logger.exception("Indexing failed for material %s", material.id)
        return 0


def _ensure_indexed(db: Session, study_space_id: str | None, material_id: str | None) -> None:
    """Index anything uploaded before Course Memory existed (or that failed earlier)."""
    if time.time() - _last_failure < FAIL_COOLDOWN:
        return
    q = db.query(models.Material).filter(models.Material.extracted_text.isnot(None))
    if material_id:
        q = q.filter(models.Material.id == material_id)
    else:
        q = q.filter(models.Material.study_space_id == study_space_id)
    for m in q.limit(MAX_AUTO_INDEX).all():
        if not (m.extracted_text or "").strip():
            continue
        has_chunks = db.query(models.MaterialChunk.id).filter(models.MaterialChunk.material_id == m.id).first()
        if not has_chunks:
            index_material(db, m)


def retrieve(
    db: Session,
    query: str,
    study_space_id: str | None = None,
    material_id: str | None = None,
    k: int = TOP_K,
) -> list[tuple[str, str]]:
    """
    Return up to k (filename, chunk_text) pairs, most relevant first, from one
    material or a whole course. Returns [] when nothing is indexed or on error,
    so callers can fall back to their old behaviour.
    """
    if not ai.client or not query.strip() or not (study_space_id or material_id):
        return []
    try:
        _ensure_indexed(db, study_space_id, material_id)

        q = (
            db.query(models.MaterialChunk, models.Material.filename)
            .join(models.Material, models.Material.id == models.MaterialChunk.material_id)
        )
        if material_id:
            q = q.filter(models.MaterialChunk.material_id == material_id)
        else:
            q = q.filter(models.MaterialChunk.study_space_id == study_space_id)
        rows = q.all()
        if not rows:
            return []

        qvec = embed_texts([query], "RETRIEVAL_QUERY")[0]
        scored = []
        for chunk, filename in rows:
            vec = json.loads(chunk.embedding)
            score = sum(a * b for a, b in zip(qvec, vec))  # both unit-length -> cosine
            scored.append((score, filename, chunk.content))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [(filename, content) for _, filename, content in scored[:k]]
    except Exception:
        logger.exception("Retrieval failed")
        return []
