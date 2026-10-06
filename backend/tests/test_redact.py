import pytest

from majordome.policy.redact import (
    DEFAULT_REDACTORS,
    redact,
    redact_access_links,
    redact_card,
    redact_iban,
    redact_otp,
    redact_password,
)


@pytest.mark.parametrize("text,expected", [
    ("Votre code de vérification est 482913.", "Votre code de vérification est [OTP_CODE]."),
    ("Your verification code is: 123-456", "Your verification code is: [OTP_CODE]"),
    ("G-583920 est votre code de validation Google.",
     "[OTP_CODE] est votre code de validation Google."),
    ("123456 is your Instagram code", "[OTP_CODE] is your Instagram code"),
    ("Code de connexion WhatsApp : 731 204", "Code de connexion WhatsApp : [OTP_CODE]"),
    ("Votre code à usage unique : 9031", "Votre code à usage unique : [OTP_CODE]"),
    ("Use OTP 88213945 to confirm", "Use OTP [OTP_CODE] to confirm"),
])
def test_otp_codes_are_redacted(text, expected):
    assert redact_otp(text) == expected


@pytest.mark.parametrize("text", [
    "Le cycle Coton 40 est terminé.",
    "RDV le 12/10 à 14h30, salle 2045",
    "Votre colis 6A1234567 sera livré demain.",
    "Code postal 75011 Paris",
    "Votre code promo 2024 est actif",
    "Numéro de suivi : 88123456",
])
def test_ordinary_numbers_are_kept(text):
    assert redact_otp(text) == text


def test_cards_are_luhn_checked():
    assert redact_card("Carte 4970 1012 3456 7893 expirée") == "Carte [CARD_NUMBER] expirée"
    assert redact_card("card 4111-1111-1111-1111") == "card [CARD_NUMBER]"
    # 16 digits failing Luhn (e.g. an order number) are kept.
    assert redact_card("Commande 1234 5678 9012 3456") == "Commande 1234 5678 9012 3456"


def test_iban_is_mod97_checked():
    assert redact_iban("IBAN : FR76 3000 6000 0112 3456 7890 189 merci") == "IBAN : [IBAN] merci"
    assert redact_iban("DE89370400440532013000") == "[IBAN]"
    assert redact_iban("FR76 3000 6000 0112 3456 7890 188") == "FR76 3000 6000 0112 3456 7890 188"


def test_passwords():
    assert (redact_password("Mot de passe provisoire : Xk!92ab")
            == "Mot de passe provisoire : [PASSWORD]")
    assert redact_password("password=hunter2 then log in") == "password=[PASSWORD] then log in"
    assert redact_password("Change your password soon") == "Change your password soon"


def test_access_links():
    text = ("Réinitialisez ici https://acme.com/reset?token=abc123 ou visitez "
            "https://acme.com/blog/news")
    assert redact_access_links(text) == ("Réinitialisez ici [SENSITIVE_LINK] ou visitez "
                                         "https://acme.com/blog/news")
    assert "[SENSITIVE_LINK]" in redact_access_links("https://x.io/magic-login/eyJhbGci")


def test_combined_and_custom_patterns():
    text = "Code 482913 — carte 4111 1111 1111 1111 — dossier ABC-77"
    out = redact(text, DEFAULT_REDACTORS, (r"ABC-\d+",))
    assert out == "Code [OTP_CODE] — carte [CARD_NUMBER] — dossier [REDACTED]"
    assert redact(out, DEFAULT_REDACTORS, (r"ABC-\d+",)) == out  # idempotent
    assert redact("x", frozenset(), ("(",)) == "x"  # invalid pattern never crashes
    assert redact(None, DEFAULT_REDACTORS) is None
