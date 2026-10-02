import time

import pytest
from fastapi.testclient import TestClient

from app import auth, main, oidc, vault

USER = "@dan:example.test"
PASSPHRASE = "correct horse battery staple"


class FakeManager:
    def __init__(self):
        self.started = []
        self.vault_conn = None

    async def start_user(self, user_id, device_id, vault_conn, access_token):
        self.started.append((user_id, device_id, access_token))
        self.vault_conn = vault_conn


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "user_dir", lambda _user_id: str(tmp_path / "dan"))
    manager = FakeManager()
    monkeypatch.setattr(
        main,
        "app_state",
        {"manager": manager, "http_session": object(), "discovery": {}, "client_id": "c", "client_secret": None},
    )
    auth._pending_tokens.clear()

    client = TestClient(main.app)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session_cookie_value(USER))
    return client, manager


def make_vault(expires_at, refresh_token="stored-refresh"):
    conn = vault.open_vault(USER, PASSPHRASE)
    vault.set_oauth(conn, "OLDDEVICE", "stored-access", refresh_token, expires_at)
    conn.close()


def unlock(client, passphrase=PASSPHRASE):
    return client.post("/api/vault/unlock", json={"passphrase": passphrase})


def refresh_fails(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise oidc.OIDCError("Token refresh failed (400): invalid_grant")

    monkeypatch.setattr(oidc, "refresh_token", fail)


def test_unexpired_session_is_used_as_is(env, monkeypatch):
    client, manager = env
    make_vault(expires_at=time.time() + 3600)
    refresh_fails(monkeypatch)

    assert unlock(client).status_code == 200
    assert manager.started == [(USER, "OLDDEVICE", "stored-access")]


def test_expired_session_is_refreshed_and_saved(env, monkeypatch):
    client, manager = env
    make_vault(expires_at=1.0)

    async def refresh(*_args, **_kwargs):
        return {"access_token": "renewed-access", "refresh_token": "renewed-refresh", "expires_in": 300}

    monkeypatch.setattr(oidc, "refresh_token", refresh)

    assert unlock(client).status_code == 200
    assert manager.started == [(USER, "OLDDEVICE", "renewed-access")]
    stored = vault.get_oauth(manager.vault_conn)
    assert (stored["access_token"], stored["refresh_token"]) == ("renewed-access", "renewed-refresh")


@pytest.mark.parametrize("refresh_token", ["revoked-refresh", None])
def test_dead_session_asks_user_to_sign_in_again(env, monkeypatch, refresh_token):
    client, manager = env
    make_vault(expires_at=1.0, refresh_token=refresh_token)
    refresh_fails(monkeypatch)

    response = unlock(client)

    assert response.status_code == 401
    assert response.json()["needs_reauth"] is True
    assert manager.started == []


def test_dead_session_adopts_a_fresh_sign_in_and_keeps_the_vault(env, monkeypatch):
    client, manager = env
    make_vault(expires_at=1.0)
    refresh_fails(monkeypatch)
    auth.store_pending_tokens(USER, "NEWDEVICE", "fresh-access", "fresh-refresh", time.time() + 300)

    assert unlock(client).status_code == 200

    assert manager.started == [(USER, "NEWDEVICE", "fresh-access")]
    stored = vault.get_oauth(manager.vault_conn)
    assert (stored["device_id"], stored["refresh_token"]) == ("NEWDEVICE", "fresh-refresh")
    assert auth.peek_pending_tokens(USER) is None


def test_wrong_passphrase_is_rejected_before_any_session_handling(env, monkeypatch):
    client, manager = env
    make_vault(expires_at=1.0)
    auth.store_pending_tokens(USER, "NEWDEVICE", "fresh-access", "fresh-refresh", time.time() + 300)

    response = unlock(client, passphrase="not the passphrase")

    assert response.status_code == 401
    assert manager.started == []
    assert auth.peek_pending_tokens(USER) is not None
