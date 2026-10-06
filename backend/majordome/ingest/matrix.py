"""Matrix ingester: follows the bridge portal rooms and stores their messages.

Majordome is a *silent* Matrix client. It must never send read receipts, typing notifications,
presence or messages: otherwise bridges would forward them, and contacts would see "read" for
messages the user never saw. This is enforced by an endpoint allowlist in `MatrixClient`.
"""

import asyncio
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from majordome.config import Settings
from majordome.ingest.normalize import (
    AccountRef,
    AttachmentRef,
    ConversationRef,
    NormalizedMessage,
    SenderRef,
    find_message_id,
    store_message,
)
from majordome.store import db
from majordome.store.models import Message

log = logging.getLogger(__name__)

# (method, path regex). Everything else is refused before any request is made.
ALLOWED_ENDPOINTS: list[tuple[str, re.Pattern[str]]] = [
    (m, re.compile(p))
    for m, p in [
        ("POST", r"^/_matrix/client/v3/login$"),
        ("GET", r"^/_matrix/client/v3/sync$"),
        ("POST", r"^/_matrix/client/v3/user/[^/]+/filter$"),
        ("POST", r"^/_matrix/client/v3/rooms/[^/]+/join$"),
        ("GET", r"^/_matrix/client/v3/rooms/[^/]+/state$"),
        ("GET", r"^/_matrix/client/v3/rooms/[^/]+/joined_members$"),
        ("GET", r"^/_matrix/client/v1/media/download/[^/]+/[^/]+$"),
    ]
]


class ForbiddenEndpoint(RuntimeError):
    pass


def check_endpoint(method: str, path: str) -> None:
    decoded = urllib.parse.unquote(path)
    if not any(m == method and p.match(path) for m, p in ALLOWED_ENDPOINTS):
        raise ForbiddenEndpoint(f"{method} {decoded} is not allowed for the silent client")


class MatrixClient:
    def __init__(self, homeserver: str, token: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        self.http = httpx.AsyncClient(base_url=homeserver, timeout=60, transport=transport)

    async def request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        check_endpoint(method, path)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = await self.http.request(method, path, headers=headers, **kw)
        resp.raise_for_status()
        return resp.json()

    async def login(self, user: str, password: str, device_id: str) -> str:
        res = await self.request("POST", "/_matrix/client/v3/login", json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user},
            "password": password,
            "device_id": device_id,
            "initial_device_display_name": "Majordome ingester",
        })
        self.token = res["access_token"]
        return self.token

    async def aclose(self) -> None:
        await self.http.aclose()


def q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


# ---------------------------------------------------------------------------------------------
# Pure event handling (unit-tested)
# ---------------------------------------------------------------------------------------------

PROTOCOL_SOURCES = {"whatsapp": "whatsapp", "discordgo": "discord", "discord": "discord"}
SOURCE_NAMES = {"whatsapp": "WhatsApp", "discord": "Discord"}
MEDIA_TYPES = {"m.image", "m.file", "m.audio", "m.video"}


@dataclass
class RoomInfo:
    room_id: str
    source: str | None  # None: not a bridged portal (management room, space, ...)
    bridgebot: str | None
    title: str | None
    kind: str
    is_space: bool
    members: dict[str, str | None]  # mxid -> display name
    protocol: dict[str, Any] | None = None
    channel: dict[str, Any] | None = None
    receiver: str | None = None  # bridge login the room belongs to (multi-account bridges)
    self_ghosts: frozenset[str] = frozenset()  # ghosts that represent the user themselves

    @property
    def is_portal(self) -> bool:
        return self.source is not None and not self.is_space

    def account(self) -> AccountRef:
        assert self.source and self.bridgebot
        external_id = f"{self.bridgebot}/{self.receiver}" if self.receiver else self.bridgebot
        return AccountRef(self.source, external_id, SOURCE_NAMES.get(self.source, self.source))


def room_info_from_state(room_id: str, state: list[dict[str, Any]], own_mxid: str) -> RoomInfo:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for ev in state:
        by_type.setdefault(ev["type"], []).append(ev)

    create = (by_type.get("m.room.create") or [{}])[0].get("content", {})
    bridge = (by_type.get("m.bridge") or by_type.get("uk.half-shot.bridge") or [{}])[0].get(
        "content", {})
    members = {
        ev["state_key"]: ev["content"].get("displayname")
        for ev in by_type.get("m.room.member", [])
        if ev["content"].get("membership") == "join"
    }
    bridgebot = bridge.get("bridgebot")
    protocol = bridge.get("protocol") or {}
    channel = bridge.get("channel") or {}
    source = PROTOCOL_SOURCES.get(protocol.get("id", ""), protocol.get("id")) if bridge else None
    # Newer (bridgev2) bridges state the room type explicitly; older ones need a heuristic.
    room_type = bridge.get("com.beeper.room_type.v2") or bridge.get("com.beeper.room_type")

    others = [m for m in members if m not in (own_mxid, bridgebot)]
    self_ghosts: frozenset[str] = frozenset()
    contact = others[0] if len(others) == 1 else None
    if room_type == "dm" and len(others) > 1:
        # The user's own ghost joins DMs too; the contact is the ghost named after the channel.
        channel_local = str(channel.get("id", "")).split("@")[0]
        contact = next((m for m in others if channel_local and channel_local in m), None)
        if contact:
            self_ghosts = frozenset(m for m in others if m != contact)

    name = (by_type.get("m.room.name") or [{}])[0].get("content", {}).get("name")
    if not name and contact:
        name = members[contact]
    is_dm = room_type == "dm" if room_type else len(others) <= 1
    return RoomInfo(
        room_id=room_id,
        source=source,
        bridgebot=bridgebot,
        title=name,
        kind="dm" if is_dm else "group",
        is_space=create.get("type") == "m.space" or room_type == "personal_filtering_space",
        members=members,
        protocol=protocol or None,
        channel=channel or None,
        receiver=channel.get("fi.mau.receiver"),
        self_ghosts=self_ghosts,
    )


@dataclass
class Edit:
    target_event_id: str
    new_body: str
    at: datetime


@dataclass
class Redaction:
    target_event_id: str
    at: datetime


_REPLY_FALLBACK = re.compile(r"\A(?:> .*\n)+\n", re.MULTILINE)


def _ts(event: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(event["origin_server_ts"] / 1000, tz=UTC)


def interpret_event(room: RoomInfo, event: dict[str, Any], own_mxid: str,
                    self_ids: frozenset[str] = frozenset()
                    ) -> NormalizedMessage | Edit | Redaction | None:
    """Turn one timeline event of a portal room into something to store (or None to skip).

    `self_ids`: the user's own ghosts learnt from other rooms (groups don't reveal them).
    """
    if not room.is_portal or event.get("sender") == room.bridgebot:
        return None  # bridge status notices are not messages

    etype = event.get("type")
    content = event.get("content") or {}
    if etype == "m.room.redaction":
        target = event.get("redacts") or content.get("redacts")
        return Redaction(target, _ts(event)) if target else None
    if etype not in ("m.room.message", "m.sticker") or not content:
        return None

    relates = content.get("m.relates_to") or {}
    if relates.get("rel_type") == "m.replace":
        new = content.get("m.new_content") or {}
        return Edit(relates.get("event_id", ""), new.get("body", ""), _ts(event))

    body = content.get("body") or ""
    if relates.get("m.in_reply_to"):
        body = _REPLY_FALLBACK.sub("", body, count=1)

    attachments: list[AttachmentRef] = []
    msgtype = content.get("msgtype")
    if msgtype in MEDIA_TYPES or etype == "m.sticker":
        info = content.get("info") or {}
        filename = content.get("filename") or body
        attachments.append(AttachmentRef(filename, info.get("mimetype"), info.get("size"),
                                         content.get("url")))
        # With `filename` set, `body` is a caption; otherwise it is just the file name.
        body = body if content.get("filename") and body != content.get("filename") else ""

    sender = event["sender"]
    return NormalizedMessage(
        account=room.account(),
        conversation=ConversationRef(
            external_id=room.room_id, kind=room.kind, title=room.title,
            meta={"protocol": room.protocol, "channel": room.channel},
        ),
        sender=SenderRef(room.source or "matrix", sender, room.members.get(sender)),
        external_id=event["event_id"],
        ingest_channel="matrix",
        sent_at=_ts(event),
        body_text=body or None,
        direction="out" if sender == own_mxid or sender in room.self_ghosts | self_ids else "in",
        raw={"event": event},
        attachments=attachments,
    )


# ---------------------------------------------------------------------------------------------
# Sync loop
# ---------------------------------------------------------------------------------------------

SYNC_FILTER = {
    "presence": {"types": []},
    "account_data": {"types": []},
    "room": {
        "ephemeral": {"types": []},  # no receipts/typing needed
        "account_data": {"types": []},
        "state": {"lazy_load_members": False},
        "timeline": {"limit": 50},
    },
}


class MatrixIngester:
    STATE_AUTH = "matrix.auth"
    STATE_SYNC = "matrix.sync"
    STATE_SELF = "matrix.self_ghosts"

    def __init__(self, cfg: Settings, client: MatrixClient | None = None):
        self.cfg = cfg
        self.own_mxid = f"@{cfg.matrix_user}:{cfg.matrix_server_name}"
        self.client = client or MatrixClient(cfg.matrix_homeserver)
        self.trusted_inviters = re.compile(cfg.matrix_trusted_inviters)
        self.rooms: dict[str, RoomInfo] = {}
        self.self_ghosts: set[str] = set()

    async def _ensure_login(self) -> None:
        async with db.session() as s:
            self.self_ghosts |= set((await db.get_state(s, self.STATE_SELF) or {}).get("ids", []))
            auth = await db.get_state(s, self.STATE_AUTH)
            if auth:
                self.client.token = auth["access_token"]
                return
            token = await self.client.login(self.cfg.matrix_user, self.cfg.matrix_password,
                                            "MAJORDOME_INGEST")
            await db.set_state(s, self.STATE_AUTH, {"access_token": token})
            await s.commit()

    async def _room(self, room_id: str, refresh: bool = False) -> RoomInfo:
        if refresh or room_id not in self.rooms:
            state = await self.client.request("GET", f"/_matrix/client/v3/rooms/{q(room_id)}/state")
            info = room_info_from_state(room_id, state, self.own_mxid)
            self.rooms[room_id] = info
            if not info.self_ghosts <= self.self_ghosts:
                self.self_ghosts |= info.self_ghosts
                async with db.session() as s:
                    await db.set_state(s, self.STATE_SELF, {"ids": sorted(self.self_ghosts)})
                    await s.commit()
        return self.rooms[room_id]

    async def _handle_invites(self, invites: dict[str, Any]) -> None:
        for room_id, data in invites.items():
            inviter = next(
                (ev["sender"] for ev in data.get("invite_state", {}).get("events", [])
                 if ev["type"] == "m.room.member" and ev.get("state_key") == self.own_mxid),
                None,
            )
            if inviter and self.trusted_inviters.match(inviter):
                log.info("joining %s (invited by %s)", room_id, inviter)
                await self.client.request("POST", f"/_matrix/client/v3/rooms/{q(room_id)}/join",
                                          json={})
            else:
                log.warning("ignoring invite to %s from untrusted %s", room_id, inviter)

    async def _apply(self, s, room: RoomInfo, item) -> None:
        if isinstance(item, NormalizedMessage):
            await store_message(s, item)
        elif room.is_portal:
            msg_id = await find_message_id(s, room.account(), item.target_event_id)
            if msg_id is None:
                return
            m = await s.get(Message, msg_id)
            if isinstance(item, Edit):
                history = m.raw.get("edits", []) + [{"at": item.at.isoformat(),
                                                     "previous": m.body_text}]
                m.raw = {**m.raw, "edits": history}
                m.body_text, m.edited_at = item.new_body, item.at
            else:
                m.deleted_at = item.at  # keep the content: knowing what was deleted is useful

    async def _process(self, sync: dict[str, Any], initial: bool) -> None:
        rooms = sync.get("rooms", {})
        await self._handle_invites(rooms.get("invite", {}))
        async with db.session() as s:
            for room_id, data in rooms.get("join", {}).items():
                state_changed = any(
                    ev["type"] in ("m.bridge", "uk.half-shot.bridge", "m.room.name",
                                   "m.room.member", "m.room.create")
                    for ev in data.get("state", {}).get("events", [])
                    + data.get("timeline", {}).get("events", [])
                    if "state_key" in ev
                )
                room = await self._room(room_id, refresh=state_changed)
                if initial:
                    continue  # history starts "from now"
                for ev in data.get("timeline", {}).get("events", []):
                    item = interpret_event(room, ev, self.own_mxid, frozenset(self.self_ghosts))
                    if item is not None:
                        await self._apply(s, room, item)
            # Checkpoint in the same transaction as the messages: exactly-once ingestion.
            await db.set_state(s, self.STATE_SYNC, {"next_batch": sync["next_batch"],
                                                    "at": datetime.now(UTC).isoformat()})
            await s.commit()

    async def run(self) -> None:
        await self._ensure_login()
        backoff = 1
        while True:
            try:
                async with db.session() as s:
                    since = (await db.get_state(s, self.STATE_SYNC) or {}).get("next_batch")
                params = {"filter": _json(SYNC_FILTER), "timeout": "30000",
                          "set_presence": "offline"}
                if since:
                    params["since"] = since
                else:
                    params["timeout"] = "0"
                sync = await self.client.request("GET", "/_matrix/client/v3/sync", params=params)
                await self._process(sync, initial=since is None)
                backoff = 1
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    log.warning("matrix token rejected, logging in again")
                    async with db.session() as s:
                        await db.set_state(s, self.STATE_AUTH, {})
                        await s.commit()
                    await self._ensure_login()
                    continue
                log.exception("matrix sync failed")
            except Exception:
                log.exception("matrix sync failed")
            else:
                continue
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)



def _json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
