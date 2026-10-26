"""
app/routers/meetings.py - live audio study meetings (up to 8 people, join by code).

Voice runs on LiveKit (hosted WebRTC). This file only decides WHO may join: it creates meetings,
enforces the 8-person cap, and signs a short-lived LiveKit token for each person who is allowed in.
Environment variables (from your LiveKit project settings):
    LIVEKIT_URL         e.g. wss://your-project.livekit.cloud
    LIVEKIT_API_KEY
    LIVEKIT_API_SECRET
No extra Python packages are needed (the token is signed with the standard library).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas
from app.ratelimit import meeting_create_limit, meeting_join_limit

try:
    from app.config import settings
except Exception:  # pragma: no cover
    settings = None

logger = logging.getLogger("imowe.meetings")

from app import entitlements

router = APIRouter(prefix="/meetings", tags=["meetings"])

MAX_PARTICIPANTS = 8
PRESENCE_SECONDS = 60       # someone who has not pinged for this long no longer counts toward the cap
TOKEN_TTL_SECONDS = 3 * 3600
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L, easy to read out loud


def _cfg(name: str):
    return os.getenv(name) or (getattr(settings, name, None) if settings else None) or None


def _ws_url(url: str) -> str:
    """LiveKit shows the project address as https://... in some places and wss://... in others; the browser needs wss://."""
    url = (url or "").strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    if "://" not in url:
        return "wss://" + url
    return url


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_livekit_token(api_key: str, api_secret: str, identity: str, name: str, room: str) -> str:
    """A LiveKit access token (JWT, HS256). Audio only: the person may publish their microphone, nothing else."""
    now = int(time.time())
    claims = {
        "iss": api_key,
        "sub": identity,
        "name": name,
        "nbf": now - 10,
        "exp": now + TOKEN_TTL_SECONDS,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
            "canPublishSources": ["microphone"],
        },
    }
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(api_secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def _present_cutoff() -> datetime:
    return datetime.utcnow() - timedelta(seconds=PRESENCE_SECONDS)


def _people(db: Session, meeting: models.Meeting) -> list[schemas.MeetingPerson]:
    rows = (
        db.query(models.MeetingParticipant, models.User)
        .join(models.User, models.User.id == models.MeetingParticipant.user_id)
        .filter(
            models.MeetingParticipant.meeting_id == meeting.id,
            models.MeetingParticipant.left_at.is_(None),
            models.MeetingParticipant.last_seen >= _present_cutoff(),
        )
        .order_by(models.MeetingParticipant.joined_at)
        .all()
    )
    return [
        schemas.MeetingPerson(user_id=u.id, name=u.full_name or "Student", is_host=(u.id == meeting.host_id))
        for _, u in rows
    ]


def _meeting_out(db: Session, meeting: models.Meeting, user: models.User) -> schemas.MeetingOut:
    host = db.query(models.User).filter(models.User.id == meeting.host_id).first()
    return schemas.MeetingOut(
        id=meeting.id,
        code=meeting.code,
        title=meeting.title,
        host_name=(host.full_name if host else None) or "Host",
        is_host=(meeting.host_id == user.id),
        max_participants=meeting.max_participants,
        status=meeting.status,
    )


def _get_meeting(db: Session, meeting_id: str) -> models.Meeting:
    meeting = db.query(models.Meeting).filter(models.Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    return meeting


@router.post("", response_model=schemas.MeetingOut, dependencies=[Depends(meeting_create_limit), Depends(entitlements.gate("meeting"))])
def create_meeting(
    payload: schemas.MeetingCreateIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    first = (current_user.full_name or "Study").split()[0]
    title = (payload.title or "").strip()[:120] or f"{first}'s study meeting"
    for _ in range(8):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        meeting = models.Meeting(code=code, host_id=current_user.id, title=title, max_participants=MAX_PARTICIPANTS)
        db.add(meeting)
        try:
            db.commit()
            db.refresh(meeting)
            return _meeting_out(db, meeting, current_user)
        except IntegrityError:
            db.rollback()  # the code was already taken: try another
    raise HTTPException(status_code=500, detail="Couldn't create a meeting code. Please try again.")


@router.post("/join", response_model=schemas.MeetingJoinOut, dependencies=[Depends(meeting_join_limit), Depends(entitlements.gate("meeting"))])
def join_meeting(
    payload: schemas.MeetingJoinIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    api_key, api_secret, url = _cfg("LIVEKIT_API_KEY"), _cfg("LIVEKIT_API_SECRET"), _cfg("LIVEKIT_URL")
    if not (api_key and api_secret and url):
        raise HTTPException(status_code=503, detail="Study meetings aren't switched on yet.")

    code = "".join(ch for ch in (payload.code or "").upper() if ch.isalnum())
    meeting = db.query(models.Meeting).filter(models.Meeting.code == code).first()
    if not meeting or meeting.status != "active":
        raise HTTPException(status_code=404, detail="No active meeting has that code.")

    now = datetime.utcnow()
    mine = (
        db.query(models.MeetingParticipant)
        .filter(models.MeetingParticipant.meeting_id == meeting.id, models.MeetingParticipant.user_id == current_user.id)
        .first()
    )
    present = (
        db.query(models.MeetingParticipant)
        .filter(
            models.MeetingParticipant.meeting_id == meeting.id,
            models.MeetingParticipant.left_at.is_(None),
            models.MeetingParticipant.last_seen >= _present_cutoff(),
            models.MeetingParticipant.user_id != current_user.id,
        )
        .count()
    )
    if present >= meeting.max_participants:
        raise HTTPException(status_code=409, detail=f"This meeting is full ({meeting.max_participants} people).")

    if mine:
        mine.left_at = None
        mine.last_seen = now
    else:
        db.add(models.MeetingParticipant(meeting_id=meeting.id, user_id=current_user.id, joined_at=now, last_seen=now))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # two taps at once: the row already exists, which is fine

    token = make_livekit_token(api_key, api_secret, current_user.id, current_user.full_name or "Student", meeting.id)
    return schemas.MeetingJoinOut(
        meeting=_meeting_out(db, meeting, current_user),
        token=token,
        url=_ws_url(url),
        identity=current_user.id,
        participants=_people(db, meeting),
    )


@router.post("/{meeting_id}/ping", response_model=schemas.MeetingPingOut)
def ping_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Called every ~20 seconds by the app: keeps this person counted as present and returns who is here."""
    meeting = _get_meeting(db, meeting_id)
    mine = (
        db.query(models.MeetingParticipant)
        .filter(models.MeetingParticipant.meeting_id == meeting.id, models.MeetingParticipant.user_id == current_user.id)
        .first()
    )
    if mine and meeting.status == "active" and mine.left_at is None:
        mine.last_seen = datetime.utcnow()
        db.commit()
        entitlements.add_usage(db, current_user.id, entitlements.COST["meeting_ping"])
    return schemas.MeetingPingOut(status=meeting.status, participants=_people(db, meeting))


@router.post("/{meeting_id}/leave", status_code=204)
def leave_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    meeting = _get_meeting(db, meeting_id)
    mine = (
        db.query(models.MeetingParticipant)
        .filter(models.MeetingParticipant.meeting_id == meeting.id, models.MeetingParticipant.user_id == current_user.id)
        .first()
    )
    if mine and mine.left_at is None:
        mine.left_at = datetime.utcnow()
        db.commit()
    return None


@router.post("/{meeting_id}/end", status_code=204)
def end_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Host only: ends the meeting for everyone (their apps notice on the next ping)."""
    meeting = _get_meeting(db, meeting_id)
    if meeting.host_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the host can end the meeting.")
    if meeting.status != "ended":
        now = datetime.utcnow()
        meeting.status = "ended"
        meeting.ended_at = now
        db.query(models.MeetingParticipant).filter(
            models.MeetingParticipant.meeting_id == meeting.id, models.MeetingParticipant.left_at.is_(None)
        ).update({"left_at": now}, synchronize_session=False)
        db.commit()
    return None
