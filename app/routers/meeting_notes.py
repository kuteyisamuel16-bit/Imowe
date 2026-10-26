"""
app/routers/meeting_notes.py - text chat inside a study meeting, and saving the chat as notes.

Meetings are audio only and never recorded. The text chat is the only written record, and
everyone is told in the app that chat messages can be saved by anyone in the meeting. Saving
is each person's own choice: it copies the chat into THEIR OWN Study Space as a new material.
"""
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, ai, rag, study_ai, ratelimit, entitlements
from app.activity import log_event

logger = logging.getLogger("imowe.meeting_notes")
router = APIRouter(prefix="/meetings", tags=["meeting-chat"])

MAX_MESSAGE_CHARS = 1000
KEEP_MESSAGES = 1000          # per meeting, newest
MAX_STORED_CHARS = 200000


class ChatIn(BaseModel):
    text: str


class ChatOut(BaseModel):
    id: int
    user_id: str
    name: str
    text: str
    created_at: datetime


class SaveNotesIn(BaseModel):
    study_space_id: str
    summarize: bool = True


def _meeting_and_membership(db: Session, meeting_id: str, user: models.User):
    meeting = db.query(models.Meeting).filter(models.Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    mine = (
        db.query(models.MeetingParticipant)
        .filter(models.MeetingParticipant.meeting_id == meeting.id, models.MeetingParticipant.user_id == user.id)
        .first()
    )
    if not mine:
        raise HTTPException(status_code=403, detail="You weren't in this meeting.")
    return meeting, mine


@router.get("/{meeting_id}/messages", response_model=list[ChatOut])
def list_messages(
    meeting_id: str,
    after: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    meeting, _ = _meeting_and_membership(db, meeting_id, user)
    return (
        db.query(models.MeetingMessage)
        .filter(models.MeetingMessage.meeting_id == meeting.id, models.MeetingMessage.id > after)
        .order_by(models.MeetingMessage.id)
        .limit(200)
        .all()
    )


@router.post("/{meeting_id}/messages", response_model=ChatOut, dependencies=[Depends(ratelimit.meeting_chat_limit)])
def send_message(
    meeting_id: str,
    payload: ChatIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    meeting, mine = _meeting_and_membership(db, meeting_id, user)
    if meeting.status != "active" or mine.left_at is not None:
        raise HTTPException(status_code=400, detail="This meeting has ended.")
    text = (payload.text or "").replace("\x00", "").strip()[:MAX_MESSAGE_CHARS]
    if not text:
        raise HTTPException(status_code=422, detail="Type a message first.")
    count = db.query(models.MeetingMessage).filter(models.MeetingMessage.meeting_id == meeting.id).count()
    if count >= KEEP_MESSAGES:
        raise HTTPException(status_code=400, detail="This meeting's chat is full.")
    msg = models.MeetingMessage(
        meeting_id=meeting.id, user_id=user.id, name=(user.full_name or "Student")[:150], text=text,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


@router.post("/{meeting_id}/save-notes", dependencies=[Depends(ratelimit.meeting_notes_limit)])
def save_notes(
    meeting_id: str,
    payload: SaveNotesIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    meeting, _ = _meeting_and_membership(db, meeting_id, user)
    space = (
        db.query(models.StudySpace)
        .filter(models.StudySpace.id == payload.study_space_id, models.StudySpace.user_id == user.id)
        .first()
    )
    if not space:
        raise HTTPException(status_code=404, detail="That course was not found.")

    messages = (
        db.query(models.MeetingMessage)
        .filter(models.MeetingMessage.meeting_id == meeting.id)
        .order_by(models.MeetingMessage.id)
        .all()
    )
    if not messages:
        raise HTTPException(status_code=400, detail="There are no chat messages to save yet.")

    people = (
        db.query(models.User.full_name)
        .join(models.MeetingParticipant, models.MeetingParticipant.user_id == models.User.id)
        .filter(models.MeetingParticipant.meeting_id == meeting.id)
        .all()
    )
    names = ", ".join(sorted({(p[0] or "Student") for p in people}))
    when = (meeting.created_at or datetime.utcnow()).strftime("%d %b %Y")
    transcript = "\n".join(f"{m.name}: {m.text}" for m in messages)

    parts = [f"Study meeting: {meeting.title}", f"Date: {when}", f"People: {names}", ""]
    if payload.summarize:
        entitlements.ensure_allowed(db, user.id)
        notes = study_ai.generate_meeting_notes(transcript, meeting.title)
        if notes and (notes["summary"] or notes["key_points"] or notes["action_items"]):
            entitlements.add_usage(db, user.id, entitlements.COST["summary"])
            if notes["summary"]:
                parts += ["SUMMARY", notes["summary"], ""]
            if notes["key_points"]:
                parts += ["KEY POINTS"] + [f"- {p}" for p in notes["key_points"]] + [""]
            if notes["action_items"]:
                parts += ["ACTION ITEMS"] + [f"- {p}" for p in notes["action_items"]] + [""]
    parts += ["CHAT", transcript]
    text = "\n".join(parts).replace("\x00", "")[:MAX_STORED_CHARS]

    material = models.Material(
        study_space_id=space.id,
        filename=f"Meeting notes - {meeting.title}"[:300],
        file_path=None,
        content_type="text/plain",
        material_type=models.MaterialType.recording,
        status=models.MaterialStatus.processing,
    )
    db.add(material)
    db.commit()
    db.refresh(material)
    try:
        topics = ai.extract_topics(text)
        material.extracted_text = text
        material.extracted_topics = json.dumps(topics)
        material.status = models.MaterialStatus.processed if topics else models.MaterialStatus.uploaded
    except Exception:
        logger.exception("Processing meeting notes failed")
        material.extracted_text = text
        material.status = models.MaterialStatus.uploaded
    db.commit()
    db.refresh(material)
    rag.index_material(db, material)
    log_event(db, user.id, space.id, "meeting_notes_saved")
    return {"material_id": material.id, "filename": material.filename, "study_space_id": space.id}
