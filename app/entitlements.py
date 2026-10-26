"""
app/entitlements.py - who may use IMOWE's AI features, and how much.

Plans (each one lasts a 4-month semester = 120 days):
    trial     7 days free, 10 hours total
    basic     $20, 20 hours of use per month
    standard  $30, 50 hours of use per month
    full      $50, unlimited

"Use" is measured in seconds charged per action (see COST). Reading your own materials is free.
A usage month is a 30-day block counted from the day the plan started.

Nothing is blocked until you set the environment variable BILLING_ENFORCED=true. Before that,
usage is still recorded so you can see real numbers first.
"""
import os
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models

SEMESTER_DAYS = 120
TRIAL_DAYS = 7
USAGE_MONTH_DAYS = 30
HOUR = 3600

PLANS = {
    "basic":    {"name": "Basic",    "price": 20, "hours": 20},
    "standard": {"name": "Standard", "price": 30, "hours": 50},
    "full":     {"name": "Full",     "price": 50, "hours": None},
}
TRIAL = {"name": "Free trial", "hours": 10}

# seconds of "use" charged each time
COST = {
    "tutor": 60, "video": 300, "narration": 30, "narration_material": 120,
    "summary": 120, "flashcards": 120, "quiz": 120, "chapters": 120,
    "meeting": 0, "meeting_ping": 20,
}


def enforced() -> bool:
    return (os.getenv("BILLING_ENFORCED") or "").strip().lower() in ("1", "true", "yes", "on")


def get_subscription(db: Session, user_id: str) -> models.Subscription:
    """The student's plan. Everyone gets the free trial the first time we look."""
    sub = db.query(models.Subscription).filter(models.Subscription.user_id == user_id).first()
    if not sub:
        now = datetime.utcnow()
        sub = models.Subscription(
            user_id=user_id, plan="trial", starts_at=now, expires_at=now + timedelta(days=TRIAL_DAYS),
        )
        db.add(sub)
        db.commit()
    return sub


def limit_seconds(sub: models.Subscription) -> int | None:
    hours = TRIAL["hours"] if sub.plan == "trial" else PLANS.get(sub.plan, {}).get("hours")
    return None if hours is None else int(hours * HOUR)


def window_start(sub: models.Subscription, now: datetime) -> datetime:
    if sub.plan == "trial":
        return sub.starts_at
    blocks = max(0, (now - sub.starts_at).days // USAGE_MONTH_DAYS)
    return sub.starts_at + timedelta(days=USAGE_MONTH_DAYS * blocks)


def used_seconds(db: Session, user_id: str, since: datetime) -> int:
    total = (
        db.query(func.coalesce(func.sum(models.UsageEvent.seconds), 0))
        .filter(models.UsageEvent.user_id == user_id, models.UsageEvent.created_at >= since)
        .scalar()
    )
    return int(total or 0)


def snapshot(db: Session, user_id: str) -> dict:
    sub = get_subscription(db, user_id)
    now = datetime.utcnow()
    active = sub.expires_at > now
    lim = limit_seconds(sub)
    used = used_seconds(db, user_id, window_start(sub, now))
    ws = window_start(sub, now)
    return {
        "plan": sub.plan,
        "plan_name": TRIAL["name"] if sub.plan == "trial" else PLANS.get(sub.plan, {}).get("name", sub.plan),
        "status": ("trial" if sub.plan == "trial" else "active") if active else "expired",
        "expires_at": sub.expires_at,
        "days_left": max(0, (sub.expires_at - now).days + (1 if (sub.expires_at - now).seconds else 0)) if active else 0,
        "used_hours": round(used / HOUR, 1),
        "limit_hours": None if lim is None else round(lim / HOUR, 1),
        "resets_at": None if (lim is None or sub.plan == "trial") else ws + timedelta(days=USAGE_MONTH_DAYS),
        "enforced": enforced(),
    }


def add_usage(db: Session, user_id: str, seconds: int) -> None:
    if seconds <= 0:
        return
    db.add(models.UsageEvent(user_id=user_id, seconds=int(seconds)))
    db.commit()


def ensure_allowed(db: Session, user_id: str) -> None:
    """Raises 402 if billing is enforced and this student has no usable plan or hours."""
    sub = get_subscription(db, user_id)
    if not enforced():
        return
    now = datetime.utcnow()
    if sub.expires_at <= now:
        raise HTTPException(status_code=402, detail={
            "code": "subscription_required",
            "msg": "Your free trial has ended. Choose a plan to keep learning." if sub.plan == "trial"
                   else "Your plan has ended. Renew to keep using IMOWE.",
        })
    lim = limit_seconds(sub)
    if lim is not None and used_seconds(db, user_id, window_start(sub, now)) >= lim:
        raise HTTPException(status_code=402, detail={
            "code": "usage_limit",
            "msg": "You've used all your hours for now. Upgrade your plan or wait for your hours to reset.",
        })


def gate(kind: str):
    """Use as: dependencies=[Depends(entitlements.gate("tutor"))] on an AI route."""
    cost = COST[kind]

    def dependency(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
        ensure_allowed(db, user.id)
        add_usage(db, user.id, cost)

    return dependency
