"""
app/email_service.py - sends transactional email.

Two ways to send, picked automatically from which environment variables exist:

  A) Gmail / any SMTP server (no domain needed - good to start with)
       SMTP_USER        the Gmail address that sends the emails
       SMTP_PASSWORD    a Gmail "App password" (NOT your normal password)
       SMTP_HOST        optional, default smtp.gmail.com
       SMTP_PORT        optional, default 587 (use 465 for SSL)
       EMAIL_FROM       optional, default "IMOWE <SMTP_USER>"

  B) Resend (needs a domain you own, verified in Resend - better deliverability)
       RESEND_API_KEY   your Resend API key
       EMAIL_FROM       e.g. "IMOWE <no-reply@yourdomain.com>"

  Both need:
       FRONTEND_URL     where index.html is hosted, e.g. https://yourname.github.io/Imowe/

If both A and B are configured, Resend is used. Only the standard library is
needed - nothing new to install.
"""
import html
import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

try:  # optional fallback to your pydantic Settings object
    from app.config import settings
except Exception:  # pragma: no cover
    settings = None

logger = logging.getLogger("imowe.email")

RESEND_API_URL = os.getenv("RESEND_API_URL", "https://api.resend.com/emails")


def _cfg(name: str) -> str | None:
    return os.getenv(name) or (getattr(settings, name, None) if settings else None) or None


def build_reset_link(token: str) -> str:
    base = (_cfg("FRONTEND_URL") or "").rstrip("/")
    return f"{base}/?reset_token={token}"


def _send_via_resend(api_key: str, sender: str, to: str, subject: str, html_body: str, text_body: str) -> bool:
    payload = json.dumps({
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }).encode("utf-8")
    req = urllib.request.Request(
        RESEND_API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "imowe-api/1.0",  # Resend's edge rejects the default Python UA
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        logger.error("Resend rejected the email (%s): %s", e.code, e.read().decode("utf-8", "ignore")[:300])
    except Exception:
        logger.exception("Sending email via Resend failed")
    return False


def _send_via_smtp(user: str, password: str, sender: str, to: str, subject: str, html_body: str, text_body: str) -> bool:
    host = _cfg("SMTP_HOST") or "smtp.gmail.com"
    port = int(_cfg("SMTP_PORT") or 587)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if (_cfg("SMTP_SECURITY") or "starttls").lower() != "none":
                server.starttls()
        with server:
            server.login(user, password)
            server.send_message(msg)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP login failed. For Gmail you must use an App Password (needs 2-Step Verification), not your normal password.")
    except Exception:
        logger.exception("Sending email via SMTP failed")
    return False


def send_email(to: str, subject: str, html_body: str, text_body: str) -> bool:
    """Returns True if the email was accepted for delivery. Never raises."""
    resend_key = _cfg("RESEND_API_KEY")
    sender = _cfg("EMAIL_FROM")
    if resend_key and sender:
        return _send_via_resend(resend_key, sender, to, subject, html_body, text_body)

    smtp_user = _cfg("SMTP_USER")
    smtp_password = _cfg("SMTP_PASSWORD")
    if smtp_user and smtp_password:
        return _send_via_smtp(smtp_user, smtp_password, sender or f"IMOWE <{smtp_user}>", to, subject, html_body, text_body)

    logger.warning("Email not sent: configure SMTP_USER + SMTP_PASSWORD (Gmail) or RESEND_API_KEY + EMAIL_FROM.")
    return False


def send_password_reset_email(to: str, full_name: str, link: str) -> bool:
    first = html.escape((full_name or "there").split()[0])
    safe_link = html.escape(link, quote=True)
    html_body = f"""
<div style="background:#101828;padding:32px 16px;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:460px;margin:0 auto;background:#16203A;border-radius:20px;padding:32px;color:#F8F7F2;">
    <div style="font-size:22px;font-weight:800;letter-spacing:.5px;">IMOWE</div>
    <h2 style="margin:22px 0 8px;font-size:20px;">Reset your password</h2>
    <p style="color:#AEB6C9;line-height:1.55;margin:0 0 22px;">
      Hi {first}, we received a request to reset your IMOWE password.
      This link works for 30 minutes and can only be used once.
    </p>
    <a href="{safe_link}" style="display:inline-block;background:#B8F34A;color:#101828;text-decoration:none;
       font-weight:700;padding:13px 26px;border-radius:999px;">Choose a new password</a>
    <p style="color:#78829B;font-size:12.5px;line-height:1.5;margin:24px 0 0;">
      If you didn't ask for this, you can safely ignore this email. Your password won't change.
    </p>
  </div>
</div>"""
    text_body = (
        f"Hi {first},\n\nWe received a request to reset your IMOWE password. "
        f"Use this link within 30 minutes (it works once):\n\n{link}\n\n"
        "If you didn't ask for this, ignore this email and your password stays the same.\n"
    )
    return send_email(to, "Reset your IMOWE password", html_body, text_body)
