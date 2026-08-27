import asyncio
import logging
import os
import time

from nio import (
    AsyncClient,
    AsyncClientConfig,
    MatrixRoom,
    MegolmEvent,
    MessageDirection,
    RoomMessagesError,
    RoomMessageText,
    SyncError,
    SyncResponse,
)

from app.search_index import add_message, prune_older_than

log = logging.getLogger("matrix_client")


class UserIndexer:
    """Owns one user's Matrix sync/backfill/search-index lifecycle.

    Unlike a password/SSO-login flow, the access token here was already
    obtained by the web app's own OAuth exchange - this client is handed a
    token+device_id it should just start using, never calling nio's login().
    """

    def __init__(self, user_id: str, device_id: str, access_token: str, store_path: str, conn, cfg):
        self.user_id = user_id
        self.device_id = device_id
        self.conn = conn
        self.cfg = cfg
        self._prev_batches: dict[str, str] = {}
        self._undecryptable = 0
        self._stop = False
        self.last_sync_attempt_at: float | None = None
        self.last_sync_success_at: float | None = None
        self.last_error: str | None = None

        os.makedirs(store_path, exist_ok=True)
        client_config = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True)
        self.client = AsyncClient(
            cfg.HOMESERVER, user_id, device_id=device_id, store_path=store_path, config=client_config
        )
        self.client.access_token = access_token
        self.client.user_id = user_id
        self.client.device_id = device_id
        self.client.load_store()

        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_undecryptable, MegolmEvent)
        self.client.add_response_callback(self._on_sync, SyncResponse)

    def update_access_token(self, access_token: str) -> None:
        self.client.access_token = access_token

    def _cutoff_ts_ms(self) -> int | None:
        if not self.cfg.RETENTION_MONTHS:
            return None
        return int((time.time() - self.cfg.RETENTION_MONTHS * 30 * 86400) * 1000)

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText):
        cutoff = self._cutoff_ts_ms()
        if cutoff and event.server_timestamp < cutoff:
            log.info(
                "[%s] SKIP (older than retention cutoff) event %s in room %s (%s)",
                self.user_id, event.event_id, room.room_id, room.display_name,
            )
            return
        log.info(
            "[%s] indexing event %s in room %s (%s): %r",
            self.user_id, event.event_id, room.room_id, room.display_name, event.body[:60],
        )
        add_message(
            self.conn, event.event_id, room.room_id, room.display_name, event.sender, event.body, event.server_timestamp
        )

    async def _on_undecryptable(self, room: MatrixRoom, event: MegolmEvent):
        log.info(
            "[%s] UNDECRYPTABLE event %s in room %s (%s)",
            self.user_id, event.event_id, room.room_id, room.display_name,
        )
        self._undecryptable += 1

    async def _on_sync(self, response: SyncResponse):
        for room_id, room_info in response.rooms.join.items():
            if room_info.timeline.prev_batch:
                self._prev_batches[room_id] = room_info.timeline.prev_batch
            else:
                log.warning("[%s] room %s has no prev_batch in this sync response", self.user_id, room_id)
            log.info(
                "[%s] sync timeline for %s: %d event(s), limited=%s",
                self.user_id, room_id, len(room_info.timeline.events), room_info.timeline.limited,
            )

    async def run(self):
        asyncio.create_task(self._prune_loop())
        await self.resync_history()
        while not self._stop:
            try:
                await self.client.sync_forever(timeout=30000, full_state=False)
            except Exception as e:
                # sync_forever loops internally and only returns/raises on a
                # real failure or cancellation - a healthy long-running sync
                # never reaches this line at all, so this only ever reflects
                # a genuine crash, not routine idle polling.
                self.last_error = f"sync_forever crashed: {e}"
                log.exception("[%s] sync_forever crashed, retrying in 10s", self.user_id)
                await asyncio.sleep(10)

    def health(self) -> dict:
        return {
            "last_sync_attempt_at": self.last_sync_attempt_at,
            "last_sync_success_at": self.last_sync_success_at,
            "last_error": self.last_error,
            "undecryptable": self._undecryptable,
        }

    async def _prune_loop(self):
        while not self._stop:
            cutoff = self._cutoff_ts_ms()
            if cutoff:
                try:
                    removed = prune_older_than(self.conn, cutoff)
                    if removed:
                        log.info("[%s] pruned %d messages older than retention window", self.user_id, removed)
                except Exception:
                    log.exception("[%s] prune failed", self.user_id)
            await asyncio.sleep(self.cfg.PRUNE_INTERVAL_SECONDS)

    async def resync_history(self):
        """Full sync + backfill of every joined room. Safe to call repeatedly
        (e.g. after a key import) since inserts are idempotent on event_id."""
        log.info("[%s] performing full sync...", self.user_id)
        self.last_sync_attempt_at = time.time()
        resp = await self.client.sync(timeout=30000, full_state=True)
        if isinstance(resp, SyncError):
            self.last_error = f"full sync failed (status={getattr(resp, 'status_code', '?')}): {resp}"
            log.error("[%s] %s - skipping this resync attempt", self.user_id, self.last_error)
            return
        self.last_sync_success_at = time.time()
        self.last_error = None
        log.info("[%s] sync complete, %d rooms joined", self.user_id, len(self.client.rooms))

        # Iterate every currently-known joined room (from the client's local
        # state, always populated) rather than _prev_batches - that dict only
        # gets filled in when nio's sync() actually processes a response,
        # which it silently skips if the sync token hasn't moved since the
        # last call (see _backfill_room's fallback for the same reason).
        for room_id in list(self.client.rooms.keys()):
            try:
                await self._backfill_room(room_id)
            except Exception:
                log.exception("[%s] backfill failed for room %s", self.user_id, room_id)
        self.conn.commit()
        log.info(
            "[%s] backfill complete (%d events could not be decrypted with current keys)",
            self.user_id,
            self._undecryptable,
        )

    async def _backfill_room(self, room_id: str):
        room = self.client.rooms.get(room_id)
        room_name = room.display_name if room else room_id
        # _prev_batches is only populated when nio's sync() actually processes
        # a response - if the sync token hasn't moved since last time (e.g. no
        # new server-side activity between two resync_history() calls), nio
        # silently no-ops the whole thing and _on_sync never fires. Fall back
        # to the client's current position so backfill still runs.
        token = self._prev_batches.get(room_id) or self.client.next_batch
        pages = 0
        cutoff = self._cutoff_ts_ms()

        while token and pages < self.cfg.MAX_BACKFILL_PAGES_PER_ROOM:
            resp = await self.client.room_messages(room_id, start=token, direction=MessageDirection.back, limit=200)
            if isinstance(resp, RoomMessagesError):
                log.warning("[%s] room_messages error for %s: %s", self.user_id, room_id, resp)
                break
            if not resp.chunk:
                break

            reached_cutoff = False
            for event in resp.chunk:
                if cutoff and event.server_timestamp < cutoff:
                    reached_cutoff = True
                    break
                if isinstance(event, RoomMessageText):
                    add_message(
                        self.conn,
                        event.event_id,
                        room_id,
                        room_name,
                        event.sender,
                        event.body,
                        event.server_timestamp,
                        commit=False,
                    )
                elif isinstance(event, MegolmEvent):
                    self._undecryptable += 1

            self.conn.commit()
            pages += 1
            if reached_cutoff:
                break
            if resp.end == token:
                break
            token = resp.end

    async def import_keys(self, path: str, passphrase: str):
        await self.client.import_keys(path, passphrase)

    async def close(self):
        self._stop = True
        await self.client.close()
