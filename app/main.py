import asyncio
import json
import logging
import os
import secrets
import time

import aiohttp
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import auth, config, control_store, oidc, vault
from app.search_index import get_stats, search
from app.worker_manager import WorkerManager, user_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

app = FastAPI(title="matrix-search-hub")

app_state: dict = {}


class PassphraseBody(BaseModel):
    passphrase: str


@app.on_event("startup")
async def startup():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.USERS_DIR, exist_ok=True)

    http_session = aiohttp.ClientSession()
    app_state["http_session"] = http_session

    discovery = await oidc.discover(http_session, config.HOMESERVER)
    app_state["discovery"] = discovery
    log.info("Discovered OIDC issuer: %s", discovery.get("issuer"))

    client_id, client_secret = await _load_or_register_client(http_session, discovery)
    app_state["client_id"] = client_id
    app_state["client_secret"] = client_secret

    app_state["control_conn"] = control_store.init_db(config.CONTROL_DB_PATH)
    app_state["manager"] = WorkerManager(http_session, discovery, client_id, client_secret)

    log.info(
        "Startup complete. No user data is decrypted until each user unlocks their "
        "own vault - nothing auto-resumes after a restart by design."
    )


@app.on_event("shutdown")
async def shutdown():
    session = app_state.get("http_session")
    if session:
        await session.close()


async def _load_or_register_client(http_session, discovery):
    if os.path.exists(config.OAUTH_CLIENT_FILE):
        with open(config.OAUTH_CLIENT_FILE) as f:
            data = json.load(f)
        log.info("Reusing previously registered OAuth client %s", data["client_id"])
        return data["client_id"], data.get("client_secret")

    if config.STATIC_CLIENT_ID:
        log.info("Using statically configured OAuth client %s", config.STATIC_CLIENT_ID)
        return config.STATIC_CLIENT_ID, config.STATIC_CLIENT_SECRET

    redirect_uri = f"{config.BASE_URL}/auth/callback"
    reg = await oidc.register_client(http_session, discovery, redirect_uri, "matrix-search-hub")
    client_id, client_secret = reg["client_id"], reg.get("client_secret")
    with open(config.OAUTH_CLIENT_FILE, "w") as f:
        json.dump({"client_id": client_id, "client_secret": client_secret}, f)
    log.info("Dynamically registered new OAuth client %s", client_id)
    return client_id, client_secret


@app.get("/auth/login")
async def auth_login():
    verifier, challenge = oidc.new_pkce_pair()
    device_id = oidc.new_device_id()
    state_token = secrets.token_urlsafe(24)
    auth.store_pending_login(state_token, verifier, device_id)

    redirect_uri = f"{config.BASE_URL}/auth/callback"
    url = oidc.build_authorize_url(
        app_state["discovery"], app_state["client_id"], redirect_uri, state_token, challenge, device_id
    )
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return JSONResponse({"error": error}, status_code=400)
    if not code or not state:
        return JSONResponse({"error": "missing code/state in callback"}, status_code=400)

    pending = auth.pop_pending_login(state)
    if not pending:
        return JSONResponse({"error": "login expired or already used, please try signing in again"}, status_code=400)

    http_session = app_state["http_session"]
    discovery = app_state["discovery"]
    redirect_uri = f"{config.BASE_URL}/auth/callback"

    try:
        token_resp = await oidc.exchange_code(
            http_session,
            discovery,
            app_state["client_id"],
            app_state["client_secret"],
            redirect_uri,
            code,
            pending["code_verifier"],
        )
        who = await oidc.whoami(http_session, config.HOMESERVER, token_resp["access_token"])
    except oidc.OIDCError:
        log.exception("OIDC login failed")
        return JSONResponse({"error": "login failed, see server logs"}, status_code=400)

    user_id = who["user_id"]
    device_id = who.get("device_id") or pending["device_id"]
    expires_at = time.time() + token_resp.get("expires_in", 300)

    # Tokens are held in memory only until the user supplies their vault
    # passphrase (setup or unlock) - never written to control.db.
    auth.store_pending_tokens(user_id, device_id, token_resp["access_token"], token_resp.get("refresh_token"), expires_at)
    control_store.upsert_user(app_state["control_conn"], user_id, device_id)

    resp = RedirectResponse("/")
    resp.set_cookie(
        auth.SESSION_COOKIE,
        auth.create_session_cookie_value(user_id),
        httponly=True,
        samesite="lax",
        max_age=auth.SESSION_MAX_AGE,
    )
    return resp


@app.get("/auth/logout")
async def auth_logout():
    resp = RedirectResponse("/")
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    user_id = auth.read_session_user_id(request)
    if not user_id:
        return JSONResponse({"logged_in": False}, status_code=401)
    return {"logged_in": True, "user_id": user_id}


@app.get("/api/vault-status")
async def api_vault_status(request: Request):
    user_id = auth.require_user(request)
    manager: WorkerManager = app_state["manager"]
    return {
        "exists": vault.exists(user_id),
        "unlocked": manager.is_unlocked(user_id),
        "has_pending_login": auth.peek_pending_tokens(user_id) is not None,
    }


@app.post("/api/vault/setup")
async def api_vault_setup(request: Request, body: PassphraseBody):
    user_id = auth.require_user(request)
    if vault.exists(user_id):
        return JSONResponse({"error": "a vault already exists for this account, unlock it instead"}, status_code=409)
    if len(body.passphrase) < config.MIN_VAULT_PASSPHRASE_LENGTH:
        return JSONResponse(
            {"error": f"passphrase must be at least {config.MIN_VAULT_PASSPHRASE_LENGTH} characters"}, status_code=400
        )

    pending = auth.pop_pending_tokens(user_id)
    if not pending:
        return JSONResponse({"error": "your login has expired, please sign in again"}, status_code=400)

    conn = vault.open_vault(user_id, body.passphrase)
    vault.set_oauth(conn, pending["device_id"], pending["access_token"], pending["refresh_token"], pending["expires_at"])

    manager: WorkerManager = app_state["manager"]
    await manager.start_user(user_id, pending["device_id"], conn, pending["access_token"])
    return {"status": "unlocked"}


@app.post("/api/vault/unlock")
async def api_vault_unlock(request: Request, body: PassphraseBody):
    user_id = auth.require_user(request)
    if not vault.exists(user_id):
        return JSONResponse({"error": "no vault exists yet for this account, set one up instead"}, status_code=409)

    try:
        conn = vault.open_vault(user_id, body.passphrase)
    except vault.WrongPassphrase:
        return JSONResponse({"error": "incorrect passphrase"}, status_code=401)

    record = vault.get_oauth(conn)
    if not record:
        conn.close()
        return JSONResponse({"error": "vault has no stored session, contact an admin"}, status_code=500)

    access_token = record["access_token"]
    if (record.get("expires_at") or 0) - time.time() < config.TOKEN_REFRESH_MARGIN_SECONDS:
        if not record.get("refresh_token"):
            conn.close()
            return JSONResponse({"error": "your Matrix session has expired, please sign in again"}, status_code=401)
        try:
            body_resp = await oidc.refresh_token(
                app_state["http_session"], app_state["discovery"], app_state["client_id"], app_state["client_secret"],
                record["refresh_token"],
            )
        except oidc.OIDCError:
            conn.close()
            return JSONResponse({"error": "your Matrix session could not be renewed, please sign in again"}, status_code=401)
        access_token = body_resp["access_token"]
        new_refresh = body_resp.get("refresh_token", record["refresh_token"])
        new_expires_at = time.time() + body_resp.get("expires_in", 300)
        vault.set_oauth(conn, record["device_id"], access_token, new_refresh, new_expires_at)

    manager: WorkerManager = app_state["manager"]
    await manager.start_user(user_id, record["device_id"], conn, access_token)
    return {"status": "unlocked"}


@app.post("/api/vault/lock")
async def api_vault_lock(request: Request):
    user_id = auth.require_user(request)
    manager: WorkerManager = app_state["manager"]
    await manager.lock_user(user_id)
    return {"status": "locked"}


@app.get("/api/config")
async def api_config():
    allowed = [m for m in config.SEARCH_RANGE_OPTIONS_MONTHS if m <= config.RETENTION_MONTHS]
    if not allowed:
        allowed = [config.RETENTION_MONTHS]
    default = config.DEFAULT_SEARCH_RANGE_MONTHS if config.DEFAULT_SEARCH_RANGE_MONTHS in allowed else allowed[0]
    return {"range_options_months": allowed, "default_months": default, "retention_months": config.RETENTION_MONTHS}


def _require_unlocked(request: Request):
    user_id = auth.require_user(request)
    manager: WorkerManager = app_state["manager"]
    if not manager.is_unlocked(user_id):
        raise _locked_error()
    return user_id, manager


def _locked_error():
    return HTTPException(status_code=423, detail="vault is locked, unlock it first")


@app.get("/api/search")
async def api_search(request: Request, q: str = Query(..., min_length=1), limit: int = 50, months: int | None = None):
    user_id, manager = _require_unlocked(request)
    conn = manager.vault_conns[user_id]

    allowed = [m for m in config.SEARCH_RANGE_OPTIONS_MONTHS if m <= config.RETENTION_MONTHS] or [config.RETENTION_MONTHS]
    if months not in allowed:
        months = config.DEFAULT_SEARCH_RANGE_MONTHS if config.DEFAULT_SEARCH_RANGE_MONTHS in allowed else allowed[0]
    since_ts = int((time.time() - months * 30 * 86400) * 1000)

    rows = search(conn, q, limit=limit, since_ts=since_ts)
    for row in rows:
        row["matrix_to_url"] = f"https://matrix.to/#/{row['room_id']}/{row['event_id']}"
        row["element_url"] = f"{config.ELEMENT_URL}/#/room/{row['room_id']}/{row['event_id']}"
    return {"query": q, "months": months, "results": rows}


@app.get("/api/status")
async def api_status(request: Request):
    user_id, manager = _require_unlocked(request)
    conn = manager.vault_conns[user_id]
    return get_stats(conn)


@app.post("/api/import-keys")
async def api_import_keys(request: Request, file: UploadFile = File(...), passphrase: str = Form(...)):
    user_id, manager = _require_unlocked(request)
    indexer = manager.indexers[user_id]

    d = user_dir(user_id)
    os.makedirs(d, exist_ok=True)
    keys_path = os.path.join(d, "imported-keys.txt")
    try:
        with open(keys_path, "wb") as f:
            f.write(await file.read())
        await indexer.import_keys(keys_path, passphrase)
    finally:
        if os.path.exists(keys_path):
            os.remove(keys_path)

    asyncio.create_task(indexer.resync_history())
    return {"status": "importing", "detail": "Re-scanning your history in the background - check back shortly."}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
