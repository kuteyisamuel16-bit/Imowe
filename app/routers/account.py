"""
app/routers/account.py - edit your own account: name, institution, programme, password.

PUT  /profile                  change name / institution / programme
POST /profile/change-password  needs the current password
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas
from app.routers.auth import MIN_PASSWORD_LENGTH
from app.security import hash_password, verify_password
from app.ratelimit import password_change_limit

router = APIRouter(prefix="/profile", tags=["profile"])


def _clean(value: str | None, limit: int) -> str | None:
    value = (value or "").strip()[:limit]
    return value or None


@router.put("", response_model=schemas.UserOut)
def update_profile(
    payload: schemas.ProfileUpdateIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    fields = payload.model_fields_set

    if "full_name" in fields:
        name = (payload.full_name or "").strip()[:150]
        if len(name) < 2:
            raise HTTPException(status_code=422, detail="Please enter your full name.")
        current_user.full_name = name

    profile = current_user.academic_profile
    if not profile:
        profile = models.AcademicProfile(user_id=current_user.id)
        db.add(profile)
        current_user.academic_profile = profile
    if "institution" in fields:
        profile.institution = _clean(payload.institution, 200)
    if "programme" in fields:
        profile.programme = _clean(payload.programme, 200)

    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/change-password", response_model=schemas.MessageResponse, dependencies=[Depends(password_change_limit)])
def change_password(
    payload: schemas.ChangePasswordIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Your current password is not correct.")
    if len(payload.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"New password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="Choose a password different from your current one.")
    current_user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return schemas.MessageResponse(message="Password changed.")
