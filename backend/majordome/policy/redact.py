"""Deterministic redactors (FR + EN). The agent only ever sees text that went through these.

Each redactor replaces a sensitive span with a stable placeholder such as `[OTP_CODE]`, so the
agent still understands what kind of message it is ("your code is [OTP_CODE]").
"""

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Redactor:
    id: str
    label: str
    apply: Callable[[str], str]


# --- One-time codes --------------------------------------------------------------------------

_OTP_KEYWORDS = (
    r"code|otp|passcode|pin|verification|vérification|verify|sécurité|security|"
    r"confirmation|connexion|login|sign[- ]?in|2fa|authenticat\w*|authentification|"
    r"usage unique|one[- ]time|token"
)
# A keyword, then (within a short distance on the same line) a 4-8 digit code, possibly split.
_OTP_AFTER = re.compile(
    rf"(?i)(\b(?:{_OTP_KEYWORDS})\b[^\n\d]{{0,40}}?)(\b[A-Z]{{0,2}}-?\d{{3,4}}[ -]?\d{{0,4}}\b)"
)
# The code first: "123456 is your verification code", "G-123456 est votre code".
_OTP_BEFORE = re.compile(
    rf"(?i)(\b[A-Z]{{0,2}}-?\d{{3,4}}[ -]?\d{{0,4}}\b)([^\n\d]{{0,30}}\b(?:{_OTP_KEYWORDS})\b)"
)


_NOT_OTP = re.compile(r"(?i)postal|zip|promo|client|dossier|commande|order|suivi|tracking")


def _otp_ok(code: str, context: str) -> bool:
    digits = re.sub(r"\D", "", code)
    return 4 <= len(digits) <= 8 and not _NOT_OTP.search(context)


def redact_otp(text: str) -> str:
    def after(m: re.Match[str]) -> str:
        return m.group(1) + "[OTP_CODE]" if _otp_ok(m.group(2), m.group(1)) else m.group(0)

    def before(m: re.Match[str]) -> str:
        return "[OTP_CODE]" + m.group(2) if _otp_ok(m.group(1), m.group(2)) else m.group(0)

    return _OTP_BEFORE.sub(before, _OTP_AFTER.sub(after, text))


# --- Payment card numbers (Luhn-checked) -----------------------------------------------------

_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total, alt = total + n, not alt
    return total % 10 == 0


def redact_card(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[CARD_NUMBER]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    return _CARD.sub(sub, text)


# --- IBAN (mod-97 checked) -------------------------------------------------------------------

_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")


def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    rearranged = s[4:] + s[:4]
    number = "".join(str(int(c, 36)) for c in rearranged)
    return int(number) % 97 == 1


def redact_iban(text: str) -> str:
    return _IBAN.sub(lambda m: "[IBAN]" if _iban_ok(m.group(0)) else m.group(0), text)


# --- Passwords given in clear ----------------------------------------------------------------

_PASSWORD = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|mot de passe|mdp|temporary password|"
    r"mot de passe provisoire|mot de passe temporaire)\b\s*[:=]\s*)(\S+)"
)


def redact_password(text: str) -> str:
    return _PASSWORD.sub(lambda m: m.group(1) + "[PASSWORD]", text)


# --- Links that grant access (reset, magic login, verification) ------------------------------

_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_SENSITIVE_URL = re.compile(
    r"(?i)(reset|r[ée]initiali|password|passwd|magic|verify|verif|confirm|activate|"
    r"login|signin|sign-in|auth|token=|code=|key=|otp|unsubscribe\?.*token)"
)


def redact_access_links(text: str) -> str:
    return _URL.sub(
        lambda m: "[SENSITIVE_LINK]" if _SENSITIVE_URL.search(m.group(0)) else m.group(0), text
    )


BUILTIN: dict[str, Redactor] = {
    r.id: r
    for r in [
        Redactor("otp", "One-time codes", redact_otp),
        Redactor("card", "Card numbers", redact_card),
        Redactor("iban", "Bank account numbers (IBAN)", redact_iban),
        Redactor("password", "Passwords written in messages", redact_password),
        Redactor("access_links", "Login, reset and verification links", redact_access_links),
    ]
}
DEFAULT_REDACTORS = frozenset(BUILTIN)


def redact(text: str | None, redactors: frozenset[str],
           patterns: tuple[str, ...] = ()) -> str | None:
    if text is None:
        return None
    for rid in sorted(redactors):  # deterministic order
        if rid in BUILTIN:
            text = BUILTIN[rid].apply(text)
    for p in patterns:
        try:
            text = re.sub(p, "[REDACTED]", text, flags=re.IGNORECASE)
        except re.error:
            continue  # invalid custom patterns are rejected at save time; never crash ingestion
    return text
