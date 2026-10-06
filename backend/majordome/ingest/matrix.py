"""Matrix ingester: receives bridge portal events as an *appservice* and stores them.

Majordome never acts as the user on Matrix. Any event the user's Matrix account sends into a
portal (a message, a receipt, even joining the room) makes bridges such as mautrix-whatsapp send
a read receipt to the real network, which clears notifications on the user's phone.

So Majordome registers as a passive appservice with *non-exclusive* namespaces covering the
bridge bots and ghosts: Synapse then pushes every event of every room those users are in,
without anyone joining anything. Room state is read (GET only) by masquerading as a bridge user
already in the room. An endpoint allowlist in `MatrixClient` enforces that nothing is ever sent.
"""

import hmac
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

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

# (method, path regex). Read-only: everything else is refused before any request is made.
ALLOWED_ENDPOINTS: list[tuple[str, re.Pattern[str]]] = [
    (m, re.compile(p))
    for m, p in [
        ("GET", r"^/_matrix/client/v3/rooms/[^/]+/state$"),
        ("GET", r"^/_matrix/client/v1/media/download/[^/]+/[^/]+$"),
    ]
]


class ForbiddenEndpoint(RuntimeError):
    pass


def check_endpoint(method: str, path: str) -> None:
    decoded = urllib.parse.unquote(path)
    if not any(m == method and p.match(path) for m, p in ALLOWED_ENDPOINTS):
        raise ForbiddenEndpoint(f"{method} {decoded} is not allowed: Majordome is read-only")


class MatrixClient:
    """Appservice client. `as_user` masquerades (read-only) as a bridge user in the room."""

    def __init__(self, homeserver: str, as_token: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.as_token = as_token
        self.http = httpx.AsyncClient(base_url=homeserver, timeout=60, transport=transport)

    async def get(self, path: str, as_user: str | None = None) -> Any:
        check_endpoint("GET", path)
        params = {"user_id": as_user} if as_user else {}
        resp = await self.http.get(path, params=params,
                                   headers={"Authorization": f"Bearer {self.as_token}"})
        resp.raise_for_status()
        return resp.json()

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
# Appservice: Synapse pushes transactions of events to us
# ---------------------------------------------------------------------------------------------

ROOM_INFO_STATE = ("m.bridge", "uk.half-shot.bridge", "m.room.name", "m.room.member",
                   "m.room.create")


class AppserviceIngester:
    STATE_AS = "matrix.appservice"
    STATE_SELF = "matrix.self_ghosts"
    TXN_MEMORY = 200

    def __init__(self, cfg: Settings, client: MatrixClient | None = None):
        self.cfg = cfg
        self.own_mxid = f"@{cfg.matrix_user}:{cfg.matrix_server_name}"
        self.client = client or MatrixClient(cfg.matrix_homeserver, cfg.matrix_as_token)
        self.bridge_users = re.compile(cfg.matrix_bridge_users)
        self.rooms: dict[str, RoomInfo] = {}
        self.self_ghosts: set[str] | None = None
        self.recent_txns: list[str] | None = None

    async def _load(self, s) -> None:
        if self.self_ghosts is None:
            self.self_ghosts = set((await db.get_state(s, self.STATE_SELF) or {}).get("ids", []))
        if self.recent_txns is None:
            self.recent_txns = (await db.get_state(s, self.STATE_AS) or {}).get("txns", [])

    async def _room(self, s, room_id: str, as_user: str | None, refresh: bool) -> RoomInfo | None:
        if refresh or room_id not in self.rooms:
            if not as_user:
                return self.rooms.get(room_id)
            state = await self.client.get(f"/_matrix/client/v3/rooms/{q(room_id)}/state", as_user)
            info = room_info_from_state(room_id, state, self.own_mxid)
            self.rooms[room_id] = info
            assert self.self_ghosts is not None
            if not info.self_ghosts <= self.self_ghosts:
                self.self_ghosts |= info.self_ghosts
                await db.set_state(s, self.STATE_SELF, {"ids": sorted(self.self_ghosts)})
        return self.rooms[room_id]

    async def _apply(self, s, room: RoomInfo, item) -> None:
        if isinstance(item, NormalizedMessage):
            await store_message(s, item)
            return
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

    async def handle_transaction(self, txn_id: str, events: list[dict[str, Any]]) -> int:
        """Apply one Synapse transaction atomically. Returns the number of events applied."""
        async with db.session() as s:
            await self._load(s)
            assert self.recent_txns is not None and self.self_ghosts is not None
            if txn_id in self.recent_txns:
                return 0  # Synapse retried a transaction we already committed
            applied = 0
            for ev in events:
                room_id = ev.get("room_id")
                if not room_id:
                    continue
                sender = ev.get("sender", "")
                as_user = sender if self.bridge_users.match(sender) else None
                refresh = ev.get("type") in ROOM_INFO_STATE and "state_key" in ev
                room = await self._room(s, room_id, as_user, refresh)
                if room is None or not room.is_portal:
                    continue
                item = interpret_event(room, ev, self.own_mxid, frozenset(self.self_ghosts))
                if item is not None:
                    await self._apply(s, room, item)
                    applied += 1
            self.recent_txns = (self.recent_txns + [txn_id])[-self.TXN_MEMORY:]
            await db.set_state(s, self.STATE_AS, {"txns": self.recent_txns,
                                                  "at": datetime.now(UTC).isoformat()})
            await s.commit()
        if applied:
            log.info("transaction %s: %d event(s) applied", txn_id, applied)
        return applied

    # --- HTTP (Synapse -> Majordome) ---------------------------------------------------------

    def _authorized(self, request: Request) -> bool:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        token = token or request.query_params.get("access_token", "")
        return bool(self.cfg.matrix_hs_token) and hmac.compare_digest(
            token.encode(), self.cfg.matrix_hs_token.encode())

    def http_app(self) -> Starlette:
        async def transactions(request: Request) -> JSONResponse:
            if not self._authorized(request):
                return JSONResponse({"errcode": "M_FORBIDDEN"}, status_code=403)
            body = await request.json()
            # Raises on failure: Synapse gets a 500 and retries the transaction later.
            await self.handle_transaction(request.path_params["txn_id"], body.get("events", []))
            return JSONResponse({})

        async def not_found(request: Request) -> JSONResponse:
            if not self._authorized(request):
                return JSONResponse({"errcode": "M_FORBIDDEN"}, status_code=403)
            return JSONResponse({"errcode": "M_NOT_FOUND"}, status_code=404)

        async def ping(request: Request) -> JSONResponse:
            if not self._authorized(request):
                return JSONResponse({"errcode": "M_FORBIDDEN"}, status_code=403)
            return JSONResponse({})

        return Starlette(routes=[
            Route("/_matrix/app/v1/transactions/{txn_id}", transactions, methods=["PUT"]),
            Route("/transactions/{txn_id}", transactions, methods=["PUT"]),
            Route("/_matrix/app/v1/ping", ping, methods=["POST"]),
            Route("/_matrix/app/v1/users/{user_id}", not_found, methods=["GET"]),
            Route("/_matrix/app/v1/rooms/{alias}", not_found, methods=["GET"]),
        ])
