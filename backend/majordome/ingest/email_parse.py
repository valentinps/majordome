"""Pure email parsing: RFC 5322 bytes -> NormalizedMessage. No network, unit-tested."""

import email
import email.policy
import email.utils
import re
from datetime import UTC, datetime
from email.message import EmailMessage

from bs4 import BeautifulSoup

from majordome.ingest.normalize import (
    AccountRef,
    AttachmentRef,
    ConversationRef,
    NormalizedMessage,
    SenderRef,
)

# Headers kept in `raw.headers` for display and scam detection (all headers are kept too).
KEY_HEADERS = [
    "From", "To", "Cc", "Reply-To", "Return-Path", "Date", "Subject", "Message-ID",
    "In-Reply-To", "References", "List-Id", "List-Unsubscribe", "Authentication-Results",
    "Received-SPF", "DKIM-Signature",
]
_SUBJECT_PREFIX = re.compile(r"^\s*((re|fwd?|tr|aw|wg)\s*:\s*)+", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n\s*\n\s*(\n\s*)+")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


def clean_subject(subject: str | None) -> str | None:
    return _SUBJECT_PREFIX.sub("", subject).strip() if subject else subject


def _msgids(value: str | None) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def thread_key(msg: EmailMessage, gmail_thread_id: int | None = None) -> str:
    if gmail_thread_id:
        return f"gm:{gmail_thread_id}"
    refs = _msgids(msg.get("References"))
    if refs:
        return refs[0]
    reply_to = _msgids(msg.get("In-Reply-To"))
    if reply_to:
        return reply_to[0]
    own = _msgids(msg.get("Message-ID"))
    return own[0] if own else f"subject:{clean_subject(str(msg.get('Subject') or ''))}"


def _body(msg: EmailMessage) -> tuple[str | None, str]:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return None, "none"
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if part.get_content_subtype() == "html":
        return html_to_text(content), "html"
    return content.strip(), "plain"


def _date(msg: EmailMessage, fallback: datetime | None) -> datetime:
    try:
        dt = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return fallback or datetime.now(UTC)


def parse_email(data: bytes, account: AccountRef, *, folder: str, uid: int, uidvalidity: int,
                internal_date: datetime | None = None,
                gmail_thread_id: int | None = None) -> NormalizedMessage:
    msg: EmailMessage = email.message_from_bytes(data, policy=email.policy.default)  # type: ignore[assignment]

    from_name, from_addr = email.utils.parseaddr(str(msg.get("From") or ""))
    from_addr = from_addr.lower()
    body, body_format = _body(msg)
    subject = str(msg.get("Subject")) if msg.get("Subject") is not None else None

    attachments = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        name = part.get_filename()
        attachments.append(AttachmentRef(name, part.get_content_type(), len(payload),
                                         f"imap:{folder}:{uid}:{name}"))

    message_ids = _msgids(msg.get("Message-ID"))
    external_id = message_ids[0] if message_ids else f"uid:{folder}:{uidvalidity}:{uid}"

    return NormalizedMessage(
        account=account,
        conversation=ConversationRef(
            external_id=thread_key(msg, gmail_thread_id), kind="email_thread",
            title=clean_subject(subject), meta={"folder": folder},
        ),
        sender=SenderRef("email", from_addr, from_name or None) if from_addr else None,
        external_id=external_id,
        ingest_channel="imap",
        sent_at=_date(msg, internal_date),
        subject=subject,
        body_text=body,
        direction="out" if from_addr == account.external_id.lower() else "in",
        raw={
            "folder": folder, "uid": uid, "uidvalidity": uidvalidity,
            "body_format": body_format,
            "headers": {h: str(msg.get(h)) for h in KEY_HEADERS if msg.get(h) is not None},
            "all_headers": [[k, str(v)] for k, v in msg.items()],
        },
        attachments=attachments,
    )
