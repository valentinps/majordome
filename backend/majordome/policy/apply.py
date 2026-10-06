"""Applying policy decisions to messages: at ingestion, retroactively, and for retention."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from majordome.policy.engine import Decision, Facts, Rule, agent_text, resolve
from majordome.store.models import Attachment, Message, PolicyRule

# Raw payload fields kept for metadata-only messages (no content).
_METADATA_RAW_KEYS = {"folder", "uid", "uidvalidity"}


async def load_rules(s: AsyncSession) -> list[Rule]:
    rows = (await s.scalars(select(PolicyRule).order_by(PolicyRule.id))).all()
    return [to_rule(r) for r in rows]


def to_rule(r: PolicyRule) -> Rule:
    return Rule(r.id, r.name, r.matchers or {}, r.outcomes or {}, r.enabled)


def apply_decision(m: Message, d: Decision) -> None:
    """Set the policy-derived fields of a message (storage must already be 'store' or
    'metadata_only'; dropping is done by the caller)."""
    if d.storage == "metadata_only":
        m.body_text = None
        m.subject = None
        m.raw = {k: v for k, v in (m.raw or {}).items() if k in _METADATA_RAW_KEYS}
        m.raw["metadata_only"] = True
    m.visibility = d.visibility
    m.body_agent = agent_text(d, m.body_text)
    m.expires_at = m.sent_at + timedelta(days=d.retention_days) if d.retention_days else None
    m.policy = d.explain()


def facts_of(m: Message) -> Facts:
    return Facts(
        source=m.account.source,
        account_id=m.account_id,
        conversation_id=m.conversation_id,
        conversation_kind=m.conversation.kind,
        sender=m.sender.external_id if m.sender else None,
        subject=m.subject,
        body=m.body_text,
        has_attachment=bool(m.attachments),
    )


@dataclass
class ReapplyStats:
    examined: int = 0
    matched: int = 0  # messages matched by the rule under preview (dry runs)
    changed: int = 0
    wiped: int = 0  # turned metadata-only
    deleted: int = 0
    samples: list[dict[str, Any]] | None = None


async def reapply(s: AsyncSession, rules: list[Rule] | None = None, *, dry_run: bool = False,
                  preview: Rule | None = None, batch: int = 500) -> ReapplyStats:
    """Re-evaluate every stored message against the rules (plus `preview`, if given).

    With dry_run, nothing is written: the stats say what *would* change. Used both after a rule
    is saved and for the "this rule would affect N messages" preview in the UI.
    """
    rules = await load_rules(s) if rules is None else rules
    if preview is not None:
        rules = [r for r in rules if r.id != preview.id] + [preview]
    stats = ReapplyStats(samples=[] if preview else None)
    last_id = 0
    while True:
        msgs = (await s.scalars(
            select(Message).where(Message.id > last_id).order_by(Message.id).limit(batch)
            .options(selectinload(Message.account), selectinload(Message.conversation),
                     selectinload(Message.sender), selectinload(Message.attachments))
        )).all()
        if not msgs:
            break
        last_id = msgs[-1].id
        for m in msgs:
            stats.examined += 1
            d = resolve(rules, facts_of(m))
            if preview is not None and any(i == preview.id for i, _ in d.matched):
                stats.matched += 1
                if stats.samples is not None and len(stats.samples) < 5:
                    stats.samples.append({
                        "id": m.id, "source": m.account.source,
                        "conversation": m.conversation.title,
                        "sender": m.sender.display_name if m.sender else None,
                        "snippet": (m.subject or m.body_text or "")[:120],
                        "sent_at": m.sent_at.isoformat(),
                    })
            if d.storage == "drop":
                stats.deleted += 1
                if not dry_run:
                    await s.delete(m)
                continue
            if d.storage == "metadata_only" and m.body_text is not None:
                stats.wiped += 1
            before = (m.visibility, m.body_agent, m.expires_at, m.policy)
            if dry_run:
                continue
            apply_decision(m, d)
            if d.storage == "metadata_only":
                await s.execute(delete(Attachment).where(Attachment.message_id == m.id))
            if (m.visibility, m.body_agent, m.expires_at, m.policy) != before:
                stats.changed += 1
        if not dry_run:
            await s.flush()
    return stats


async def purge_expired(s: AsyncSession, now: datetime | None = None) -> int:
    res = await s.execute(
        delete(Message).where(Message.expires_at.is_not(None),
                              Message.expires_at < (now or datetime.now(UTC)))
    )
    return res.rowcount or 0

