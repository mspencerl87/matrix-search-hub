import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth, config, control_store


def request_with_cookie(value=None):
    headers = []
    if value is not None:
        headers.append((b"cookie", f"{auth.SESSION_COOKIE}={value}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_session_cookie_round_trip_and_rejects_invalid_value():
    token = auth.create_session_cookie_value("@alice:example.test")

    assert auth.read_session_user_id(request_with_cookie(token)) == "@alice:example.test"
    assert auth.read_session_user_id(request_with_cookie()) is None
    assert auth.read_session_user_id(request_with_cookie("not-a-token")) is None


def test_require_user_rejects_anonymous_request():
    with pytest.raises(HTTPException) as exc:
        auth.require_user(request_with_cookie())

    assert exc.value.status_code == 401


def test_admin_checks_static_and_dynamic_admins(monkeypatch):
    conn = control_store.init_db(":memory:")
    control_store.add_admin(conn, "@dynamic:example.test", "@owner:example.test")
    monkeypatch.setattr(config, "ADMIN_USER_IDS", {"@static:example.test"})

    assert auth.is_admin("@static:example.test")
    assert auth.is_admin("@dynamic:example.test", conn)
    assert not auth.is_admin("@user:example.test", conn)

    request = request_with_cookie(auth.create_session_cookie_value("@user:example.test"))
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(request, conn)
    assert exc.value.status_code == 403


def test_pending_login_is_single_use_and_expires(monkeypatch):
    auth._pending_logins.clear()
    auth.store_pending_login("fresh", "verifier", "DEVICE")

    assert auth.pop_pending_login("fresh") == {
        "code_verifier": "verifier",
        "device_id": "DEVICE",
        "created_at": pytest.approx(time.time(), abs=1),
    }
    assert auth.pop_pending_login("fresh") is None

    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    auth._pending_logins["expired"] = {
        "code_verifier": "old",
        "device_id": "OLD",
        "created_at": now - auth.PENDING_LOGIN_TTL - 1,
    }
    assert auth.pop_pending_login("expired") is None


def test_pending_tokens_can_be_peeked_then_popped(monkeypatch):
    auth._pending_tokens.clear()
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    auth.store_pending_tokens("@alice:test", "DEVICE", "access", "refresh", 123.0)

    expected = {
        "device_id": "DEVICE",
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": 123.0,
        "created_at": now,
    }
    assert auth.peek_pending_tokens("@alice:test") == expected
    assert auth.pop_pending_tokens("@alice:test") == expected
    assert auth.peek_pending_tokens("@alice:test") is None


def test_expired_pending_tokens_are_rejected(monkeypatch):
    auth._pending_tokens.clear()
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    auth._pending_tokens["@alice:test"] = {
        "created_at": now - config.PENDING_TOKENS_TTL_SECONDS - 1,
    }

    assert auth.peek_pending_tokens("@alice:test") is None
    assert auth.pop_pending_tokens("@alice:test") is None
