from datetime import UTC, datetime

from majordome.ingest.bridge_status import collect_states, merge, summarize

NOW = datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
T = int(NOW.timestamp())


def test_flat_and_nested_payloads():
    flat = {"state_event": "CONNECTED", "remote_id": "336", "timestamp": T, "ttl": 3600}
    nested = {"remoteState": {"336": {"state_event": "BAD_CREDENTIALS", "remote_id": "336"}},
              "bridgeState": {"state_event": "RUNNING", "source": "bridge"}}
    assert [s["state_event"] for s in collect_states(flat)] == ["CONNECTED"]
    found = sorted(s["state_event"] for s in collect_states(nested))
    assert found == ["BAD_CREDENTIALS", "RUNNING"]


def test_login_state_wins_and_latest_report_replaces():
    st = merge({}, {"state_event": "UNCONFIGURED", "source": "bridge", "timestamp": T,
                    "ttl": 21600})
    assert summarize(st, NOW)["status"] == "error"  # no login yet
    st = merge(st, {"state_event": "CONNECTED", "remote_id": "336", "timestamp": T, "ttl": 3600})
    assert summarize(st, NOW) == {"status": "ok", "label": "Connected", "detail": None, "since": T}
    st = merge(st, {"state_event": "BAD_CREDENTIALS", "remote_id": "336", "timestamp": T,
                    "ttl": 3600, "message": "Logged out from phone"})
    s = summarize(st, NOW)
    assert s["status"] == "error" and s["detail"] == "Logged out from phone"


def test_stale_and_missing_reports():
    st = merge({}, {"state_event": "CONNECTED", "remote_id": "1", "timestamp": T - 7200,
                    "ttl": 3600})
    assert summarize(st, NOW) == {"status": "warn", "label": "No recent report from the bridge",
                                  "detail": None, "since": T - 7200}
    assert summarize({}, NOW)["status"] == "unknown"
