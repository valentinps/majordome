from datetime import UTC, datetime, timedelta

import httpx
from argon2 import PasswordHasher

from majordome.ingest.normalize import (
    AccountRef,
    ConversationRef,
    NormalizedMessage,
    SenderRef,
    store_message,
)

WA = AccountRef("whatsapp", "@whatsappbot:majordome.internal", "WhatsApp")
MAIL = AccountRef("email", "me@example.com", "Gmail")
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def msg(account=WA, ext="$1", body="hello", minutes=0, conv="!room:x", subject=None):
    return NormalizedMessage(
        account=account, conversation=ConversationRef(conv, "dm", "Alice"),
        sender=SenderRef(account.source, "@alice:x", "Alice"), external_id=ext,
        ingest_channel="matrix", sent_at=T0 + timedelta(minutes=minutes), body_text=body,
        subject=subject,
    )


async def test_store_is_idempotent(s):
    first = await store_message(s, msg())
    again = await store_message(s, msg())
    await s.commit()
    assert first is not None and again is None


async def test_accent_insensitive_search_and_filters(s, monkeypatch):
    await store_message(s, msg(ext="$1", body="La machine à laver a terminé son cycle d'été"))
    await store_message(s, msg(ext="$2", body="unrelated", minutes=1))
    await store_message(s, msg(account=MAIL, ext="<m@x>", body="Votre colis", conv="<m@x>",
                               subject="Livraison prévue", minutes=2))
    await s.commit()

    from majordome.api.app import app
    from majordome.config import settings

    monkeypatch.setattr(settings(), "ui_password_hash", PasswordHasher().hash("correct horse"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/messages")).status_code == 401
        assert (await c.post("/api/login", json={"password": "nope"})).status_code == 401
        assert (await c.post("/api/login", json={"password": "correct horse"})).status_code == 200

        all_msgs = (await c.get("/api/messages")).json()["messages"]
        assert [m["snippet"] for m in all_msgs][0] == "Votre colis"  # newest first
        assert len(all_msgs) == 3

        hits = (await c.get("/api/messages", params={"q": "ete termine"})).json()["messages"]
        assert [h["snippet"][:10] for h in hits] == ["La machine"]
        hits = (await c.get("/api/messages", params={"q": "livraison"})).json()["messages"]
        assert hits[0]["source"] == "email"

        wa = (await c.get("/api/messages", params={"source": "whatsapp"})).json()["messages"]
        assert {m["source"] for m in wa} == {"whatsapp"}

        oldest = all_msgs[1]
        page = (await c.get("/api/messages", params={
            "before": oldest["sent_at"], "before_id": oldest["id"]})).json()["messages"]
        assert len(page) == 1

        detail = (await c.get(f"/api/messages/{all_msgs[0]['id']}")).json()
        assert detail["body"] == "Votre colis"
        assert "Content-Security-Policy" in (await c.get("/api/me")).headers
