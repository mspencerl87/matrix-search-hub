import threading
import time

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import config

SESSION_COOKIE = "matrix_search_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

PENDING_LOGIN_TTL = 600  # 10 minutes, generous enough to complete an SSO prompt

_serializer = URLSafeTimedSerializer(config.SESSION_SECRET, salt="matrix-search-hub-session")

_pending_lock = threading.Lock()
_pending_logins: dict[str, dict] = {}


def create_session_cookie_value(user_id: str) -> str:
    return _serializer.dumps({"user_id": user_id})


def read_session_user_id(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("user_id")


def require_user(request: Request) -> str:
    user_id = read_session_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user_id


def store_pending_login(state: str, code_verifier: str, device_id: str) -> None:
    with _pending_lock:
        _prune_pending()
        _pending_logins[state] = {
            "code_verifier": code_verifier,
            "device_id": device_id,
            "created_at": time.time(),
        }


def pop_pending_login(state: str) -> dict | None:
    with _pending_lock:
        entry = _pending_logins.pop(state, None)
    if not entry or time.time() - entry["created_at"] > PENDING_LOGIN_TTL:
        return None
    return entry


def _prune_pending() -> None:
    cutoff = time.time() - PENDING_LOGIN_TTL
    stale = [k for k, v in _pending_logins.items() if v["created_at"] < cutoff]
    for k in stale:
        _pending_logins.pop(k, None)
