"""Built-in gateway heartbeat: probes, alert cadence, and delivery gating.

The service exists so a degraded Hermes constellation (dead adapter, missing
dashboard backend, silently-failing nightly sync) reports itself to the chat
surface the user actually watches, instead of rotting in log files — the
observed production failure was a nightly sync that had been failing for
weeks with nobody told.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from gateway.heartbeat import (
    HeartbeatConfig,
    HeartbeatService,
    HealthReport,
    ProbeResult,
    parse_channel,
    probe_dashboard,
    probe_gateway,
    probe_nightly_sync,
    write_dashboard_portfile,
)


# ── config parsing ─────────────────────────────────────────────────────────


def test_config_defaults_to_disabled():
    assert HeartbeatConfig.from_config(None).enabled is False
    assert HeartbeatConfig.from_config({}).enabled is False
    assert HeartbeatConfig.from_config({"heartbeat": "on"}).enabled is False


def test_config_parses_block():
    cfg = HeartbeatConfig.from_config({
        "heartbeat": {
            "enabled": True,
            "channel": "telegram:-100999",
            "check_interval_minutes": 2,
            "heartbeat_hours": 12,
            "realert_hours": 3,
            "nightly_max_age_hours": 48,
        }
    })
    assert cfg.enabled is True
    assert cfg.channel == "telegram:-100999"
    assert cfg.check_interval_s == 120.0
    assert cfg.heartbeat_hours == 12.0
    assert cfg.realert_hours == 3.0
    assert cfg.nightly_max_age_hours == 48.0


def test_config_survives_garbage_numbers():
    cfg = HeartbeatConfig.from_config({
        "heartbeat": {"enabled": True, "check_interval_minutes": "soon"}
    })
    assert cfg.check_interval_s == 300.0  # default 5 min


def test_parse_channel():
    assert parse_channel("telegram:-100123") == ("telegram", "-100123")
    assert parse_channel("Discord: 42") == ("discord", "42")
    assert parse_channel("") is None
    assert parse_channel("telegram") is None
    assert parse_channel(":123") is None


# ── probes ─────────────────────────────────────────────────────────────────


def test_probe_gateway_reports_adapters():
    r = probe_gateway(lambda: {"telegram": object(), "discord": object()})
    assert r.healthy is True and "telegram" in r.detail


def test_probe_gateway_unhealthy_with_no_adapters():
    assert probe_gateway(lambda: {}).healthy is False


def test_probe_gateway_survives_provider_crash():
    r = probe_gateway(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert r.healthy is False and "boom" in r.detail


def test_probe_dashboard_missing_portfile(tmp_path):
    r = probe_dashboard(tmp_path / "nope.json")
    assert r.healthy is False and "portfile" in r.detail


def test_probe_dashboard_dead_pid(tmp_path):
    pf = tmp_path / "dashboard.portfile.json"
    # PID 1 is alive but unkillable-by-us (healthy path is separately tested
    # against a real server); use an absurd dead PID here.
    pf.write_text(json.dumps({"port": 1, "pid": 99999999}))
    r = probe_dashboard(pf)
    assert r.healthy is False and "gone" in r.detail


def test_probe_dashboard_happy_path_against_real_http(tmp_path):
    import http.server

    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        pf = write_dashboard_portfile(port, hermes_home=tmp_path)
        assert pf is not None and pf.exists()
        r = probe_dashboard(pf)
        assert r.healthy is True, r.detail
        assert str(port) in r.detail
    finally:
        server.shutdown()


def test_portfile_records_pid_and_port(tmp_path):
    pf = write_dashboard_portfile(4242, hermes_home=tmp_path)
    data = json.loads(pf.read_text())
    assert data["port"] == 4242
    assert data["pid"] == os.getpid()


def _nightly(tmp_path, status, detail="", age_hours=1.0):
    f = tmp_path / "last-run.json"
    finished = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    f.write_text(json.dumps({
        "status": status, "detail": detail, "finished_at": finished.isoformat(),
    }))
    return f


def test_probe_nightly_ok_statuses(tmp_path):
    for status in ("up_to_date", "staged", "skipped", "awaiting_pr"):
        r = probe_nightly_sync(_nightly(tmp_path, status), max_age_hours=30)
        assert r.healthy is True, (status, r.detail)


def test_probe_nightly_failed_is_unhealthy(tmp_path):
    f = _nightly(tmp_path, "failed", detail="merge conflicts unresolved (31 files)")
    r = probe_nightly_sync(f, max_age_hours=30)
    assert r.healthy is False
    assert "merge conflicts" in r.detail


def test_probe_nightly_stale_is_unhealthy(tmp_path):
    f = _nightly(tmp_path, "up_to_date", age_hours=50)
    r = probe_nightly_sync(f, max_age_hours=30)
    assert r.healthy is False and "stale" in r.detail


def test_probe_nightly_missing_file(tmp_path):
    r = probe_nightly_sync(tmp_path / "absent.json", max_age_hours=30)
    assert r.healthy is False


# ── alert cadence ──────────────────────────────────────────────────────────


class _Harness:
    """HeartbeatService with injected probes and a captured send channel."""

    def __init__(self, tmp_path, **cfg):
        base = {"enabled": True, "channel": "telegram:-1", "heartbeat_hours": 24,
                "realert_hours": 6}
        base.update(cfg)
        self.sent: list[tuple[str, str]] = []
        self.healthy = True
        self.svc = HeartbeatService(
            HeartbeatConfig.from_config({"heartbeat": base}),
            send=lambda ch, msg: self.sent.append((ch, msg)) or True,
            hermes_home=tmp_path,
        )
        # Deterministic collect: one probe controlled by self.healthy.
        self.svc.collect = lambda: HealthReport(
            results=[ProbeResult("gateway", self.healthy, "test")]
        )


def test_alert_on_transition_and_realert_cadence(tmp_path):
    h = _Harness(tmp_path)
    t0 = time.time()
    assert h.svc.tick(now=t0) is None            # healthy, no beat due
    h.healthy = False
    assert h.svc.tick(now=t0 + 60) == "alert"     # transition → immediate
    assert h.svc.tick(now=t0 + 120) is None       # still broken, within cooldown
    assert h.svc.tick(now=t0 + 7 * 3600) == "alert"   # realert after 6h
    h.healthy = True
    assert h.svc.tick(now=t0 + 8 * 3600) == "recovery"
    assert h.svc.tick(now=t0 + 8 * 3600 + 60) is None


def test_periodic_heartbeat_when_all_healthy(tmp_path):
    h = _Harness(tmp_path, heartbeat_hours=1)
    t0 = time.time()
    h.svc.tick(now=t0, force_status=True)         # on-start beat
    assert [k for k, _ in [("status", "")]]       # (sent below)
    assert h.svc.tick(now=t0 + 1800) is None      # 30 min — not due
    assert h.svc.tick(now=t0 + 3700) == "heartbeat"


def test_on_start_beat_reports_current_state(tmp_path):
    h = _Harness(tmp_path)
    assert h.svc.tick(force_status=True) == "status"
    h2 = _Harness(tmp_path)
    h2.healthy = False
    assert h2.svc.tick(force_status=True) == "alert"


def test_failed_send_retries_next_tick(tmp_path):
    h = _Harness(tmp_path)
    h.svc._send = lambda ch, msg: False           # delivery down
    h.healthy = False
    t0 = time.time()
    assert h.svc.tick(now=t0) is None             # send failed → not recorded
    h.svc._send = lambda ch, msg: True
    # Next tick alerts even though no transition happened and no realert time
    # passed — the failed send left cadence state untouched.
    assert h.svc.tick(now=t0 + 60) == "alert"


def test_render_marks_unhealthy_lines():
    report = HealthReport(results=[
        ProbeResult("gateway", True, "adapters: telegram"),
        ProbeResult("nightly_sync", False, "last run failed: conflicts"),
    ])
    text = report.render("alert")
    assert "ALERT" in text
    assert "✅ gateway" in text
    assert "❌ nightly_sync" in text


def test_service_refuses_to_start_without_channel(tmp_path):
    svc = HeartbeatService(
        HeartbeatConfig.from_config({"heartbeat": {"enabled": True}}),
        send=lambda ch, msg: True,
        hermes_home=tmp_path,
    )
    assert svc.start() is False


def test_service_disabled_by_default(tmp_path):
    svc = HeartbeatService(HeartbeatConfig(), send=lambda ch, msg: True, hermes_home=tmp_path)
    assert svc.start() is False
