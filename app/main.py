import asyncio
import json
import logging
import os
import secrets
import time

import aiohttp
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import auth, config, control_store, oidc
from app.search_index import get_stats, search
from app.worker_manager import WorkerManager, user_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

app = FastAPI(title="matrix-search-hub")

app_state: dict = {}


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

    control_conn = control_store.init_db(config.CONTROL_DB_PATH)
    app_state["control_conn"] = control_conn

    manager = WorkerManager(control_conn, http_session, discovery, client_id, client_secret)
    app_state["manager"] = manager
    await manager.restart_known_users()


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
async def auth_callback(request: Request, code: str = None, state: str = None, error: str = None):
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

    control_store.upsert_user(
        app_state["control_conn"], user_id, device_id, token_resp["access_token"], token_resp.get("refresh_token"), expires_at
    )

    manager: WorkerManager = app_state["manager"]
    await manager.start_user(user_id, device_id, token_resp["access_token"])

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


@app.get("/api/search")
async def api_search(request: Request, q: str = Query(..., min_length=1), limit: int = 50):
    user_id = auth.require_user(request)
    manager: WorkerManager = app_state["manager"]
    conn = manager.get_search_conn(user_id)
    rows = search(conn, q, limit=limit)
    for row in rows:
        row["matrix_to_url"] = f"https://matrix.to/#/{row['room_id']}/{row['event_id']}"
        row["element_url"] = f"{config.ELEMENT_URL}/#/room/{row['room_id']}/{row['event_id']}"
    return {"query": q, "results": rows}


@app.get("/api/status")
async def api_status(request: Request):
    user_id = auth.require_user(request)
    manager: WorkerManager = app_state["manager"]
    conn = manager.get_search_conn(user_id)
    return get_stats(conn)


@app.post("/api/import-keys")
async def api_import_keys(request: Request, file: UploadFile = File(...), passphrase: str = Form(...)):
    user_id = auth.require_user(request)
    manager: WorkerManager = app_state["manager"]
    indexer = manager.indexers.get(user_id)
    if not indexer:
        return JSONResponse({"error": "your sync worker isn't ready yet, try again in a moment"}, status_code=503)

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
