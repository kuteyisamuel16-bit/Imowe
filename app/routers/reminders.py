"""
app/routers/reminders.py - the daily deadline reminder email.

FastAPI Cloud has no built-in scheduler, so a free outside service (cron-job.org) calls this
endpoint once a day. Set the environment variable CRON_SECRET to a long random string and send
the same value in a header called X-Cron-Secret. Without a matching secret the endpoint refuses.

Each student gets at most ONE email per day, only if something is due today, tomorrow or within
3 days, and never if they turned reminders off in Settings.

Optional env: REMINDER_UTC_OFFSET_HOURS (default 1 = Lagos) decides what "today" means.
"""
import hmac
import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app import models, email_service

router = APIRouter(prefix="/internal", tags=["internal"])

MAX_USERS_PER_RUN = 500
WINDOW_DAYS = 3


def _today() -> date:
    try:
        offset = float(os.getenv("REMINDER_UTC_OFFSET_HOURS", "1"))
    except ValueError:
        offset = 1.0
    return (datetime.utcnow() + timedelta(hours=offset)).date()


def _collect(db: Session, today: date) -> list[dict]:
    """One entry per student who should get an email today."""
    last = today + timedelta(days=WINDOW_DAYS)
    rows = (
        db.query(models.Deadline)
        .filter(models.Deadline.done.is_(False), models.Deadline.due_date <= last)
        .order_by(models.Deadline.due_date, models.Deadline.due_time)
        .all()
    )
    by_user: dict[str, list] = {}
    for d in rows:
        by_user.setdefault(d.user_id, []).append(d)

    jobs = []
    for user_id, items in by_user.items():
        if len(jobs) >= MAX_USERS_PER_RUN:
            break
        upcoming = [d for d in items if d.due_date >= today]
        if not upcoming:      # overdue items alone don't trigger an email
            continue
        prefs = db.query(models.UserPrefs).filter(models.UserPrefs.user_id == user_id).first()
        if prefs and (not prefs.email_reminders or prefs.last_reminder_on == today):
            continue
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user or not user.email:
            continue
        if not prefs:
            prefs = models.UserPrefs(user_id=user_id, email_reminders=True)
            db.add(prefs)
        prefs.last_reminder_on = today
        jobs.append({
            "email": user.email,
            "name": user.full_name,
            "items": [
                {"title": d.title, "kind": d.kind, "days": (d.due_date - today).days,
                 "due": d.due_date.strftime("%a %d %b"), "time": d.due_time}
                for d in items
            ],
        })
    db.commit()
    return jobs


def _send_all(jobs: list[dict]) -> None:
    for j in jobs:
        email_service.send_deadline_digest_email(j["email"], j["name"], j["items"])


@router.api_route("/send-deadline-reminders", methods=["GET", "POST"])
def send_deadline_reminders(background_tasks: BackgroundTasks, x_cron_secret: str | None = Header(default=None)):
    secret = os.getenv("CRON_SECRET") or ""
    if not secret or not x_cron_secret or not hmac.compare_digest(secret, x_cron_secret):
        raise HTTPException(status_code=403, detail="Not allowed.")
    db = SessionLocal()
    try:
        jobs = _collect(db, _today())
    finally:
        db.close()
    background_tasks.add_task(_send_all, jobs)
    return {"emails_queued": len(jobs)}
