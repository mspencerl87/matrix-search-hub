import asyncio
import logging
import os
import shutil
import time

from app import config, oidc, vault
from app.matrix_client import UserIndexer
from app.paths import user_dir

log = logging.getLogger("worker_manager")


class WorkerManager:
    """Tracks running per-user sync workers and their (unlocked) vault
    connections. Nothing here is auto-started on process boot - a user's
    vault can only be opened with their passphrase, so every restart leaves
    everyone locked until they visit the app and unlock again."""

    def __init__(self, http_session, discovery, client_id, client_secret):
        self.http_session = http_session
        self.discovery = discovery
        self.client_id = client_id
        self.client_secret = client_secret
        self.indexers: dict[str, UserIndexer] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.vault_conns: dict[str, object] = {}

    def is_unlocked(self, user_id: str) -> bool:
        task = self.tasks.get(user_id)
        return bool(task and not task.done())

    async def start_user(self, user_id: str, device_id: str, vault_conn, access_token: str):
        if self.is_unlocked(user_id):
            return
        self.vault_conns[user_id] = vault_conn
        store_path = os.path.join(user_dir(user_id), "nio_store")
        indexer = UserIndexer(user_id, device_id, access_token, store_path, vault_conn, config)
        self.indexers[user_id] = indexer
        self.tasks[user_id] = asyncio.create_task(self._run_worker(user_id, indexer))
        log.info("Unlocked and started sync worker for %s", user_id)

    async def lock_user(self, user_id: str):
        task = self.tasks.pop(user_id, None)
        indexer = self.indexers.pop(user_id, None)
        conn = self.vault_conns.pop(user_id, None)
        if indexer:
            await indexer.close()
        if task:
            task.cancel()
        if conn:
            conn.close()
        log.info("Locked vault for %s", user_id)

    async def deprovision(self, user_id: str):
        """Irreversibly wipes a user's vault and Matrix crypto store. Locks
        them out first so nothing is writing to the files while removing
        them. Used for offboarding - there's no undo."""
        await self.lock_user(user_id)
        d = user_dir(user_id)
        if os.path.isdir(d):
            shutil.rmtree(d)
        log.info("Deprovisioned %s (deleted %s)", user_id, d)

    async def _run_worker(self, user_id: str, indexer: UserIndexer):
        refresh_task = asyncio.create_task(self._refresh_loop(user_id, indexer))
        try:
            await indexer.run()
        finally:
            refresh_task.cancel()

    async def _refresh_loop(self, user_id: str, indexer: UserIndexer):
        while True:
            conn = self.vault_conns.get(user_id)
            if conn is None:
                return
            record = vault.get_oauth(conn)
            if not record or not record.get("refresh_token"):
                return
            sleep_for = max((record.get("expires_at") or 0) - time.time() - config.TOKEN_REFRESH_MARGIN_SECONDS, 5)
            await asyncio.sleep(sleep_for)

            conn = self.vault_conns.get(user_id)
            if conn is None:
                return
            record = vault.get_oauth(conn)
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
            vault.set_oauth(conn, record["device_id"], new_access, new_refresh, new_expires_at)
            indexer.update_access_token(new_access)
            log.info("Refreshed access token for %s", user_id)
