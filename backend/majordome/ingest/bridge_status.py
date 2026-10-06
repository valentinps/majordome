"""Bridge connection state, reported by the mautrix bridges themselves.

Each bridge POSTs its state to `homeserver.status_endpoint` (configured to point at the worker):
whether it is connected to the network, reconnecting, or logged out. That is the only reliable
health signal for chat apps: a quiet chat says nothing about whether the bridge still works.
"""

from datetime import UTC, datetime
from typing import Any

OK = {"CONNECTED", "BACKFILLING"}
WARN = {"CONNECTING", "TRANSIENT_DISCONNECT", "STARTING"}
# Anything else (BAD_CREDENTIALS, LOGGED_OUT, UNKNOWN_ERROR, UNCONFIGURED, ...) needs the user.

PLAIN = {
    "CONNECTED": "Connected",
    "BACKFILLING": "Connected, catching up",
    "CONNECTING": "Connecting",
    "STARTING": "Starting",
    "TRANSIENT_DISCONNECT": "Reconnecting",
    "BAD_CREDENTIALS": "Logged out: log in again",
    "LOGGED_OUT": "Logged out: log in again",
    "UNCONFIGURED": "No account linked",
    "UNKNOWN_ERROR": "Error",
    "BRIDGE_UNREACHABLE": "Bridge unreachable",
}


def collect_states(payload: Any) -> list[dict[str, Any]]:
    """Every bridge-state object in a status payload (flat or nested global-state shapes)."""
    found: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("state_event"), str):
            found.append(payload)
        for v in payload.values():
            if isinstance(v, (dict, list)):
                found.extend(collect_states(v))
    elif isinstance(payload, list):
        for v in payload:
            found.extend(collect_states(v))
    return found


def merge(stored: dict[str, Any], payload: Any) -> dict[str, Any]:
    """Merge newly reported states into what we know, keyed by bridge-level vs. login."""
    states = dict(stored.get("states", {}))
    for st in collect_states(payload):
        key = f"remote:{st['remote_id']}" if st.get("remote_id") else "bridge"
        states[key] = {k: st.get(k) for k in
                       ("state_event", "error", "message", "timestamp", "ttl", "remote_name")}
    return {"states": states, "received_at": datetime.now(UTC).isoformat()}


def summarize(stored: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """One status for the UI: ok / warn / error / unknown, with a plain-language label.

    Login (remote) states win over the bridge-level state: what matters is whether the user's
    account is connected. A state older than its TTL is no longer trusted.
    """
    now = now or datetime.now(UTC)
    states = stored.get("states", {})
    remote = [v for k, v in states.items() if k.startswith("remote:")]
    candidates = remote or ([states["bridge"]] if "bridge" in states else [])
    if not candidates:
        return {"status": "unknown", "label": "No report from the bridge yet"}

    def level(st: dict[str, Any]) -> int:
        ts, ttl = st.get("timestamp"), st.get("ttl")
        if ts and ttl and now.timestamp() > ts + ttl:
            return 1  # stale report
        ev = st.get("state_event")
        return 0 if ev in OK else 1 if ev in WARN else 2

    worst = max(candidates, key=level)
    lvl = level(worst)
    ev = worst.get("state_event") or "UNKNOWN_ERROR"
    label = PLAIN.get(ev, ev.replace("_", " ").capitalize())
    if lvl == 1 and ev in OK:
        label = "No recent report from the bridge"
    return {"status": ("ok", "warn", "error")[lvl], "label": label,
            "detail": worst.get("message") or worst.get("error"),
            "since": worst.get("timestamp")}
