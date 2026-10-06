import pytest

from majordome.ingest.matrix import (
    Edit,
    ForbiddenEndpoint,
    Redaction,
    check_endpoint,
    interpret_event,
    room_info_from_state,
)
from majordome.ingest.normalize import NormalizedMessage

ME = "@me:majordome.internal"
BOT = "@whatsappbot:majordome.internal"
ALICE = "@whatsapp_33600000001:majordome.internal"
BOB = "@whatsapp_33600000002:majordome.internal"


def member(mxid, name=None, membership="join"):
    return {"type": "m.room.member", "state_key": mxid,
            "content": {"membership": membership, "displayname": name}}


def bridge_state(protocol="whatsapp"):
    return {"type": "m.bridge", "state_key": "x",
            "content": {"bridgebot": BOT, "protocol": {"id": protocol},
                        "channel": {"id": "33600000001"}}}


def dm_room():
    return room_info_from_state("!dm:x", [bridge_state(), member(ME), member(BOT),
                                          member(ALICE, "Alice")], ME)


def ev(content, sender=ALICE, etype="m.room.message", **extra):
    return {"type": etype, "sender": sender, "event_id": "$e1",
            "origin_server_ts": 1_700_000_000_000, "content": content, **extra}


# --- endpoint allowlist: the client must stay silent -----------------------------------------

@pytest.mark.parametrize("method,path", [
    ("GET", "/_matrix/client/v3/sync"),
    ("POST", "/_matrix/client/v3/login"),
    ("POST", "/_matrix/client/v3/rooms/%21abc%3Ax/join"),
    ("GET", "/_matrix/client/v3/rooms/%21abc%3Ax/state"),
])
def test_allowed_endpoints(method, path):
    check_endpoint(method, path)


@pytest.mark.parametrize("method,path", [
    ("POST", "/_matrix/client/v3/rooms/%21abc%3Ax/receipt/m.read/%24ev"),
    ("POST", "/_matrix/client/v3/rooms/%21abc%3Ax/read_markers"),
    ("PUT", "/_matrix/client/v3/rooms/%21abc%3Ax/typing/%40me%3Ax"),
    ("PUT", "/_matrix/client/v3/presence/%40me%3Ax/status"),
    ("PUT", "/_matrix/client/v3/rooms/%21abc%3Ax/send/m.room.message/1"),
    ("PUT", "/_matrix/client/v3/rooms/%21abc%3Ax/state/m.room.name/"),
    ("GET", "/_matrix/client/v3/sync/../rooms/%21a/receipt"),
    ("POST", "/_matrix/client/v3/rooms/%21abc%3Ax/join/extra"),
])
def test_forbidden_endpoints(method, path):
    with pytest.raises(ForbiddenEndpoint):
        check_endpoint(method, path)


# --- room info ------------------------------------------------------------------------------

def test_dm_room_info():
    room = dm_room()
    assert room.is_portal and room.source == "whatsapp" and room.kind == "dm"
    assert room.title == "Alice"


def test_group_room_and_discord_protocol():
    room = room_info_from_state("!g:x", [
        bridge_state("discordgo"), member(ME), member(BOT), member(ALICE), member(BOB),
        {"type": "m.room.name", "state_key": "", "content": {"name": "Family"}},
    ], ME)
    assert room.source == "discord" and room.kind == "group" and room.title == "Family"


def test_management_room_and_space_are_not_portals():
    assert not room_info_from_state("!m:x", [member(ME), member(BOT)], ME).is_portal
    space = room_info_from_state("!s:x", [
        bridge_state(), {"type": "m.room.create", "state_key": "", "content": {"type": "m.space"}},
    ], ME)
    assert not space.is_portal


# --- events ---------------------------------------------------------------------------------

def test_text_message():
    msg = interpret_event(dm_room(), ev({"msgtype": "m.text", "body": "Salut !"}), ME)
    assert isinstance(msg, NormalizedMessage)
    assert msg.body_text == "Salut !" and msg.sender.display_name == "Alice"
    assert msg.account.source == "whatsapp" and msg.conversation.kind == "dm"
    assert msg.direction == "in" and msg.sent_at.year == 2023


def test_reply_fallback_is_stripped():
    content = {"msgtype": "m.text", "body": "> <@x> original\n> line 2\n\nmy answer",
               "m.relates_to": {"m.in_reply_to": {"event_id": "$orig"}}}
    assert interpret_event(dm_room(), ev(content), ME).body_text == "my answer"


def test_image_with_caption_and_without():
    with_caption = interpret_event(dm_room(), ev({
        "msgtype": "m.image", "body": "look at this", "filename": "IMG_1.jpg",
        "url": "mxc://x/abc", "info": {"mimetype": "image/jpeg", "size": 1234},
    }), ME)
    assert with_caption.body_text == "look at this"
    assert with_caption.attachments[0].filename == "IMG_1.jpg"
    assert with_caption.attachments[0].external_ref == "mxc://x/abc"

    no_caption = interpret_event(dm_room(), ev({"msgtype": "m.file", "body": "doc.pdf",
                                                "url": "mxc://x/d"}), ME)
    assert no_caption.body_text is None and no_caption.attachments[0].filename == "doc.pdf"


def test_edit_and_redaction():
    edit = interpret_event(dm_room(), ev({
        "msgtype": "m.text", "body": "* fixed",
        "m.new_content": {"msgtype": "m.text", "body": "fixed"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
    }), ME)
    assert isinstance(edit, Edit) and edit.target_event_id == "$orig" and edit.new_body == "fixed"

    red = interpret_event(dm_room(), ev({}, etype="m.room.redaction", redacts="$orig"), ME)
    assert isinstance(red, Redaction) and red.target_event_id == "$orig"


def test_skips_bridge_notices_non_portals_and_other_events():
    assert interpret_event(dm_room(), ev({"msgtype": "m.notice", "body": "x"}, sender=BOT),
                           ME) is None
    mgmt = room_info_from_state("!m:x", [member(ME), member(BOT)], ME)
    assert interpret_event(mgmt, ev({"msgtype": "m.text", "body": "x"}), ME) is None
    assert interpret_event(dm_room(), ev({"key": "👍"}, etype="m.reaction"), ME) is None
