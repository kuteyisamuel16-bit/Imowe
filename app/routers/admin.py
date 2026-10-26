"""
app/routers/admin.py - numbers for the owner: sign-ups, plans, revenue and how much students actually use.

Only people whose email is listed in the environment variable ADMIN_EMAILS (comma separated, e.g.
samuelkuteyi7@gmail.com) can see this. Everyone else gets a plain "not allowed".
Nothing here shows messages, materials or anything a student wrote. Only counts and totals.
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, entitlements as ent

router = APIRouter(prefix="/admin", tags=["admin"])

BUCKETS = [(0, 1, "under 1h"), (1, 5, "1-5h"), (5, 10, "5-10h"), (10, 20, "10-20h"), (20, 50, "20-50h"), (50, 10**9, "50h+")]


def _admin_emails() -> set[str]:
    return {e.strip().lower() for e in (os.getenv("ADMIN_EMAILS") or "").split(",") if e.strip()}


def _is_admin(user: models.User) -> bool:
    return (user.email or "").strip().lower() in _admin_emails()


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Not allowed.")
    return user


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
    return round(sorted_vals[i], 1)


@router.get("/me")
def admin_me(user: models.User = Depends(get_current_user)):
    return {"is_admin": _is_admin(user)}


@router.get("/overview")
def overview(db: Session = Depends(get_db), admin: models.User = Depends(require_admin)):
    now = datetime.utcnow()
    d7, d14, d30 = now - timedelta(days=7), now - timedelta(days=14), now - timedelta(days=30)

    # ---- people
    total_users = db.query(func.count(models.User.id)).scalar() or 0
    new7 = db.query(func.count(models.User.id)).filter(models.User.created_at >= d7).scalar() or 0
    new30 = db.query(func.count(models.User.id)).filter(models.User.created_at >= d30).scalar() or 0
    signups = defaultdict(int)
    for (day, n) in (
        db.query(func.date(models.User.created_at), func.count(models.User.id))
        .filter(models.User.created_at >= d30).group_by(func.date(models.User.created_at)).all()
    ):
        signups[str(day)] += n
    signups_daily = [
        {"day": (now - timedelta(days=i)).strftime("%Y-%m-%d"), "n": signups.get((now - timedelta(days=i)).strftime("%Y-%m-%d"), 0)}
        for i in range(29, -1, -1)
    ]
    active7 = db.query(func.count(func.distinct(models.UsageEvent.user_id))).filter(models.UsageEvent.created_at >= d7).scalar() or 0
    active30 = db.query(func.count(func.distinct(models.UsageEvent.user_id))).filter(models.UsageEvent.created_at >= d30).scalar() or 0

    # ---- plans
    plan_counts = {"trial": 0, "basic": 0, "standard": 0, "full": 0, "expired": 0}
    plan_of: dict[str, str] = {}
    for sub in db.query(models.Subscription).all():
        key = "expired" if sub.expires_at <= now else sub.plan
        plan_counts[key] = plan_counts.get(key, 0) + 1
        plan_of[sub.user_id] = key
    paying_now = plan_counts["basic"] + plan_counts["standard"] + plan_counts["full"]

    # ---- revenue (US dollars, paid only)
    paid = db.query(models.Payment).filter(models.Payment.status == "paid").all()
    revenue_total = sum(p.amount for p in paid)
    revenue_30 = sum(p.amount for p in paid if p.paid_at and p.paid_at >= d30)
    by_plan = {pid: {"count": 0, "amount": 0} for pid in ent.PLANS}
    by_month = defaultdict(int)
    for p in paid:
        if p.plan in by_plan:
            by_plan[p.plan]["count"] += 1
            by_plan[p.plan]["amount"] += p.amount
        if p.paid_at:
            by_month[p.paid_at.strftime("%Y-%m")] += p.amount
    months = []
    y, m = now.year, now.month
    for _ in range(6):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    revenue_months = [{"month": k, "amount": by_month.get(k, 0)} for k in reversed(months)]
    paying_customers = len({p.user_id for p in paid})

    # ---- usage over the last 30 days
    hours_by_user: dict[str, float] = defaultdict(float)
    for uid, secs in (
        db.query(models.UsageEvent.user_id, func.sum(models.UsageEvent.seconds))
        .filter(models.UsageEvent.created_at >= d30).group_by(models.UsageEvent.user_id).all()
    ):
        hours_by_user[uid] = float(secs or 0) / 3600.0
    vals = sorted(hours_by_user.values())
    buckets = [{"label": lab, "users": sum(1 for v in vals if lo <= v < hi)} for lo, hi, lab in BUCKETS]
    daily = defaultdict(float)
    for day, secs in (
        db.query(func.date(models.UsageEvent.created_at), func.sum(models.UsageEvent.seconds))
        .filter(models.UsageEvent.created_at >= d14).group_by(func.date(models.UsageEvent.created_at)).all()
    ):
        daily[str(day)] += float(secs or 0) / 3600.0
    usage_daily = [
        {"day": (now - timedelta(days=i)).strftime("%Y-%m-%d"), "hours": round(daily.get((now - timedelta(days=i)).strftime("%Y-%m-%d"), 0.0), 1)}
        for i in range(13, -1, -1)
    ]

    # how close each plan's students come to its limit
    limits = {"trial": ent.TRIAL["hours"], "basic": ent.PLANS["basic"]["hours"], "standard": ent.PLANS["standard"]["hours"]}
    plan_usage = []
    for key in ("trial", "basic", "standard", "full"):
        uids = [u for u, k in plan_of.items() if k == key]
        used = [hours_by_user.get(u, 0.0) for u in uids]
        lim = limits.get(key)
        plan_usage.append({
            "plan": key,
            "students": len(uids),
            "limit_hours": lim,
            "avg_hours": round(sum(used) / len(used), 1) if used else 0.0,
            "max_hours": round(max(used), 1) if used else 0.0,
            "near_limit": sum(1 for v in used if lim and v >= lim * 0.8),
            "over_limit": sum(1 for v in used if lim and v >= lim),
        })

    # plain-language hints
    tips = []
    for row in plan_usage:
        if row["limit_hours"] and row["students"] >= 5:
            share = row["near_limit"] / row["students"]
            if share >= 0.3:
                tips.append(f"{round(share * 100)}% of {row['plan']} students used 80% or more of their {row['limit_hours']:.0f} hours. Consider raising that limit or the price.")
            elif share == 0 and row["avg_hours"] < row["limit_hours"] * 0.15:
                tips.append(f"{row['plan'].title()} students use very little of their {row['limit_hours']:.0f} hours (average {row['avg_hours']}h). You could lower the limit or the price.")
    if not tips:
        tips.append("Not enough usage yet for a recommendation. Check again after a few weeks and at least 5 students per plan.")

    return {
        "generated_at": now.isoformat(),
        "people": {"total": total_users, "new_7d": new7, "new_30d": new30, "active_7d": active7, "active_30d": active30,
                   "signups_daily": signups_daily},
        "plans": {**plan_counts, "paying_now": paying_now},
        "revenue": {"currency": "USD", "total": revenue_total, "last_30d": revenue_30, "paying_customers": paying_customers,
                    "by_plan": [{"plan": k, **v} for k, v in by_plan.items()], "months": revenue_months,
                    "conversion_percent": round(paying_customers / total_users * 100, 1) if total_users else 0.0},
        "usage": {"students_30d": len(vals), "total_hours_30d": round(sum(vals), 1),
                  "median_hours": _pct(vals, 0.5), "p75_hours": _pct(vals, 0.75), "p90_hours": _pct(vals, 0.9), "max_hours": _pct(vals, 1.0),
                  "buckets": buckets, "daily": usage_daily, "by_plan": plan_usage, "tips": tips},
    }
