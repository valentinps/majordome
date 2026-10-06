"""Source-independent message representation, and the single entry point that stores it.

Every ingester (matrix, imap, android) converts what it receives into `NormalizedMessage`
and calls `store_message`, which applies the user's policy rules before anything is stored.
Dedup (M5) will hook in here too.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from majordome.policy.apply import apply_decision, load_defaults, load_rules
from majordome.policy.engine import Facts, resolve
from majordome.store.models import Account, Attachment, Conversation, Identity, Message


@dataclass(frozen=True)
class AccountRef:
    source: str
    external_id: str
    name: str


@dataclass(frozen=True)
class ConversationRef:
    external_id: str
    kind: str  # dm, group, email_thread
    title: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SenderRef:
    source: str
    external_id: str
    display_name: str | None = None


@dataclass(frozen=True)
class AttachmentRef:
    filename: str | None
    mime_type: str | None
    size: int | None
    external_ref: str | None


@dataclass
class NormalizedMessage:
    account: AccountRef
    conversation: ConversationRef
    sender: SenderRef | None
    external_id: str
    ingest_channel: str
    sent_at: datetime
    body_text: str | None
    subject: str | None = None
    direction: str = "in"
    raw: dict[str, Any] = field(default_factory=dict)
    attachments: list[AttachmentRef] = field(default_factory=list)


async def _upsert_returning_id(s: AsyncSession, model, conflict_cols: list[str], values: dict,
                               update_cols: list[str]) -> int:
    stmt = insert(model).values(**values)
    # A no-op update is still needed so RETURNING yields the id of an existing row.
    cols = update_cols or conflict_cols[:1]
    set_ = {c: stmt.excluded[c] for c in cols}
    stmt = stmt.on_conflict_do_update(index_elements=conflict_cols, set_=set_).returning(model.id)
    return (await s.execute(stmt)).scalar_one()


async def upsert_account(s: AsyncSession, ref: AccountRef) -> int:
    return await _upsert_returning_id(
        s, Account, ["source", "external_id"],
        {"source": ref.source, "external_id": ref.external_id, "name": ref.name}, [],
    )


async def upsert_conversation(s: AsyncSession, account_id: int, ref: ConversationRef) -> int:
    values = {"account_id": account_id, "external_id": ref.external_id, "kind": ref.kind,
              "title": ref.title, "meta": ref.meta}
    update_cols = ["kind", "meta"] + (["title"] if ref.title else [])
    return await _upsert_returning_id(s, Conversation, ["account_id", "external_id"], values,
                                      update_cols)


async def upsert_identity(s: AsyncSession, ref: SenderRef) -> int:
    values = {"source": ref.source, "external_id": ref.external_id,
              "display_name": ref.display_name}
    return await _upsert_returning_id(s, Identity, ["source", "external_id"], values,
                                      ["display_name"] if ref.display_name else [])


async def store_message(s: AsyncSession, msg: NormalizedMessage) -> int | None:
    """Apply policy and insert a message. Returns its id, or None if it was already stored
    (idempotent) or the policy says not to keep it."""
    account_id = await upsert_account(s, msg.account)
    conversation_id = await upsert_conversation(s, account_id, msg.conversation)
    sender_id = await upsert_identity(s, msg.sender) if msg.sender else None

    decision = resolve(await load_rules(s), Facts(
        source=msg.account.source, account_id=account_id, conversation_id=conversation_id,
        conversation_kind=msg.conversation.kind,
        sender=msg.sender.external_id if msg.sender else None,
        subject=msg.subject, body=msg.body_text, has_attachment=bool(msg.attachments),
    ), await load_defaults(s))
    if decision.storage == "drop":
        return None
    shaped = Message(subject=msg.subject, body_text=msg.body_text, raw=msg.raw,
                     sent_at=msg.sent_at)
    apply_decision(shaped, decision)
    attachments = msg.attachments if decision.storage == "store" else []

    stmt = (
        insert(Message)
        .values(
            account_id=account_id, conversation_id=conversation_id,
            sender_identity_id=sender_id, direction=msg.direction,
            external_id=msg.external_id, ingest_channel=msg.ingest_channel,
            sent_at=msg.sent_at, subject=shaped.subject, body_text=shaped.body_text,
            raw=shaped.raw, body_agent=shaped.body_agent, visibility=shaped.visibility,
            expires_at=shaped.expires_at, policy=shaped.policy,
        )
        .on_conflict_do_nothing(index_elements=["account_id", "external_id"])
        .returning(Message.id)
    )
    message_id = (await s.execute(stmt)).scalar_one_or_none()
    if message_id is None:
        return None

    for a in attachments:
        s.add(Attachment(message_id=message_id, filename=a.filename, mime_type=a.mime_type,
                         size=a.size, external_ref=a.external_ref))
    await s.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .where((Conversation.last_message_at.is_(None))
               | (Conversation.last_message_at < msg.sent_at))
        .values(last_message_at=msg.sent_at)
    )
    return message_id


async def find_message_id(s: AsyncSession, account: AccountRef, external_id: str) -> int | None:
    return await s.scalar(
        select(Message.id)
        .join(Account, Account.id == Message.account_id)
        .where(Account.source == account.source, Account.external_id == account.external_id,
               Message.external_id == external_id)
    )
