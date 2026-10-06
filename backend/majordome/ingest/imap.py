"""IMAP ingester: one IDLE loop per mailbox, strictly read-only.

Read-only guarantees: folders are opened with EXAMINE (`readonly=True`), bodies are fetched with
BODY.PEEK (never sets \\Seen), and no STORE/COPY/MOVE/EXPUNGE command is ever issued.
History starts "from now": on first sight of a folder we record UIDNEXT and skip older mail.
"""

import asyncio
import contextlib
import json
import logging
import ssl
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from imapclient import IMAPClient

from majordome.config import Settings
from majordome.ingest import oauth
from majordome.ingest.email_parse import parse_email
from majordome.ingest.normalize import AccountRef, store_message
from majordome.store import db

log = logging.getLogger(__name__)

FETCH_ITEMS = ["BODY.PEEK[]", "INTERNALDATE"]
IDLE_SECONDS = 5 * 60
RECONNECT_SECONDS = 25 * 60  # servers drop long IDLE sessions (~29 min)


@dataclass
class EmailAccount:
    name: str
    address: str
    host: str
    port: int = 993
    auth: str = "password"  # password | oauth_microsoft
    password: str | None = None
    client_id: str | None = None  # for oauth_microsoft
    folders: list[str] = field(default_factory=lambda: ["INBOX"])

    @property
    def ref(self) -> AccountRef:
        return AccountRef("email", self.address.lower(), self.name)


def load_accounts(cfg: Settings) -> list[EmailAccount]:
    if not cfg.email_accounts_file.exists():
        log.info("no email accounts file at %s", cfg.email_accounts_file)
        return []
    return [EmailAccount(**a) for a in json.loads(cfg.email_accounts_file.read_text())]


class ImapIngester:
    def __init__(self, cfg: Settings, account: EmailAccount):
        self.cfg = cfg
        self.acc = account
        self.loop: asyncio.AbstractEventLoop | None = None

    def _await(self, coro_fn: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        assert self.loop
        return asyncio.run_coroutine_threadsafe(coro_fn(), self.loop).result()

    def _state_key(self, folder: str) -> str:
        return f"imap.{self.acc.address.lower()}.{folder}"

    def _connect(self) -> IMAPClient:
        client = IMAPClient(self.acc.host, port=self.acc.port, ssl=True,
                            ssl_context=ssl.create_default_context(), timeout=60)
        if self.acc.auth == "oauth_microsoft":
            token = oauth.microsoft_access_token(self.cfg, self.acc.address, self.acc.client_id)
            client.oauth2_login(self.acc.address, token)
        else:
            client.login(self.acc.address, self.acc.password)
        return client

    def _sync_folder(self, client: IMAPClient, folder: str) -> None:
        sel = client.select_folder(folder, readonly=True)  # EXAMINE
        uidvalidity = int(sel[b"UIDVALIDITY"])
        uidnext = int(sel.get(b"UIDNEXT", 1))
        key = self._state_key(folder)

        async def get():
            async with db.session() as s:
                return await db.get_state(s, key)

        st = self._await(get)
        if not st or st.get("uidvalidity") != uidvalidity:
            st = {"uidvalidity": uidvalidity, "last_uid": uidnext - 1}
            log.info("%s/%s: starting from UID %d", self.acc.address, folder, uidnext)
        last_uid = st["last_uid"]

        uids = [u for u in client.search(["UID", f"{last_uid + 1}:*"]) if u > last_uid]
        gmail = b"X-GM-EXT-1" in client.capabilities()
        items = FETCH_ITEMS + (["X-GM-THRID"] if gmail else [])
        for i in range(0, len(uids), 25):
            chunk = sorted(uids[i:i + 25])
            fetched = client.fetch(chunk, items)
            msgs = []
            for uid in chunk:
                data = fetched.get(uid)
                if not data or b"BODY[]" not in data:
                    continue
                internal = data.get(b"INTERNALDATE")
                if internal and internal.tzinfo is None:
                    internal = internal.replace(tzinfo=UTC)
                msgs.append(parse_email(
                    data[b"BODY[]"], self.acc.ref, folder=folder, uid=uid,
                    uidvalidity=uidvalidity, internal_date=internal,
                    gmail_thread_id=data.get(b"X-GM-THRID"),
                ))
            new_last = chunk[-1]

            async def save(msgs=msgs, new_last=new_last):
                async with db.session() as s:
                    for m in msgs:
                        await store_message(s, m)
                    await db.set_state(s, key, {**st, "last_uid": new_last,
                                                "at": datetime.now(UTC).isoformat()})
                    await s.commit()

            self._await(save)
            st["last_uid"] = new_last
            log.info("%s/%s: stored %d message(s)", self.acc.address, folder, len(msgs))

        if not uids:  # still record the checkpoint (first run, health timestamp)
            async def touch():
                async with db.session() as s:
                    await db.set_state(s, key, {**st, "at": datetime.now(UTC).isoformat()})
                    await s.commit()
            self._await(touch)

    def run_blocking(self) -> None:
        backoff = 5
        while True:
            client = None
            try:
                client = self._connect()
                started = time.monotonic()
                while time.monotonic() - started < RECONNECT_SECONDS:
                    for folder in self.acc.folders:
                        self._sync_folder(client, folder)
                    # IDLE on the first folder; other folders are polled each cycle.
                    client.select_folder(self.acc.folders[0], readonly=True)
                    client.idle()
                    client.idle_check(timeout=IDLE_SECONDS)
                    client.idle_done()
                backoff = 5
            except Exception:
                log.exception("%s: IMAP error, retrying in %ds", self.acc.address, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                if client is not None:
                    with contextlib.suppress(Exception):
                        client.logout()

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.run_blocking)
