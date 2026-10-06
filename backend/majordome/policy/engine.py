"""Policy engine: deterministic rules deciding storage, agent visibility, redaction, retention.

Plain code, no LLM. Every dimension is resolved independently and the most restrictive outcome
among matching rules wins, so adding a rule can never expose more than before (property-tested).
Agents never create or modify rules: rules come from the user through the UI/API only.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from majordome.policy.redact import DEFAULT_REDACTORS, redact

STORAGE = ("store", "metadata_only", "drop")  # least -> most restrictive
VISIBILITY = ("always", "on_request", "on_request_approval", "never")

MATCHER_KEYS = {
    "sources", "accounts", "conversations", "conversation_kinds", "senders", "sender_domains",
    "keywords", "regex", "has_attachment",
}
OUTCOME_KEYS = {"storage", "visibility", "redact", "redact_patterns", "retention_days"}


@dataclass(frozen=True)
class Facts:
    """What rules can look at, for one message."""

    source: str
    account_id: int
    conversation_id: int
    conversation_kind: str
    sender: str | None  # identity external id: email address, matrix ghost id, ...
    subject: str | None
    body: str | None
    has_attachment: bool

    @property
    def sender_domain(self) -> str | None:
        if self.sender and "@" in self.sender and not self.sender.startswith("@"):
            return self.sender.rsplit("@", 1)[1].lower()
        return None


@dataclass(frozen=True)
class Rule:
    id: int
    name: str
    matchers: dict[str, Any]
    outcomes: dict[str, Any]
    enabled: bool = True


@dataclass(frozen=True)
class Decision:
    storage: str = "store"
    visibility: str = "always"
    redactors: frozenset[str] = DEFAULT_REDACTORS
    patterns: tuple[str, ...] = ()
    retention_days: int | None = None  # None: keep forever
    matched: tuple[tuple[int, str], ...] = field(default_factory=tuple)

    def explain(self) -> dict[str, Any]:
        return {
            "storage": self.storage,
            "visibility": self.visibility,
            "redactors": sorted(self.redactors),
            "patterns": list(self.patterns),
            "retention_days": self.retention_days,
            "matched_rules": [{"id": i, "name": n} for i, n in self.matched],
        }


def fold(text: str) -> str:
    """Case- and accent-insensitive form: 'Été' -> 'ete'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def validate(matchers: dict[str, Any], outcomes: dict[str, Any]) -> list[str]:
    """Return human-readable problems; empty list means the rule is valid."""
    errors = []
    if unknown := set(matchers) - MATCHER_KEYS:
        errors.append(f"Unknown conditions: {', '.join(sorted(unknown))}")
    if unknown := set(outcomes) - OUTCOME_KEYS:
        errors.append(f"Unknown outcomes: {', '.join(sorted(unknown))}")
    if not any(v not in (None, [], "") for v in matchers.values()):
        errors.append("Add at least one condition, otherwise the rule would match everything.")
    if not any(v not in (None, [], "") for v in outcomes.values()):
        errors.append("Choose at least one thing the rule changes.")
    if outcomes.get("storage") not in (None, *STORAGE):
        errors.append("Unknown storage choice.")
    if outcomes.get("visibility") not in (None, *VISIBILITY):
        errors.append("Unknown agent access choice.")
    days = outcomes.get("retention_days")
    if days is not None and (not isinstance(days, int) or days < 1):
        errors.append("Retention must be a whole number of days, at least 1.")
    for p in [matchers.get("regex"), *outcomes.get("redact_patterns", [])]:
        if p:
            try:
                re.compile(p)
            except re.error as e:
                errors.append(f"Invalid pattern {p!r}: {e}")
    return errors


def matches(rule: Rule, f: Facts) -> bool:
    m = rule.matchers
    if not rule.enabled:
        return False
    if m.get("sources") and f.source not in m["sources"]:
        return False
    if m.get("accounts") and f.account_id not in m["accounts"]:
        return False
    if m.get("conversations") and f.conversation_id not in m["conversations"]:
        return False
    if m.get("conversation_kinds") and f.conversation_kind not in m["conversation_kinds"]:
        return False
    if m.get("senders") and (f.sender or "").lower() not in {s.lower() for s in m["senders"]}:
        return False
    if m.get("sender_domains"):
        dom = f.sender_domain
        wanted = {d.lower().lstrip("@") for d in m["sender_domains"]}
        if not dom or not any(dom == w or dom.endswith("." + w) for w in wanted):
            return False
    if m.get("has_attachment") is not None and f.has_attachment != m["has_attachment"]:
        return False
    text = f"{f.subject or ''}\n{f.body or ''}"
    if m.get("keywords"):
        folded = fold(text)
        if not any(fold(k) in folded for k in m["keywords"] if k):
            return False
    if m.get("regex"):
        try:
            if not re.search(m["regex"], text, re.IGNORECASE):
                return False
        except re.error:
            return False
    return True


def _strictest(order: tuple[str, ...], values: list[str], default: str) -> str:
    return max([default, *values], key=order.index)


def resolve(rules: list[Rule], f: Facts, defaults: Decision | None = None) -> Decision:
    base = defaults or Decision()
    hits = [r for r in rules if matches(r, f)]
    days = [r.outcomes["retention_days"] for r in hits if r.outcomes.get("retention_days")]
    if base.retention_days:
        days.append(base.retention_days)
    return Decision(
        storage=_strictest(STORAGE, [r.outcomes["storage"] for r in hits
                                     if r.outcomes.get("storage")], base.storage),
        visibility=_strictest(VISIBILITY, [r.outcomes["visibility"] for r in hits
                                           if r.outcomes.get("visibility")], base.visibility),
        redactors=base.redactors.union(*(frozenset(r.outcomes.get("redact", [])) for r in hits)),
        patterns=tuple(dict.fromkeys(
            [*base.patterns, *(p for r in hits for p in r.outcomes.get("redact_patterns", []))])),
        retention_days=min(days) if days else None,
        matched=tuple((r.id, r.name) for r in hits),
    )


def agent_text(decision: Decision, text: str | None) -> str | None:
    """The only version of a text an LLM may see (None when the agent may never see it)."""
    if decision.visibility == "never" or decision.storage != "store":
        return None
    return redact(text, decision.redactors, decision.patterns)
