"""HTTP API and web UI host. Single user, password login, signed session cookie."""

import asyncio
from datetime import datetime
from typing import Annotated, Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from majordome.config import settings
from majordome.ingest import bridge_status
from majordome.policy.apply import load_defaults, reapply, save_defaults
from majordome.policy.engine import Rule, validate
from majordome.policy.redact import BUILTIN
from majordome.store import db
from majordome.store.models import Account, Conversation, Identity, Message, PolicyRule, SyncState

COOKIE = "majordome_session"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")

app = FastAPI(title="Majordome", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _serializer() -> URLSafeTimedSerializer:
    secret = settings().session_secret
    if len(secret) < 32:
        raise HTTPException(500, "MAJORDOME_SESSION_SECRET is not configured")
    return URLSafeTimedSerializer(secret, salt="session")


def require_user(request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401)
    try:
        _serializer().loads(token, max_age=settings().session_max_age_s)
    except BadSignature as e:
        raise HTTPException(401) from e


async def get_session():
    async with db.session() as s:
        yield s


Session = Annotated[AsyncSession, Depends(get_session)]
Auth = Depends(require_user)


class LoginIn(BaseModel):
    password: str


def _password_hash() -> str:
    cfg = settings()
    if cfg.ui_password_hash:
        return cfg.ui_password_hash
    f = cfg.ui_password_hash_file
    return f.read_text().strip() if f.exists() else ""


@app.post("/api/login")
async def login(body: LoginIn, response: Response) -> dict:
    h = _password_hash()
    try:
        if not h or not PasswordHasher().verify(h, body.password):
            raise VerificationError
    except VerificationError as e:
        await asyncio.sleep(1)  # slow down guessing
        raise HTTPException(401, "wrong password") from e
    response.set_cookie(COOKIE, _serializer().dumps({"u": "me"}), httponly=True,
                        samesite="strict", max_age=settings().session_max_age_s)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me", dependencies=[Auth])
async def me() -> dict:
    return {"user": "me"}


def _message_summary(m: Message) -> dict[str, Any]:
    body = m.body_text or ""
    return {
        "id": m.id,
        "source": m.account.source,
        "account": m.account.name,
        "conversation_id": m.conversation_id,
        "conversation": m.conversation.title,
        "conversation_kind": m.conversation.kind,
        "sender": (m.sender.display_name or m.sender.external_id) if m.sender else None,
        "direction": m.direction,
        "sent_at": m.sent_at.isoformat(),
        "subject": m.subject,
        "snippet": body[:280],
        "attachments": len(m.attachments),
        "edited": m.edited_at is not None,
        "deleted": m.deleted_at is not None,
        "visibility": m.visibility,
    }


@app.get("/api/messages", dependencies=[Auth])
async def list_messages(
    s: Session,
    q: str | None = None,
    source: Annotated[list[str] | None, Query()] = None,
    conversation_id: int | None = None,
    before: datetime | None = None,
    before_id: int | None = None,
    limit: int = Query(50, le=200),
) -> dict:
    stmt = (
        select(Message)
        .join(Account, Account.id == Message.account_id)
        .options(selectinload(Message.account), selectinload(Message.conversation),
                 selectinload(Message.sender), selectinload(Message.attachments))
    )
    if q:
        stmt = stmt.where(
            Message.fts.op("@@")(func.websearch_to_tsquery("simple", func.majordome_unaccent(q)))
        )
    if source:
        stmt = stmt.where(Account.source.in_(source))
    if conversation_id:
        stmt = stmt.where(Message.conversation_id == conversation_id)
    if before and before_id:  # keyset pagination
        stmt = stmt.where(or_(Message.sent_at < before,
                              and_(Message.sent_at == before, Message.id < before_id)))
    stmt = stmt.order_by(Message.sent_at.desc(), Message.id.desc()).limit(limit)
    rows = (await s.scalars(stmt)).all()
    return {"messages": [_message_summary(m) for m in rows]}


@app.get("/api/messages/{message_id}", dependencies=[Auth])
async def get_message(message_id: int, s: Session) -> dict:
    m = await s.scalar(
        select(Message).where(Message.id == message_id)
        .options(selectinload(Message.account), selectinload(Message.conversation),
                 selectinload(Message.sender), selectinload(Message.attachments))
    )
    if m is None:
        raise HTTPException(404)
    return {
        **_message_summary(m),
        "body": m.body_text,
        "sender_id": m.sender.external_id if m.sender else None,
        "headers": m.raw.get("headers"),
        "edits": m.raw.get("edits", []),
        "visibility": m.visibility,
        "agent_view": m.body_agent,
        "policy": m.policy or {},
        "expires_at": m.expires_at.isoformat() if m.expires_at else None,
        "conversation_id": m.conversation_id,
        "account_id": m.account_id,
        "metadata_only": bool((m.raw or {}).get("metadata_only")),
        "attachment_list": [{"filename": a.filename, "mime_type": a.mime_type, "size": a.size}
                            for a in m.attachments],
    }


@app.get("/api/conversations", dependencies=[Auth])
async def list_conversations(s: Session, limit: int = Query(100, le=500)) -> dict:
    rows = (await s.execute(
        select(Conversation, Account.source)
        .join(Account, Account.id == Conversation.account_id)
        .where(Conversation.last_message_at.is_not(None))
        .order_by(Conversation.last_message_at.desc()).limit(limit)
    )).all()
    return {"conversations": [
        {"id": c.id, "title": c.title, "kind": c.kind, "source": src,
         "last_message_at": c.last_message_at.isoformat()} for c, src in rows
    ]}


@app.get("/api/health", dependencies=[Auth])
async def health(s: Session) -> dict:
    rows = (await s.scalars(select(SyncState).where(
        SyncState.key.like("imap.%")))).all()
    bridges = (await s.scalars(select(SyncState).where(SyncState.key.like("bridge.%")))).all()
    counts = dict((await s.execute(
        select(Account.source, func.count(Message.id))
        .join(Message, Message.account_id == Account.id).group_by(Account.source)
    )).all())
    return {
        "ingesters": [{"key": r.key, "updated_at": r.updated_at.isoformat()} for r in rows],
        "bridges": [{"name": b.key.removeprefix("bridge."), **bridge_status.summarize(b.value)}
                    for b in sorted(bridges, key=lambda b: b.key)],
        "message_counts": counts,
    }


# --- Policy rules (user-managed only; agents never reach these endpoints) ---

class RuleIn(BaseModel):
    name: str
    enabled: bool = True
    matchers: dict[str, Any]
    outcomes: dict[str, Any]


def _rule_out(r: PolicyRule) -> dict[str, Any]:
    return {"id": r.id, "name": r.name, "enabled": r.enabled, "matchers": r.matchers,
            "outcomes": r.outcomes, "updated_at": r.updated_at.isoformat()}


def _check(body: RuleIn) -> None:
    errors = validate(body.matchers, body.outcomes)
    if not body.name.strip():
        errors.insert(0, "Give the rule a name.")
    if errors:
        raise HTTPException(422, {"errors": errors})


def _stats(st) -> dict[str, Any]:
    return {"examined": st.examined, "matched": st.matched, "changed": st.changed,
            "wiped": st.wiped, "deleted": st.deleted, "samples": st.samples}


@app.get("/api/rules", dependencies=[Auth])
async def list_rules(s: Session) -> dict:
    rows = (await s.scalars(select(PolicyRule).order_by(PolicyRule.id))).all()
    return {
        "rules": [_rule_out(r) for r in rows],
        "redactors": [{"id": r.id, "label": r.label} for r in BUILTIN.values()],
    }


@app.post("/api/rules/preview", dependencies=[Auth])
async def preview_rule(body: RuleIn, s: Session, rule_id: int | None = None) -> dict:
    """Dry run: what saving this rule would do to the messages already stored."""
    _check(body)
    candidate = Rule(rule_id or -1, body.name, body.matchers, body.outcomes, body.enabled)
    return _stats(await reapply(s, dry_run=True, preview=candidate))


@app.post("/api/rules", dependencies=[Auth])
async def create_rule(body: RuleIn, s: Session) -> dict:
    _check(body)
    r = PolicyRule(name=body.name.strip(), enabled=body.enabled, matchers=body.matchers,
                   outcomes=body.outcomes)
    s.add(r)
    await s.flush()
    stats = await reapply(s)
    await s.commit()
    return {"rule": _rule_out(r), "applied": _stats(stats)}


@app.put("/api/rules/{rule_id}", dependencies=[Auth])
async def update_rule(rule_id: int, body: RuleIn, s: Session) -> dict:
    _check(body)
    r = await s.get(PolicyRule, rule_id)
    if r is None:
        raise HTTPException(404)
    r.name, r.enabled, r.matchers, r.outcomes = (body.name.strip(), body.enabled, body.matchers,
                                                 body.outcomes)
    await s.flush()
    stats = await reapply(s)
    await s.commit()
    await s.refresh(r)
    return {"rule": _rule_out(r), "applied": _stats(stats)}


@app.delete("/api/rules/{rule_id}", dependencies=[Auth])
async def delete_rule(rule_id: int, s: Session) -> dict:
    r = await s.get(PolicyRule, rule_id)
    if r is None:
        raise HTTPException(404)
    await s.delete(r)
    await s.flush()
    stats = await reapply(s)  # what was hidden or redacted only by this rule becomes visible
    await s.commit()
    return {"applied": _stats(stats)}


class DefaultsIn(BaseModel):
    visibility: str


@app.get("/api/policy/defaults", dependencies=[Auth])
async def get_defaults(s: Session) -> dict:
    return {"visibility": (await load_defaults(s)).visibility}


@app.put("/api/policy/defaults", dependencies=[Auth])
async def put_defaults(body: DefaultsIn, s: Session) -> dict:
    try:
        await save_defaults(s, body.visibility)
    except ValueError as e:
        raise HTTPException(422, {"errors": ["Unknown agent access choice."]}) from e
    stats = await reapply(s)
    await s.commit()
    return {"visibility": body.visibility, "applied": _stats(stats)}


@app.get("/api/accounts", dependencies=[Auth])
async def list_accounts(s: Session) -> dict:
    rows = (await s.scalars(select(Account).order_by(Account.source, Account.name))).all()
    return {"accounts": [{"id": a.id, "source": a.source, "name": a.name,
                          "external_id": a.external_id} for a in rows]}


@app.get("/api/lookup", dependencies=[Auth])
async def lookup(s: Session, conversation_ids: Annotated[list[int] | None, Query()] = None,
                 senders: Annotated[list[str] | None, Query()] = None) -> dict:
    """Readable names for the ids stored in rule conditions."""
    convs = (await s.scalars(select(Conversation).where(
        Conversation.id.in_(conversation_ids or [])))).all()
    idents = (await s.scalars(select(Identity).where(
        Identity.external_id.in_(senders or [])))).all()
    return {"conversations": {c.id: c.title for c in convs},
            "senders": {i.external_id: i.display_name for i in idents}}


# --- Web UI (built React app) ---
_dist = settings().web_dist
if (_dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404)
        return FileResponse(_dist / "index.html")
