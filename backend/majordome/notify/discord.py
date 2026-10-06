"""Discord webhook channel (push only). The destination is fixed by the user, never the agent."""

import re
from pathlib import Path

import httpx

WEBHOOK = re.compile(r"^https://(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")
SUPPRESS_EMBEDS = 1 << 2


def load_webhook(path: Path) -> str | None:
    if not path.exists():
        return None
    url = path.read_text().strip()
    return url if WEBHOOK.match(url) else None


async def send(url: str, content: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Post `content` as-is (already sanitized). Returns "sent" or an error description."""
    payload = {
        "content": content[:1900],
        "username": "Majordome",
        "allowed_mentions": {"parse": []},  # never ping anyone
        "flags": SUPPRESS_EMBEDS,  # never unfurl links into previews
    }
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as http:
            r = await http.post(url, json=payload)
    except httpx.HTTPError as e:
        return f"error: {type(e).__name__}"
    return "sent" if r.status_code in (200, 204) else f"error: HTTP {r.status_code}"
