import hashlib
import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas, email_service, ratelimit
from app.routers.prefs import record_terms_acceptance
from app.security import hash_password, verify_password, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("imowe.auth")

GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo?id_token="
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

RESET_TOKEN_TTL_MINUTES = 30
RESET_EMAIL_COOLDOWN_SECONDS = 60
MIN_PASSWORD_LENGTH = 8
FORGOT_PASSWORD_REPLY = "If an account with that email exists, we've sent a password reset link."


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post(
    "/signup",
    response_model=schemas.TokenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ratelimit.signup_limit)],
)
def signup(payload: schemas.SignUpRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    user = models.User(
        full_name=payload.full_name,
        email=payload.email,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.flush()

    profile = models.AcademicProfile(user_id=user.id, level=1, xp=0)
    db.add(profile)
    if payload.accept_terms:
        record_terms_acceptance(db, user.id)

    db.commit()

    background_tasks.add_task(email_service.send_welcome_email, user.email, user.full_name)

    token = create_access_token(subject=user.id)
    return schemas.TokenResponse(access_token=token)


def _verify_google_credential(credential: str) -> dict:
    """Asks Google to check the sign-in token, then checks it was issued for OUR app."""
    client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google sign-in isn't set up on the server yet.")
    try:
        url = GOOGLE_TOKENINFO_URL + urllib.parse.quote(credential.strip(), safe="")
        with urllib.request.urlopen(url, timeout=10) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise HTTPException(status_code=401, detail="Google sign-in failed. Please try again.")
    except Exception:
        logger.exception("Google token check failed")
        raise HTTPException(status_code=502, detail="Couldn't reach Google. Please try again.")

    if info.get("aud") != client_id or info.get("iss") not in GOOGLE_ISSUERS:
        raise HTTPException(status_code=401, detail="Google sign-in failed. Please try again.")
    if str(info.get("email_verified")).lower() != "true" or not info.get("email"):
        raise HTTPException(status_code=401, detail="Your Google email isn't verified.")
    return info


@router.post(
    "/google",
    response_model=schemas.TokenResponse,
    dependencies=[Depends(ratelimit.google_login_limit)],
)
def google_login(payload: schemas.GoogleLoginRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    info = _verify_google_credential(payload.credential)
    email = info["email"].strip().lower()

    user = db.query(models.User).filter(func.lower(models.User.email) == email).first()
    is_new = user is None
    if is_new:
        name = (info.get("name") or email.split("@")[0]).strip()[:150]
        user = models.User(
            full_name=name,
            email=email,
            # Google users have no password of their own; this one can't be guessed.
            hashed_password=hash_password(secrets.token_urlsafe(32)),
        )
        db.add(user)
        db.flush()
        db.add(models.AcademicProfile(user_id=user.id, level=1, xp=0))
        if payload.accept_terms:
            record_terms_acceptance(db, user.id)
        db.commit()
        background_tasks.add_task(email_service.send_welcome_email, user.email, user.full_name)

    return schemas.TokenResponse(access_token=create_access_token(subject=user.id))


@router.post("/login", response_model=schemas.TokenResponse)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # slows password guessing: limited per IP + email, so a shared campus Wi-Fi isn't locked out as a whole
    ratelimit.enforce(
        f"{ratelimit.client_ip(request)}|{form_data.username.strip().lower()}",
        "login", ratelimit.LOGIN_RULES, "logging in",
    )
    user = db.query(models.User).filter(models.User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    token = create_access_token(subject=user.id)
    return schemas.TokenResponse(access_token=token)


@router.post("/forgot-password", response_model=schemas.MessageResponse, dependencies=[Depends(ratelimit.forgot_password_limit)])
def forgot_password(
    payload: schemas.ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Always answers the same way, whether or not the email has an account, so the
    endpoint can't be used to discover who is registered. The email itself is
    sent in the background so response time doesn't give it away either.
    """
    email = payload.email.strip().lower()
    user = db.query(models.User).filter(func.lower(models.User.email) == email).first()

    if user:
        now = datetime.utcnow()
        recently_sent = (
            db.query(models.PasswordResetToken.id)
            .filter(
                models.PasswordResetToken.user_id == user.id,
                models.PasswordResetToken.created_at >= now - timedelta(seconds=RESET_EMAIL_COOLDOWN_SECONDS),
            )
            .first()
        )
        if not recently_sent:
            raw_token = secrets.token_urlsafe(32)
            db.add(models.PasswordResetToken(
                user_id=user.id,
                token_hash=_hash_token(raw_token),
                expires_at=now + timedelta(minutes=RESET_TOKEN_TTL_MINUTES),
            ))
            db.commit()
            background_tasks.add_task(
                email_service.send_password_reset_email,
                user.email,
                user.full_name,
                email_service.build_reset_link(raw_token),
            )

    return schemas.MessageResponse(message=FORGOT_PASSWORD_REPLY)


@router.post("/reset-password", response_model=schemas.MessageResponse)
def reset_password(payload: schemas.ResetPasswordRequest, db: Session = Depends(get_db)):
    if len(payload.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    now = datetime.utcnow()
    row = (
        db.query(models.PasswordResetToken)
        .filter(
            models.PasswordResetToken.token_hash == _hash_token(payload.token.strip()),
            models.PasswordResetToken.used_at.is_(None),
            models.PasswordResetToken.expires_at > now,
        )
        .first()
    )
    user = db.query(models.User).filter(models.User.id == row.user_id).first() if row else None
    if not row or not user:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired. Please request a new one.")

    user.hashed_password = hash_password(payload.new_password)
    # one use only - and any other outstanding links for this account die too
    db.query(models.PasswordResetToken).filter(
        models.PasswordResetToken.user_id == user.id,
        models.PasswordResetToken.used_at.is_(None),
    ).update({"used_at": now}, synchronize_session=False)
    db.commit()

    return schemas.MessageResponse(message="Your password has been updated. You can now log in.")
