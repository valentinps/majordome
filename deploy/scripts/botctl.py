#!/usr/bin/env python3
"""Minimal Matrix CLI to talk to bridge bots (login, status) without a full Matrix client.

Usage (from the deploy directory on the server):
    ./scripts/botctl.py send whatsappbot "login phone"
    ./scripts/botctl.py read whatsappbot [count]

Stdlib only. Talks to Synapse on 127.0.0.1:8008 as $MATRIX_USER from .env.
Never sends read receipts or typing notifications.
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent
HS = "http://127.0.0.1:8008"
STATE = DEPLOY / "data" / "botctl.json"


def load_env() -> dict[str, str]:
    env = {}
    for line in (DEPLOY / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    return env


def api(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        HS + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read() or b"{}")


def state() -> dict:
    env = load_env()
    st = json.loads(STATE.read_text()) if STATE.exists() else {"rooms": {}}
    if "token" not in st:
        res = api("POST", "/_matrix/client/v3/login", body={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": env["MATRIX_USER"]},
            "password": env["MATRIX_PASSWORD"],
            "device_id": "BOTCTL",
            "initial_device_display_name": "majordome botctl",
        })
        st["token"] = res["access_token"]
    st["server"] = env["SERVER_NAME"]
    return st


def save(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=2))
    STATE.chmod(0o600)


def room_for(st: dict, bot: str) -> str:
    if bot not in st["rooms"]:
        mxid = f"@{bot}:{st['server']}"
        res = api("POST", "/_matrix/client/v3/createRoom", st["token"], {
            "is_direct": True, "invite": [mxid], "preset": "private_chat",
        })
        st["rooms"][bot] = res["room_id"]
        save(st)
        time.sleep(2)  # let the bot join
    return st["rooms"][bot]


def send(bot: str, text: str) -> None:
    st = state()
    room = urllib.parse.quote(room_for(st, bot))
    txn = str(int(time.time() * 1000))
    api("PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}", st["token"],
        {"msgtype": "m.text", "body": text})
    save(st)


def read(bot: str, count: int = 10) -> None:
    st = state()
    room = urllib.parse.quote(room_for(st, bot))
    res = api("GET", f"/_matrix/client/v3/rooms/{room}/messages?dir=b&limit={count}", st["token"])
    for ev in reversed(res.get("chunk", [])):
        if ev.get("type") != "m.room.message":
            continue
        c = ev["content"]
        extra = f"  [media {c.get('url')}]" if "url" in c else ""
        print(f"{ev['sender']}: {c.get('body', '')}{extra}\n")
    save(st)


if __name__ == "__main__":
    cmd, bot, *rest = sys.argv[1:]
    if cmd == "send":
        send(bot, " ".join(rest))
    elif cmd == "read":
        read(bot, int(rest[0]) if rest else 10)
    else:
        sys.exit(__doc__)
