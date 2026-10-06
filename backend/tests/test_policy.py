from hypothesis import given, settings
from hypothesis import strategies as st

from majordome.policy.engine import (
    STORAGE,
    VISIBILITY,
    Decision,
    Facts,
    Rule,
    agent_text,
    fold,
    matches,
    resolve,
    validate,
)

MAIL = Facts(source="email", account_id=1, conversation_id=10, conversation_kind="email_thread",
             sender="alerts@secure.mybank.fr", subject="Votre relevé",
             body="Votre code de vérification est 482913.", has_attachment=True)
CHAT = Facts(source="whatsapp", account_id=2, conversation_id=20, conversation_kind="group",
             sender="@whatsapp_lid-1:x", subject=None, body="Promo été -50% !",
             has_attachment=False)


def rule(i=1, name="r", enabled=True, **kw):
    matchers = {k: v for k, v in kw.items() if k not in
                {"storage", "visibility", "redact", "redact_patterns", "retention_days"}}
    outcomes = {k: v for k, v in kw.items() if k not in matchers}
    return Rule(i, name, matchers, outcomes, enabled)


def test_matchers():
    assert matches(rule(sources=["email"], visibility="never"), MAIL)
    assert not matches(rule(sources=["email"], visibility="never"), CHAT)
    assert matches(rule(sender_domains=["mybank.fr"], visibility="never"), MAIL)  # subdomain
    assert not matches(rule(sender_domains=["bank.fr"], visibility="never"), MAIL)
    assert matches(rule(keywords=["PROMO ETE"], storage="drop"), CHAT)  # case/accent folded
    assert matches(rule(conversation_kinds=["group"], sources=["whatsapp"], storage="drop"), CHAT)
    assert not matches(rule(conversation_kinds=["dm"], sources=["whatsapp"], storage="drop"), CHAT)
    assert matches(rule(has_attachment=True, visibility="on_request"), MAIL)
    assert not matches(rule(has_attachment=True, visibility="on_request"), CHAT)
    assert matches(rule(regex=r"relev[ée]", visibility="never"), MAIL)
    assert not matches(rule(enabled=False, sources=["email"], visibility="never"), MAIL)
    # Matrix ghost ids are not email addresses: no domain.
    assert not matches(rule(sender_domains=["x"], visibility="never"), CHAT)


def test_strictest_outcome_wins_per_dimension():
    rules = [
        rule(1, "bank", sender_domains=["mybank.fr"], visibility="on_request_approval",
             retention_days=365),
        rule(2, "all mail", sources=["email"], visibility="on_request", retention_days=30,
             redact_patterns=[r"relevé"]),
        rule(3, "groups", conversation_kinds=["group"], storage="drop"),
    ]
    d = resolve(rules, MAIL)
    assert d.visibility == "on_request_approval" and d.retention_days == 30
    assert d.storage == "store" and [n for _, n in d.matched] == ["bank", "all mail"]
    assert agent_text(d, "relevé 482913 code de vérification 482913").count("[REDACTED]") == 1
    assert resolve(rules, CHAT).storage == "drop"


def test_default_is_a_fallback_for_visibility_only():
    defaults = Decision(visibility="never", retention_days=90)
    assert resolve([], MAIL, defaults).visibility == "never"
    allow = rule(1, "mail", sources=["email"], visibility="always", retention_days=365)
    d = resolve([allow], MAIL, defaults)
    assert d.visibility == "always" and d.visibility_from_rule
    assert d.retention_days == 90  # storage/retention: defaults still count, strictest wins


def test_agent_text_respects_visibility_and_storage():
    assert agent_text(Decision(visibility="never"), "hello") is None
    assert agent_text(Decision(storage="metadata_only"), "hello") is None
    assert agent_text(Decision(), "Votre code est 482913") == "Votre code est [OTP_CODE]"


def test_validation():
    assert validate({}, {"visibility": "never"})  # no condition: would match everything
    assert validate({"sources": ["email"]}, {})  # no outcome
    assert validate({"sources": ["email"]}, {"visibility": "public"})
    assert validate({"regex": "("}, {"visibility": "never"})
    assert validate({"sources": ["email"]}, {"retention_days": 0})
    assert validate({"bogus": 1}, {"visibility": "never"})
    assert validate({"sources": ["email"]}, {"visibility": "never", "retention_days": 7}) == []


def test_fold():
    assert fold("ÉTÉ Ça") == "ete ca"


# --- Property: adding a rule can never make a message more exposed --------------------------

facts_st = st.builds(
    Facts,
    source=st.sampled_from(["email", "whatsapp", "discord"]),
    account_id=st.integers(1, 3),
    conversation_id=st.integers(1, 5),
    conversation_kind=st.sampled_from(["dm", "group", "email_thread"]),
    sender=st.sampled_from([None, "a@x.com", "b@y.org", "@whatsapp_1:x"]),
    subject=st.one_of(st.none(), st.text(max_size=20)),
    body=st.one_of(st.none(), st.text(max_size=40)),
    has_attachment=st.booleans(),
)
rule_st = st.builds(
    lambda i, src, kinds, kw, att, storage, vis, days, enabled: Rule(
        i, f"r{i}",
        {k: v for k, v in {"sources": src, "conversation_kinds": kinds, "keywords": kw,
                           "has_attachment": att}.items() if v not in (None, [])},
        {k: v for k, v in {"storage": storage, "visibility": vis,
                           "retention_days": days}.items() if v is not None},
        enabled),
    st.integers(1, 1000),
    st.lists(st.sampled_from(["email", "whatsapp", "discord"]), max_size=2),
    st.lists(st.sampled_from(["dm", "group", "email_thread"]), max_size=2),
    st.lists(st.sampled_from(["a", "promo", "é"]), max_size=2),
    st.one_of(st.none(), st.booleans()),
    st.one_of(st.none(), st.sampled_from(STORAGE)),
    st.one_of(st.none(), st.sampled_from(VISIBILITY)),
    st.one_of(st.none(), st.integers(1, 400)),
    st.booleans(),
)


@settings(max_examples=400, deadline=None)
@given(st.lists(rule_st, max_size=5), rule_st, facts_st, st.sampled_from(VISIBILITY))
def test_adding_a_rule_never_exposes_more(rules, extra, facts, default_vis):
    defaults = Decision(visibility=default_vis)
    before = resolve(rules, facts, defaults)
    after = resolve([*rules, extra], facts, defaults)
    assert STORAGE.index(after.storage) >= STORAGE.index(before.storage)
    # Visibility: the default is a fallback (an allow rule may open access where only the
    # default applied), but once rules decide, a new rule can only make it stricter.
    if before.visibility_from_rule or not extra.outcomes.get("visibility"):
        assert VISIBILITY.index(after.visibility) >= VISIBILITY.index(before.visibility)
    else:
        assert after.visibility == extra.outcomes["visibility"] or not matches(extra, facts)
    assert after.redactors >= before.redactors
    assert set(after.patterns) >= set(before.patterns)
    if before.retention_days is not None:
        assert after.retention_days is not None and after.retention_days <= before.retention_days


@settings(max_examples=200, deadline=None)
@given(st.lists(rule_st, max_size=5), facts_st)
def test_rule_order_does_not_matter(rules, facts):
    a, b = resolve(rules, facts), resolve(list(reversed(rules)), facts)
    assert (a.storage, a.visibility, a.redactors, a.retention_days) == \
           (b.storage, b.visibility, b.redactors, b.retention_days)
