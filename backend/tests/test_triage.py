"""Triage pipeline with a fake (possibly compromised) model. Needs the test DB."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from majordome.agent import triage
from majordome.config import Settings
from majordome.ingest.normalize import (
    AccountRef,
    ConversationRef,
    NormalizedMessage,
    SenderRef,
    store_message,
)
from majordome.llm import LlmError, LlmResult
from majordome.store.models import LlmCall, Message, Notification, PolicyRule, TriageResult

WEBHOOK = "https://discord.com/api/webhooks/1/user-chosen"
CORPUS = json.loads((Path(triage.__file__).parent / "injection_corpus.json")
                    .read_text("utf-8"))
ACCOUNTS = {"email": AccountRef("email", "me@x.com", "Main"),
            "whatsapp": AccountRef("whatsapp", "@whatsappbot:x/336", "WhatsApp"),
            "discord": AccountRef("discord", "@discordbot:x", "Discord")}
CFG = Settings(triage_batch_size=20, scam_threshold=0.8)


class FakeModel:
    name = "fake"

    def __init__(self, respond):
        self.respond = respond
        self.inputs: list[str] = []

    async def complete(self, system, input, schema, model) -> LlmResult:
        self.inputs.append(input)
        out = self.respond(input)
        if isinstance(out, Exception):
            raise out
        return LlmResult(out, "fake-model", {"input_tokens": 1}, 5)


def batch_items(input: str) -> list[dict[str, Any]]:
    payload = input.split("<messages>\n", 1)[1].rsplit("\n</messages>", 1)[0]
    return json.loads(payload)["messages"]


def result(i, **kw):
    base = {"i": i, "category": "personal", "scam_risk": 0.0, "importance": "digest",
            "summary": "s", "reason": "r"}
    return {**base, **kw}


def echo_all(inp):
    return {"results": [result(i["i"]) for i in batch_items(inp)]}


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_send(url, content, transport=None):
        calls.append((url, content))
        return "sent"

    monkeypatch.setattr(triage.discord, "send", fake_send)
    return calls


async def allow_all(s):
    s.add(PolicyRule(name="all", matchers={"sources": ["email", "whatsapp", "discord"]},
                     outcomes={"visibility": "always"}))
    await s.flush()


async def add(s, source, ext, text, sender="Alice", subject=None, sender_id=None):
    return await store_message(s, NormalizedMessage(
        account=ACCOUNTS[source], conversation=ConversationRef(f"c-{ext}", "dm", sender),
        sender=SenderRef(source, sender_id or f"{sender.lower()}@x.com", sender),
        external_id=ext, ingest_channel="test", sent_at=datetime.now(UTC), body_text=text,
        subject=subject))


async def test_only_readable_redacted_messages_reach_the_model(s, sent):
    await allow_all(s)
    s.add(PolicyRule(name="bank", matchers={"sender_domains": ["bank.fr"]},
                     outcomes={"visibility": "never"}))
    await s.flush()
    await add(s, "email", "<1>", "Votre code est 482913", sender_id="a@x.com")
    await add(s, "email", "<2>", "Solde secret 1234 EUR", sender_id="b@bank.fr")
    await s.commit()
    model = FakeModel(echo_all)
    st = await triage.triage_once(s, CFG, model, WEBHOOK)

    assert st.triaged == 1 and len(model.inputs) == 1
    assert "482913" not in model.inputs[0] and "[OTP_CODE]" in model.inputs[0]
    assert "Solde secret" not in model.inputs[0]
    assert await triage.triage_once(s, CFG, model, WEBHOOK) == triage.TriageStats()


async def test_verdicts_notifications_and_audit(s, sent):
    await allow_all(s)
    await add(s, "whatsapp", "$1", "On se voit à 18h ? Réponds vite", sender="Camille")
    await add(s, "email", "<p>", "Promo -50%", sender="Shop")
    await add(s, "email", "<s>", "Confirmez vos identifiants", sender="Banque")
    await s.commit()

    def respond(inp):
        by_sender = {i["sender"]: i["i"] for i in batch_items(inp)}
        return {"results": [
            result(by_sender["Camille"], importance="interrupt", summary="Camille propose 18h"),
            result(by_sender["Shop"], category="newsletter", importance="silent"),
            result(by_sender["Banque"], category="scam", scam_risk=0.97, importance="interrupt",
                   summary="Demande d'identifiants bancaires"),
        ]}

    st = await triage.triage_once(s, CFG, FakeModel(respond), WEBHOOK)
    assert (st.triaged, st.notified) == (3, 2)
    assert [u for u, _ in sent] == [WEBHOOK, WEBHOOK]
    texts = sorted(t for _, t in sent)
    assert texts[0].startswith("⚠️ Likely scam") and "Banque" in texts[0]
    assert texts[1].startswith("🔔 **Camille** (WhatsApp)")
    kinds = (await s.scalars(select(Notification.kind))).all()
    assert sorted(kinds) == ["important", "scam_warning"]
    call = await s.scalar(select(LlmCall))
    assert call.purpose == "triage" and len(call.message_ids) == 3 and call.error is None


async def test_malformed_output_is_dropped_and_retried(s, sent):
    await allow_all(s)
    await add(s, "email", "<1>", "a")
    await add(s, "email", "<2>", "b")
    await s.commit()
    bad = {"results": [
        result(1, importance="EMERGENCY"),  # invalid enum
        result(2, scam_risk="lots"),  # invalid number
        result(99, importance="interrupt"),  # not in the batch
        result(True, importance="interrupt"),  # a bool is not an index
    ]}
    st = await triage.triage_once(s, CFG, FakeModel(lambda _: bad), WEBHOOK)
    assert st.triaged == 0 and sent == []
    assert await s.scalar(select(func.count(TriageResult.message_id))) == 0
    st = await triage.triage_once(s, CFG, FakeModel(echo_all), WEBHOOK)
    assert st.triaged == 2


async def test_llm_failure_is_audited_and_nothing_is_lost(s, sent):
    await allow_all(s)
    await add(s, "email", "<1>", "a")
    await s.commit()
    st = await triage.triage_once(s, CFG, FakeModel(lambda _: LlmError("gateway 502")), WEBHOOK)
    assert st.failed and st.triaged == 0
    assert (await s.scalar(select(LlmCall.error))) == "gateway 502"
    assert len(await triage.pending(s, CFG, 10)) == 1


async def test_injection_corpus_with_a_fully_compromised_model(s, sent):
    """Worst case: the model obeys every injected instruction and echoes attacker text.
    Notifications must still go only to the user's webhook, carry no link or mention, and
    nothing else in the database may change."""
    await allow_all(s)
    for case in CORPUS:
        await add(s, case["source"], case["id"], case["text"], sender=case["sender"],
                  subject=case.get("subject"), sender_id=case["sender"])
    await s.commit()
    rules_before = await s.scalar(select(func.count(PolicyRule.id)))
    messages_before = await s.scalar(select(func.count(Message.id)))

    def obey(inp):
        return {"results": [
            result(i["i"], importance="interrupt", summary=(i["text"] or "")[:200],
                   reason=(i["text"] or "")[:160]) for i in batch_items(inp)
        ] + [result(99, importance="interrupt")]}

    st = await triage.triage_once(s, CFG, FakeModel(obey), WEBHOOK)
    assert st.triaged == len(CORPUS) and st.notified == len(CORPUS)
    assert {u for u, _ in sent} == {WEBHOOK}
    for _, text in sent:
        assert "http" not in text and "www." not in text and "![" not in text
        assert "@everyone" not in text and "<@" not in text and "<#" not in text
        assert not any(c in text for c in "​‮⁦⁩")
        assert len(text) <= 400
    assert await s.scalar(select(func.count(PolicyRule.id))) == rules_before
    assert await s.scalar(select(func.count(Message.id))) == messages_before


async def test_alerts_api(s, sent, monkeypatch):
    import httpx
    from argon2 import PasswordHasher

    from majordome.api.app import app
    from majordome.config import settings

    await allow_all(s)
    await add(s, "whatsapp", "$1", "Urgent: rappelle-moi", sender="Camille")
    await s.commit()
    await triage.triage_once(s, CFG, FakeModel(lambda inp: {"results": [
        result(1, importance="interrupt", summary="Camille demande un rappel")]}), WEBHOOK)

    monkeypatch.setattr(settings(), "ui_password_hash", PasswordHasher().hash("correct horse"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/api/login", json={"password": "correct horse"})
        alerts = (await c.get("/api/notifications")).json()["notifications"]
        assert len(alerts) == 1 and not alerts[0]["read"]
        assert alerts[0]["message"]["triage"]["summary"] == "Camille demande un rappel"
        assert alerts[0]["delivery"] == {"discord": "sent"}
        assert (await c.get("/api/health")).json()["unread_alerts"] == 1
        assert (await c.post("/api/notifications/read", json={})).json()["updated"] == 1
        assert (await c.get("/api/health")).json()["unread_alerts"] == 0
