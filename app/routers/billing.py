"""
app/routers/billing.py - plans, checkout and payment confirmation (Flutterwave).

Environment variables:
    FLW_SECRET_KEY    your Flutterwave secret key (test key first, e.g. FLWSECK_TEST-...)
    FLW_SECRET_HASH   any long random text; ALSO type it into the Flutterwave dashboard
                      (Settings > Webhooks > Secret hash). Used to prove webhooks are really from Flutterwave.
    FRONTEND_URL      where index.html is hosted (the student comes back here after paying)
    BILLING_ENFORCED  "true" to start blocking users without a plan (leave unset while testing)

Money is never trusted from the browser. After the student pays, WE ask Flutterwave directly
whether the payment really happened, for the right amount, in USD, for that exact reference.
"""
import hmac
import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, ratelimit, email_service, entitlements as ent

router = APIRouter(prefix="/billing", tags=["billing"])
logger = logging.getLogger("imowe.billing")

FLW_BASE = "https://api.flutterwave.com/v3"
CURRENCY = "USD"


def _secret() -> str:
    key = (os.getenv("FLW_SECRET_KEY") or "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="Payments aren't set up yet. Please try again later.")
    return key


def _flw(method: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        FLW_BASE + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {_secret()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        logger.error("Flutterwave %s %s -> %s %s", method, path, e.code, body)
        raise HTTPException(status_code=502, detail="The payment service refused the request. Please try again.")
    except Exception:
        logger.exception("Flutterwave call failed")
        raise HTTPException(status_code=502, detail="Couldn't reach the payment service. Please try again.")


# ---------------------------------------------------------------- schemas
class CheckoutIn(BaseModel):
    plan: str


class VerifyIn(BaseModel):
    transaction_id: str
    tx_ref: str


def _plans_out() -> list[dict]:
    return [
        {"id": pid, "name": p["name"], "price": p["price"], "hours": p["hours"], "days": ent.SEMESTER_DAYS}
        for pid, p in ent.PLANS.items()
    ]


# ---------------------------------------------------------------- routes
@router.get("/status")
def billing_status(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return {**ent.snapshot(db, user.id), "plans": _plans_out(), "currency": CURRENCY}


@router.post("/checkout", dependencies=[Depends(ratelimit.billing_limit)])
def checkout(payload: CheckoutIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    plan = ent.PLANS.get((payload.plan or "").strip().lower())
    if not plan:
        raise HTTPException(status_code=422, detail="Choose Basic, Standard or Full.")
    plan_id = payload.plan.strip().lower()
    base = (os.getenv("FRONTEND_URL") or "").rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="Payments aren't set up yet. Please try again later.")

    tx_ref = f"imowe-{secrets.token_hex(10)}"
    pay = models.Payment(user_id=user.id, plan=plan_id, amount=plan["price"], currency=CURRENCY, tx_ref=tx_ref)
    db.add(pay)
    db.commit()

    data = _flw("POST", "/payments", {
        "tx_ref": tx_ref,
        "amount": plan["price"],
        "currency": CURRENCY,
        "redirect_url": base + "/",
        "customer": {"email": user.email, "name": user.full_name or "Student"},
        "customizations": {
            "title": "IMOWE",
            "description": f"{plan['name']} plan - one semester ({ent.SEMESTER_DAYS // 30} months)",
        },
        "meta": {"user_id": user.id, "plan": plan_id},
    })
    link = (data.get("data") or {}).get("link")
    if data.get("status") != "success" or not link:
        raise HTTPException(status_code=502, detail="Couldn't start the payment. Please try again.")
    return {"link": link}


def _apply_payment(db: Session, pay: models.Payment, txn: dict, background_tasks: BackgroundTasks | None):
    """Give the plan. Safe to call twice for the same payment: the second call does nothing."""
    if pay.status == "paid":
        return
    now = datetime.utcnow()
    pay.status = "paid"
    pay.paid_at = now
    pay.provider_txn_id = str(txn.get("id") or "")

    sub = ent.get_subscription(db, pay.user_id)
    if sub.plan == pay.plan and sub.expires_at > now:
        sub.expires_at = sub.expires_at + timedelta(days=ent.SEMESTER_DAYS)     # renewing early: add on
    else:
        sub.plan = pay.plan
        sub.starts_at = now
        sub.expires_at = now + timedelta(days=ent.SEMESTER_DAYS)
    db.commit()

    user = db.query(models.User).filter(models.User.id == pay.user_id).first()
    if user and background_tasks is not None:
        background_tasks.add_task(
            email_service.send_payment_receipt_email,
            user.email, user.full_name, ent.PLANS[pay.plan]["name"], pay.amount, pay.currency,
            sub.expires_at.strftime("%d %b %Y"), pay.tx_ref,
        )


def _confirm(db: Session, transaction_id: str, tx_ref: str, background_tasks, user_id: str | None):
    pay = db.query(models.Payment).filter(models.Payment.tx_ref == tx_ref).first()
    if not pay or (user_id is not None and pay.user_id != user_id):
        raise HTTPException(status_code=404, detail="We couldn't find that payment.")
    if pay.status == "paid":
        return pay

    txn = (_flw("GET", f"/transactions/{urllib.parse.quote(str(transaction_id), safe='')}/verify").get("data")) or {}
    ok = (
        str(txn.get("status")).lower() == "successful"
        and txn.get("tx_ref") == pay.tx_ref
        and str(txn.get("currency")).upper() == pay.currency
        and float(txn.get("amount") or 0) >= float(pay.amount)
    )
    if not ok:
        if str(txn.get("status")).lower() in ("failed", "cancelled"):
            pay.status = "failed"
            db.commit()
        raise HTTPException(status_code=402, detail="We couldn't confirm that payment yet. If you were charged, it will appear within a few minutes.")
    _apply_payment(db, pay, txn, background_tasks)
    return pay


@router.post("/verify", dependencies=[Depends(ratelimit.billing_limit)])
def verify(payload: VerifyIn, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _confirm(db, payload.transaction_id, payload.tx_ref, background_tasks, user.id)
    return {**ent.snapshot(db, user.id), "plans": _plans_out(), "currency": CURRENCY}


@router.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db),
                  verif_hash: str | None = Header(default=None)):
    """Flutterwave calls this itself, so the plan is still given if the student closes the page early."""
    expected = (os.getenv("FLW_SECRET_HASH") or "").strip()
    if not expected or not verif_hash or not hmac.compare_digest(expected, verif_hash):
        raise HTTPException(status_code=401, detail="Not allowed.")
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}
    data = body.get("data") or {}
    txn_id, tx_ref = data.get("id"), data.get("tx_ref")
    if txn_id and tx_ref and str(data.get("status")).lower() == "successful":
        try:
            _confirm(db, str(txn_id), tx_ref, background_tasks, None)
        except HTTPException:
            pass   # unknown reference etc.: nothing to do, but always answer 200
    return {"ok": True}
