"""Sanitize any text that came out of an LLM (or from a message) before it reaches a channel.

The model reads attacker-controlled messages, so its output is untrusted too. A notification
must not be able to carry data out (a link or image that a client fetches automatically), ping
people (@everyone), or dress itself up with formatting. So: no URLs, no markdown, no HTML, no
mentions, no invisible characters, bounded length.
"""

import re
import unicodedata

_URL = re.compile(r"(?i)\b(?:https?|ftp|file|data|javascript|mailto|discord)\s*:\s*\S+|\bwww\.\S+")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML = re.compile(r"<[^>\n]{0,200}>")
_DISCORD_MENTION = re.compile(r"<(?:@[!&]?|#|/[^:>]*:)\d+>")
_EVERYONE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
_MARKDOWN = re.compile(r"([\\*_~`|>#\[\]()])")
_SPACES = re.compile(r"\s+")
# Zero-width and bidirectional control characters can hide or reorder text.
_INVISIBLE = {"Cf", "Cc", "Co", "Cs"}


def plain(text: str | None, max_len: int = 200) -> str:
    """Single-line plain text: safe to show anywhere, including Discord."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(" " if unicodedata.category(c) in _INVISIBLE else c for c in text)
    text = _MD_IMAGE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _DISCORD_MENTION.sub("", text)
    text = _URL.sub("[link removed]", text)
    text = _HTML.sub("", text)
    text = _EVERYONE.sub(r"\1", text)
    text = text.replace("@", "＠")  # no mention can survive, whatever its form
    text = _SPACES.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def discord_escape(text: str) -> str:
    """Escape markdown so plain text is displayed literally by Discord."""
    return _MARKDOWN.sub(r"\\\1", text)
