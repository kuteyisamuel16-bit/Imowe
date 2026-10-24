"""
app/extract.py - turns an uploaded file into plain text for Course Memory.

  PDF            pypdf (already installed); scanned PDFs fall back to Gemini reading the pages
  Word  .docx    read straight from the file (standard library only)
  PowerPoint .pptx  slide text + speaker notes (standard library only)
  Text  .txt .md .csv
  Images .png .jpg .jpeg .webp   Gemini reads the text (printed or handwritten) from the picture

extract_text() never raises: on any problem it returns "" and the material is simply
saved without text.
"""
import logging
import os
import re
import xml.etree.ElementTree as ET
import zipfile

logger = logging.getLogger("imowe.extract")

SUPPORTED = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".txt": "text",
    ".md": "text",
    ".csv": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
}
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

LEGACY = {".doc": "Word .doc", ".ppt": "PowerPoint .ppt"}
SUPPORTED_LABEL = "PDF, Word (.docx), PowerPoint (.pptx), text (.txt, .md, .csv), or an image (.png, .jpg, .webp)"

MAX_ZIP_MEMBER_BYTES = 30 * 1024 * 1024   # refuse to inflate anything bigger (zip-bomb guard)
MAX_AI_READ_BYTES = 18 * 1024 * 1024      # Gemini accepts about 20 MB inline


def unsupported_message(ext: str) -> str:
    if ext in LEGACY:
        return f"{LEGACY[ext]} files can't be read directly. Please save it as {ext}x or as a PDF and upload that."
    return f"This file type isn't supported. Upload a {SUPPORTED_LABEL}."


def _read_member(z: zipfile.ZipFile, name: str) -> bytes:
    info = z.getinfo(name)
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        raise ValueError(f"{name} is too large")
    return z.read(name)


def _docx_text(path: str) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(_read_member(z, "word/document.xml"))
    paragraphs = []
    for p in root.iter(ns + "p"):
        parts = []
        for el in p.iter():
            if el.tag == ns + "t":
                parts.append(el.text or "")
            elif el.tag == ns + "tab":
                parts.append("\t")
            elif el.tag in (ns + "br", ns + "cr"):
                parts.append("\n")
        line = "".join(parts)
        if line.strip():
            paragraphs.append(line)
    return "\n".join(paragraphs)


def _pptx_text(path: str) -> str:
    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

    def paragraphs(xml_bytes: bytes) -> list[str]:
        root = ET.fromstring(xml_bytes)
        out = []
        for p in root.iter(a + "p"):
            line = "".join(t.text or "" for t in p.iter(a + "t"))
            if line.strip():
                out.append(line)
        return out

    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        slide_nums = sorted(
            int(m.group(1)) for n in names if (m := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", n))
        )
        blocks = []
        for num in slide_nums:
            lines = paragraphs(_read_member(z, f"ppt/slides/slide{num}.xml"))
            notes_name = f"ppt/notesSlides/notesSlide{num}.xml"
            notes = paragraphs(_read_member(z, notes_name)) if notes_name in names else []
            # the notes page repeats the slide number as a lone digit line - drop it
            notes = [n for n in notes if not n.strip().isdigit()]
            if not lines and not notes:
                continue
            block = f"Slide {num}:\n" + "\n".join(lines)
            if notes:
                block += "\nSpeaker notes: " + " ".join(notes)
            blocks.append(block)
    return "\n\n".join(blocks)


def _pdf_text(path: str) -> tuple[str, int]:
    from pypdf import PdfReader
    reader = PdfReader(path)
    pages = len(reader.pages)
    return "\n".join((page.extract_text() or "") for page in reader.pages), pages


def _read_with_ai(path: str, mime: str) -> str:
    """Gemini reads the text out of a scanned PDF or an image (handwriting included)."""
    if os.path.getsize(path) > MAX_AI_READ_BYTES:
        logger.warning("File too large for AI text reading: %s", path)
        return ""
    from app import study_ai
    with open(path, "rb") as f:
        return study_ai.read_file_with_ai(f.read(), mime)


def extract_text(path: str, ext: str) -> str:
    kind = SUPPORTED.get(ext.lower())
    try:
        if kind == "pdf":
            text, pages = _pdf_text(path)
            # almost no text for the number of pages = a scan; let Gemini read the pages
            if len(text.strip()) < 40 * max(pages, 1):
                ocr = _read_with_ai(path, "application/pdf")
                if len(ocr) > len(text.strip()):
                    return ocr
            return text
        if kind == "docx":
            return _docx_text(path)
        if kind == "pptx":
            return _pptx_text(path)
        if kind == "image":
            return _read_with_ai(path, IMAGE_MIME[ext.lower()])
        if kind == "text":
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        logger.exception("Text extraction failed for %s", path)
    return ""
