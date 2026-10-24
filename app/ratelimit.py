"""
app/ratelimit.py - per-user and per-IP request limits, so one account can't run up
your Gemini bill and password endpoints can't be hammered.

A sliding window is kept in memory per (bucket, key). Rules are (max_requests,
window_seconds); a request must satisfy every rule. Exceeding one returns HTTP 429
with a friendly message and a Retry-After header, which the app already shows.

Note: counters live in this process's memory. That's exactly right for a single
server process. If you later run several workers, each keeps its own counters (so a
user could get up to N times the limit) - move the counters to Redis then.
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from app.deps import get_current_user

Rule = tuple[int, int]  # (max requests, window in seconds)

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_lock = threading.Lock()
_calls = 0
_MAX_WINDOW = 24 * 3600


def _humanize(seconds: float) -> str:
    s = max(1, int(seconds))
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''}"
    m = (s + 59) // 60
    if m < 60:
        return f"{m} minute{'s' if m != 1 else ''}"
    h, rem = divmod(m, 60)
    if rem:
        return f"{h}h {rem}m"
    return f"{h} hour{'s' if h != 1 else ''}"


def _sweep(now: float) -> None:
    """Forget keys that have been idle for a full day (keeps memory bounded)."""
    stale = [k for k, dq in _hits.items() if not dq or now - dq[-1] > _MAX_WINDOW]
    for k in stale:
        del _hits[k]


def enforce(key: str, bucket: str, rules: list[Rule], label: str = "this feature") -> None:
    """Record one request for `key`, or raise 429 if any rule is already used up."""
    global _calls
    now = time.monotonic()
    longest = max(window for _, window in rules)
    wait = None
    window_hit = 0

    with _lock:
        _calls += 1
        if _calls % 500 == 0:
            _sweep(now)
        dq = _hits[(bucket, key)]
        while dq and now - dq[0] > longest:
            dq.popleft()
        for limit, window in rules:
            in_window = [t for t in dq if now - t <= window]
            if len(in_window) >= limit:
                wait = window - (now - in_window[0]) + 1
                window_hit = window
                break
        if wait is None:
            dq.append(now)
            return

    if window_hit <= 600:
        detail = f"You're going a bit fast with {label}. Please wait {_humanize(wait)} and try again."
    else:
        detail = f"You've reached the limit for {label}. Please try again in {_humanize(wait)}."
    raise HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(int(wait))})


def client_ip(request: Request) -> str:
    """Best-effort client IP. Behind one proxy the right-most X-Forwarded-For entry is the real client."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def user_limit(bucket: str, *rules: Rule, label: str = "this feature"):
    """Dependency: limit each logged-in user."""
    def dependency(current_user=Depends(get_current_user)):
        enforce(str(current_user.id), bucket, list(rules), label)
    return dependency


def ip_limit(bucket: str, *rules: Rule, label: str = "this feature"):
    """Dependency: limit each client IP (for endpoints with no logged-in user)."""
    def dependency(request: Request):
        enforce(client_ip(request), bucket, list(rules), label)
    return dependency


# ---- the limits (edit the numbers to taste) ----
# Tutor: also used by every turn of a live voice call.
tutor_limit = user_limit("tutor", (15, 60), (150, 24 * 3600), label="the AI Tutor")
quiz_generate_limit = user_limit("quiz_generate", (5, 600), (30, 24 * 3600), label="quiz generation")
narration_limit = user_limit("narration", (10, 60), (100, 24 * 3600), label="read aloud")
upload_limit = user_limit("upload", (10, 3600), (40, 24 * 3600), label="uploads")
signup_limit = ip_limit("signup", (20, 3600), label="sign-up")
forgot_password_limit = ip_limit("forgot_password", (5, 3600), label="password reset")
LOGIN_RULES: list[Rule] = [(8, 300)]  # per IP + email, checked inside the login route
