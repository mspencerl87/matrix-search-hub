import asyncio
import logging
import os
import time

from app import config, control_store, oidc
from app.matrix_client import UserIndexer
from app.search_index import init_db as init_search_db

log = logging.getLogger("worker_manager")


def user_dir(user_id: str) -> str:
    safe = user_id.replace("@", "").replace(":", "_")
    return os.path.join(config.USERS_DIR, safe)


class WorkerManager:
    def __init__(self, control_conn, http_session, discovery, client_id, client_secret):
        self.control_conn = control_conn
        self.http_session = http_session
        self.discovery = discovery
        self.client_id = client_id
        self.client_secret = client_secret
        self.indexers: dict[str, UserIndexer] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._search_conns: dict[str, object] = {}

    def get_search_conn(self, user_id: str):
        conn = self._search_conns.get(user_id)
        if conn is None:
            d = user_dir(user_id)
            os.makedirs(d, exist_ok=True)
            conn = init_search_db(os.path.join(d, "search.db"))
            self._search_conns[user_id] = conn
        return conn

    async def start_user(self, user_id: str, device_id: str, access_token: str):
        existing = self.tasks.get(user_id)
        if existing and not existing.done():
            return
        d = user_dir(user_id)
        store_path = os.path.join(d, "nio_store")
        conn = self.get_search_conn(user_id)
        indexer = UserIndexer(user_id, device_id, access_token, store_path, conn, config)
        self.indexers[user_id] = indexer
        self.tasks[user_id] = asyncio.create_task(self._run_worker(user_id, indexer))
        log.info("Started sync worker for %s", user_id)

    async def _run_worker(self, user_id: str, indexer: UserIndexer):
        refresh_task = asyncio.create_task(self._refresh_loop(user_id, indexer))
        try:
            await indexer.run()
        finally:
            refresh_task.cancel()

    async def _refresh_loop(self, user_id: str, indexer: UserIndexer):
        while True:
            record = control_store.get_user(self.control_conn, user_id)
            if not record or not record.get("refresh_token"):
                return
            sleep_for = max((record.get("expires_at") or 0) - time.time() - config.TOKEN_REFRESH_MARGIN_SECONDS, 5)
            await asyncio.sleep(sleep_for)
            try:
                body = await oidc.refresh_token(
                    self.http_session, self.discovery, self.client_id, self.client_secret, record["refresh_token"]
                )
            except Exception:
                log.exception("Token refresh failed for %s, retrying in 60s", user_id)
                await asyncio.sleep(60)
                continue

            new_access = body["access_token"]
            new_refresh = body.get("refresh_token", record["refresh_token"])
            new_expires_at = time.time() + body.get("expires_in", 300)
            control_store.update_tokens(self.control_conn, user_id, new_access, new_refresh, new_expires_at)
            indexer.update_access_token(new_access)
            log.info("Refreshed access token for %s", user_id)

    async def restart_known_users(self):
        for record in control_store.all_users(self.control_conn):
            user_id = record["user_id"]
            access_token = record["access_token"]
            expires_at = record.get("expires_at") or 0

            if expires_at - time.time() < config.TOKEN_REFRESH_MARGIN_SECONDS:
                if not record.get("refresh_token"):
                    log.warning("Stored session for %s is expired with no refresh_token; they'll need to log in again", user_id)
                    continue
                try:
                    body = await oidc.refresh_token(
                        self.http_session, self.discovery, self.client_id, self.client_secret, record["refresh_token"]
                    )
                except Exception:
                    log.exception("Could not refresh token for %s at startup; they'll need to log in again", user_id)
                    continue
                access_token = body["access_token"]
                new_refresh = body.get("refresh_token", record["refresh_token"])
                new_expires_at = time.time() + body.get("expires_in", 300)
                control_store.update_tokens(self.control_conn, user_id, access_token, new_refresh, new_expires_at)

            await self.start_user(user_id, record["device_id"], access_token)
