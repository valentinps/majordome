"""Normalized data model (see docs/PLAN.md §5). M1 subset: no policy, triage or watchers yet."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB}


class Account(Base):
    """A concrete account on a source, e.g. one WhatsApp login or one mailbox."""

    __tablename__ = "account"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # whatsapp, discord, email, android
    external_id: Mapped[str] = mapped_column(Text)  # bridge bot mxid, email address, ...
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    """A chat, group, channel or email thread."""

    __tablename__ = "conversation"
    __table_args__ = (UniqueConstraint("account_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(Text)  # matrix room id, email thread key
    kind: Mapped[str] = mapped_column(String(16))  # dm, group, email_thread
    title: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship()


class Contact(Base):
    """One real person or entity, unified across platforms (merging comes in M6)."""

    __tablename__ = "contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)


class Identity(Base):
    """A handle on one platform (matrix ghost, email address, ...)."""

    __tablename__ = "identity"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contact.id", ondelete="SET NULL"))


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id"),
        Index("ix_message_sent_at", "sent_at"),
        Index("ix_message_conversation_sent_at", "conversation_id", "sent_at"),
        Index("ix_message_fts", "fts", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"))
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id", ondelete="CASCADE"))
    sender_identity_id: Mapped[int | None] = mapped_column(
        ForeignKey("identity.id", ondelete="SET NULL")
    )
    direction: Mapped[str] = mapped_column(String(8), default="in")  # in, out
    external_id: Mapped[str] = mapped_column(Text)  # matrix event id, email Message-ID
    ingest_channel: Mapped[str] = mapped_column(String(16))  # matrix, imap, android
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    # Redacted text: the only version an LLM will ever see (None: the agent may not see it).
    body_agent: Mapped[str | None] = mapped_column(Text)
    # Policy outcome (see majordome/policy): agent visibility, expiry, and the "why" record.
    visibility: Mapped[str] = mapped_column(String(24), default="always", server_default="always")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    policy: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fts: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', majordome_unaccent("
            "coalesce(subject, '') || ' ' || coalesce(body_text, '')))",
            persisted=True,
        ),
    )

    conversation: Mapped[Conversation] = relationship()
    account: Mapped[Account] = relationship()
    sender: Mapped[Identity | None] = relationship()
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="message")


class PolicyRule(Base):
    """A user-defined rule: `matchers` (when) -> `outcomes` (then). See majordome/policy."""

    __tablename__ = "policy_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True, server_default="true")
    matchers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    outcomes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Attachment(Base):
    __tablename__ = "attachment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("message.id", ondelete="CASCADE"))
    filename: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(BigInteger)
    external_ref: Mapped[str | None] = mapped_column(Text)  # mxc:// url, email part id

    message: Mapped[Message] = relationship(back_populates="attachments")


class SyncState(Base):
    """Checkpoints for ingesters (matrix since-token, IMAP UIDs, ...) and health info."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
