import pytest

from majordome.agent.sanitize import discord_escape, plain


@pytest.mark.parametrize("text,forbidden", [
    ("see ![x](https://evil.example/?d=secret)", ["https", "evil.example", "!["]),
    ("click [here](https://evil.example/login)", ["https", "evil.example", "]("]),
    ("visit www.evil.example/path", ["www.evil"]),
    ("javascript:alert(1)", ["javascript:"]),
    ("ping @everyone and @here", ["@everyone", "@here"]),
    ("<@123456789> <@&42> <#987>", ["<@", "<#"]),
    ("<img src=x onerror=alert(1)>", ["<img"]),
])
def test_dangerous_content_removed(text, forbidden):
    out = plain(text)
    for f in forbidden:
        assert f not in out


def test_invisible_and_bidi_characters_are_removed():
    assert plain("a​b‮c⁦d⁩e\x07f") == "a b c d e f"


def test_length_and_whitespace():
    assert plain("x" * 500, 50) == "x" * 49 + "…"
    assert plain("  many\n\n  lines\t here ") == "many lines here"
    assert plain(None) == ""


def test_discord_escape():
    assert discord_escape("**bold** `code` _i_ > quote") == (
        "\\*\\*bold\\*\\* \\`code\\` \\_i\\_ \\> quote")
