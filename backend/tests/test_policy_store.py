"""Policy applied at ingestion, retroactively through the API, and by retention (needs test DB)."""

from datetime import UTC, datetime, timedelta

import httpx
from argon2 import PasswordHasher
from sqlalchemy import func, select

from majordome.ingest.normalize import (
    AccountRef,
    AttachmentRef,
    ConversationRef,
    NormalizedMessage,
    SenderRef,
    store_message,
)
from majordome.policy.apply import purge_expired
from majordome.store.models import Attachment, Message, PolicyRule

MAIL = AccountRef("email", "me@example.com", "Gmail")
WA = AccountRef("whatsapp", "@whatsappbot:x/336", "WhatsApp")
NOW = datetime.now(UTC)


def mail(ext, sender, body, subject="Hello", minutes=0, attach=False):
    return NormalizedMessage(
        account=MAIL, conversation=ConversationRef(ext, "email_thread", subject),
        sender=SenderRef("email", sender, None), external_id=ext, ingest_channel="imap",
        sent_at=NOW - timedelta(minutes=minutes), body_text=body, subject=subject,
        raw={"folder": "INBOX", "uid": 1, "headers": {"Subject": subject}},
        attachments=[AttachmentRef("a.pdf", "application/pdf", 3, "x")] if attach else [],
    )


async def test_defaults_redact_for_the_agent_but_keep_the_original(s):
    mid = await store_message(s, mail("<1>", "no-reply@bank.fr", "Votre code est 482913"))
    await s.commit()
    m = await s.get(Message, mid)
    assert m.body_text == "Votre code est 482913"
    assert m.body_agent == "Votre code est [OTP_CODE]"
    assert m.visibility == "always" and m.policy["matched_rules"] == []


async def test_rules_at_ingestion(s):
    s.add_all([
        PolicyRule(name="bank", matchers={"sender_domains": ["bank.fr"]},
                   outcomes={"visibility": "never", "retention_days": 30}),
        PolicyRule(name="newsletters", matchers={"keywords": ["unsubscribe"]},
                   outcomes={"storage": "metadata_only"}),
        PolicyRule(name="spam", matchers={"sender_domains": ["spam.biz"]},
                   outcomes={"storage": "drop"}),
    ])
    await s.flush()
    bank = await store_message(s, mail("<b>", "a@bank.fr", "Relevé disponible"))
    news = await store_message(s, mail("<n>", "n@shop.com", "Promo! Unsubscribe here", attach=True))
    spam = await store_message(s, mail("<s>", "x@spam.biz", "Win money"))
    await s.commit()

    b = await s.get(Message, bank)
    assert b.visibility == "never" and b.body_agent is None and b.body_text == "Relevé disponible"
    assert b.expires_at is not None and b.policy["matched_rules"][0]["name"] == "bank"
    n = await s.get(Message, news)
    assert n.body_text is None and n.subject is None and n.raw == {
        "folder": "INBOX", "uid": 1, "metadata_only": True}
    assert await s.scalar(select(func.count(Attachment.id))) == 0
    assert spam is None


async def test_retention_purge(s):
    s.add(PolicyRule(name="short", matchers={"sources": ["email"]},
                     outcomes={"retention_days": 1}))
    await s.flush()
    await store_message(s, mail("<old>", "a@x.com", "old", minutes=60 * 48))
    await store_message(s, mail("<new>", "a@x.com", "new"))
    await s.commit()
    assert await purge_expired(s) == 1
    await s.commit()
    assert await s.scalar(select(Message.body_text)) == "new"


async def test_rules_api_preview_save_and_delete(s, monkeypatch):
    for i in range(3):
        await store_message(s, mail(f"<p{i}>", "promo@shop.com", f"Soldes d'été {i}"))
    await store_message(s, mail("<k>", "friend@x.com", "Dîner samedi ?"))
    await s.commit()

    from majordome.api.app import app
    from majordome.config import settings

    monkeypatch.setattr(settings(), "ui_password_hash", PasswordHasher().hash("correct horse"))
    rule = {"name": "Shop promos", "matchers": {"sender_domains": ["shop.com"]},
            "outcomes": {"storage": "drop"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/api/rules", json=rule)).status_code == 401
        await c.post("/api/login", json={"password": "correct horse"})

        bad = await c.post("/api/rules", json={**rule, "matchers": {}})
        assert bad.status_code == 422 and "condition" in bad.json()["detail"]["errors"][0]

        prev = (await c.post("/api/rules/preview", json=rule)).json()
        assert prev["matched"] == 3 and prev["deleted"] == 3 and len(prev["samples"]) == 3
        assert await s.scalar(select(func.count(Message.id))) == 4  # dry run changed nothing

        hide = {"name": "Hide friend", "matchers": {"senders": ["friend@x.com"]},
                "outcomes": {"visibility": "on_request"}}
        created = (await c.post("/api/rules", json=hide)).json()
        assert created["applied"]["changed"] == 1
        detail = (await c.get("/api/messages", params={"q": "diner"})).json()["messages"][0]
        assert detail["visibility"] == "on_request"

        rid = created["rule"]["id"]
        assert (await c.delete(f"/api/rules/{rid}")).json()["applied"]["changed"] == 1
        listed = (await c.get("/api/rules")).json()
        assert listed["rules"] == [] and {r["id"] for r in listed["redactors"]} >= {"otp", "iban"}
