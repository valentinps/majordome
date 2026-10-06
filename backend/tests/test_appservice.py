"""End-to-end appservice ingestion: Synapse transaction -> stored message (needs test DB)."""

import json

import httpx
from sqlalchemy import func, select

from majordome.config import Settings
from majordome.ingest.matrix import AppserviceIngester, MatrixClient
from majordome.store.models import Message

ME = "@me:majordome.internal"
BOT = "@whatsappbot:majordome.internal"
SELF = "@whatsapp_lid-41699871080509:majordome.internal"
CONTACT = "@whatsapp_lid-42030784893169:majordome.internal"
ROOM = "!wa:majordome.internal"

ROOM_STATE = [
    {"type": "m.bridge", "state_key": "majordome.internal/whatsapp", "content": {
        "bridgebot": BOT, "protocol": {"id": "whatsapp"},
        "channel": {"id": "42030784893169@lid", "fi.mau.receiver": "33769661108"},
        "com.beeper.room_type": "dm"}},
    *({"type": "m.room.member", "state_key": u, "content": {"membership": m, "displayname": n}}
      for u, m, n in [(ME, "invite", "me"), (BOT, "join", "bot"), (SELF, "join", "+337"),
                      (CONTACT, "join", "Valentin")]),
]


def ingester(requests: list[httpx.Request]) -> AppserviceIngester:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=ROOM_STATE)

    cfg = Settings(matrix_hs_token="hs-secret", matrix_as_token="as-secret")
    client = MatrixClient("http://synapse", "as-secret", transport=httpx.MockTransport(handler))
    return AppserviceIngester(cfg, client)


def msg(event_id, sender, body, **content):
    return {"type": "m.room.message", "room_id": ROOM, "sender": sender, "event_id": event_id,
            "origin_server_ts": 1_791_300_000_000,
            "content": {"msgtype": "m.text", "body": body, **content}}


async def test_transaction_is_stored_once_and_reads_state_as_a_ghost(s):
    requests: list[httpx.Request] = []
    ing = ingester(requests)
    txn = [msg("$1", CONTACT, "Ta machine est finie"), msg("$2", SELF, "merci !")]

    assert await ing.handle_transaction("t1", txn) == 2
    assert await ing.handle_transaction("t1", txn) == 0  # Synapse retry: ignored
    rows = (await s.execute(select(Message.external_id, Message.direction, Message.body_text)
                            .order_by(Message.external_id))).all()
    assert rows == [("$1", "in", "Ta machine est finie"), ("$2", "out", "merci !")]

    # Room state is read as the event sender (a bridge ghost), never as the user, and GET only.
    assert {r.method for r in requests} == {"GET"}
    assert requests[0].url.params["user_id"] == CONTACT


async def test_edit_and_events_from_unknown_rooms(s):
    ing = ingester([])
    await ing.handle_transaction("t1", [msg("$1", CONTACT, "typo")])
    await ing.handle_transaction("t2", [msg("$e", CONTACT, "* fixed",
                                            **{"m.new_content": {"body": "fixed"},
                                               "m.relates_to": {"rel_type": "m.replace",
                                                                "event_id": "$1"}})])
    m = await s.scalar(select(Message).where(Message.external_id == "$1"))
    assert m.body_text == "fixed" and m.raw["edits"][0]["previous"] == "typo"
    # An event sent by a non-bridge user in a room we have never seen cannot be resolved.
    other = {**msg("$x", "@stranger:elsewhere", "hi"), "room_id": "!unknown:x"}
    assert await ing.handle_transaction("t3", [other]) == 0
    assert await s.scalar(select(func.count(Message.id))) == 1


async def test_http_requires_hs_token(s):
    app = ingester([]).http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://w") as c:
        body = json.dumps({"events": [msg("$1", CONTACT, "hello")]})
        assert (await c.put("/_matrix/app/v1/transactions/1", content=body)).status_code == 403
        bad = {"Authorization": "Bearer nope"}
        assert (await c.put("/_matrix/app/v1/transactions/1", content=body,
                            headers=bad)).status_code == 403
        ok = {"Authorization": "Bearer hs-secret"}
        assert (await c.put("/_matrix/app/v1/transactions/1", content=body,
                            headers=ok)).status_code == 200
    assert await s.scalar(select(func.count(Message.id))) == 1
