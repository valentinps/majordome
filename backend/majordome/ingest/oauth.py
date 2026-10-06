"""OAuth2 for Microsoft personal accounts (Outlook.com no longer accepts IMAP passwords).

Uses the device-code flow with the user's own Entra app registration (public client).
Tokens are stored in `oauth_token_dir/<address>.json` (mode 600). Only the worker reads them.
"""

import json
import os
import time
from pathlib import Path

import httpx

from majordome.config import Settings

AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPES = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


def _token_path(cfg: Settings, address: str) -> Path:
    return cfg.oauth_token_dir / f"{address.lower()}.json"


def _save(path: Path, tokens: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def device_login(cfg: Settings, address: str, client_id: str) -> None:
    """Interactive: prints a code to enter at microsoft.com/devicelogin, then stores tokens."""
    r = httpx.post(f"{AUTHORITY}/devicecode", data={"client_id": client_id, "scope": SCOPES})
    r.raise_for_status()
    flow = r.json()
    print(flow["message"])
    deadline = time.time() + flow["expires_in"]
    while time.time() < deadline:
        time.sleep(flow.get("interval", 5))
        t = httpx.post(f"{AUTHORITY}/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id, "device_code": flow["device_code"],
        })
        body = t.json()
        if t.status_code == 200:
            body["expires_at"] = time.time() + body["expires_in"]
            _save(_token_path(cfg, address), body)
            print(f"Stored tokens for {address}.")
            return
        if body.get("error") not in ("authorization_pending", "slow_down"):
            raise SystemExit(f"login failed: {body.get('error_description', body)}")
    raise SystemExit("login timed out")


def microsoft_access_token(cfg: Settings, address: str, client_id: str | None) -> str:
    path = _token_path(cfg, address)
    if not path.exists() or not client_id:
        raise RuntimeError(f"no OAuth tokens for {address}; run `majordome email-auth {address}`")
    tokens = json.loads(path.read_text())
    if tokens.get("expires_at", 0) - 120 > time.time():
        return tokens["access_token"]
    r = httpx.post(f"{AUTHORITY}/token", data={
        "grant_type": "refresh_token", "client_id": client_id,
        "refresh_token": tokens["refresh_token"], "scope": SCOPES,
    })
    r.raise_for_status()
    new = r.json()
    new.setdefault("refresh_token", tokens["refresh_token"])
    new["expires_at"] = time.time() + new["expires_in"]
    _save(path, new)
    return new["access_token"]
