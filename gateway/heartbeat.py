"""Built-in health heartbeat for the Hermes gateway.

The gateway is the always-on Hermes process, which makes it the right home for
self-monitoring: when something in the constellation degrades — a platform
adapter dies, the desktop/Ace dashboard backend disappears, the nightly
upstream sync starts failing — the place the user actually notices is their
chat surface. So the gateway watches, and *tells them there*, instead of
rotting silently until someone greps a log.

What it watches (each probe individually config-gated):

``gateway``
    The live platform adapters (``runner.adapters``): which are attached, and
    that at least one is. The heartbeat running at all already proves the
    gateway process, its config, and its outbound HTTP path.

``dashboard``
    The desktop/Ace dashboard backend. The dashboard writes a portfile at
    startup (:func:`write_dashboard_portfile`, called by ``hermes_cli.web_server``)
    recording its port + PID; the probe checks the PID is alive and the HTTP
    root answers. Ports change on every respawn, so the portfile — not config —
    is the source of truth.

``nightly_sync``
    The nightly upstream-sync pipeline's ``last-run.json`` (written by
    ``hermes-nightly-apply``/``-sync``). Unhealthy when the last run failed or
    when no run has finished within ``nightly_max_age_hours`` (the job is
    supposed to fire daily — silence is also a failure).

Alerting policy: a transition to unhealthy sends immediately; while a problem
persists it re-alerts every ``realert_hours``; recovery sends once; and an
"all healthy" beat goes out every ``heartbeat_hours`` so silence is
distinguishable from a dead monitor. Delivery reuses the standalone platform
send path (``tools.send_message_tool._send_to_platform``) — the same one cron
uses — so a beat can still get out when a live adapter object is wedged.

Config (``config.yaml``)::

    heartbeat:
      enabled: true
      channel: "telegram:-100123456"     # platform:chat_id — REQUIRED
      check_interval_minutes: 5
      heartbeat_hours: 24                # periodic all-good beat; 0 disables
      realert_hours: 6                   # re-alert cadence while unhealthy
      on_start: true                     # send a status beat at gateway start
      dashboard_portfile: ""             # override; default <hermes_home>/dashboard.portfile.json
      nightly_status_file: ""            # override; default <hermes_home>/logs/nightly-sync/last-run.json
      nightly_max_age_hours: 30
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Nightly-sync last-run.json statuses that mean "the pipeline is doing its
# job" (see automation hermes-nightly-sync.sh write_status callers). Anything
# else — notably "failed" — is unhealthy.
NIGHTLY_OK_STATUSES = frozenset({
    "ok", "success", "up_to_date", "staged", "applied", "skipped", "awaiting_pr",
})

PORTFILE_NAME = "dashboard.portfile.json"


def _default_hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def write_dashboard_portfile(port: int, *, hermes_home: Optional[Path] = None) -> Optional[Path]:
    """Record the dashboard backend's port + PID for out-of-process probes.

    Called by ``hermes_cli.web_server`` once the HTTP server is bound. The
    dashboard's port changes on every respawn (the desktop asks for an
    ephemeral port), so anything that wants to health-check it — the gateway
    heartbeat here, external tooling — needs a discovery file, not a config
    entry. Best-effort: monitoring must never break serving.
    """
    try:
        home = Path(hermes_home) if hermes_home is not None else _default_hermes_home()
        path = home / PORTFILE_NAME
        payload = {"port": int(port), "pid": os.getpid(), "written_at": time.time()}
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path
    except Exception:
        logger.debug("dashboard portfile write failed", exc_info=True)
        return None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # No signal-0 probe on Windows; report alive and let the HTTP probe
        # (the real health signal) decide.
        return True
    try:
        os.kill(pid, 0)  # windows-footgun: ok — gated on os.name above
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


@dataclass
class ProbeResult:
    name: str
    healthy: bool
    detail: str


@dataclass
class HeartbeatConfig:
    enabled: bool = False
    channel: str = ""
    check_interval_s: float = 300.0
    heartbeat_hours: float = 24.0
    realert_hours: float = 6.0
    on_start: bool = True
    dashboard_portfile: Optional[str] = None
    nightly_status_file: Optional[str] = None
    nightly_max_age_hours: float = 30.0

    @classmethod
    def from_config(cls, cfg: dict | None) -> "HeartbeatConfig":
        block = (cfg or {}).get("heartbeat")
        if not isinstance(block, dict):
            return cls()

        def _num(key: str, default: float, minimum: float = 0.0) -> float:
            try:
                return max(minimum, float(block.get(key, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=bool(block.get("enabled")),
            channel=str(block.get("channel") or ""),
            check_interval_s=_num("check_interval_minutes", 5.0, 1.0) * 60.0,
            heartbeat_hours=_num("heartbeat_hours", 24.0),
            realert_hours=_num("realert_hours", 6.0, 0.25),
            on_start=bool(block.get("on_start", True)),
            dashboard_portfile=str(block.get("dashboard_portfile") or "") or None,
            nightly_status_file=str(block.get("nightly_status_file") or "") or None,
            nightly_max_age_hours=_num("nightly_max_age_hours", 30.0, 1.0),
        )


# ── probes ──────────────────────────────────────────────────────────────────


def probe_gateway(adapters_provider: Callable[[], dict] | None) -> ProbeResult:
    """The gateway's own adapter surface. Running here proves the process."""
    if adapters_provider is None:
        return ProbeResult("gateway", True, "process up (no adapter introspection)")
    try:
        adapters = adapters_provider() or {}
    except Exception as exc:
        return ProbeResult("gateway", False, f"adapter introspection failed: {exc}")
    names = sorted(str(k) for k in adapters)
    if not names:
        return ProbeResult("gateway", False, "no platform adapters attached")
    return ProbeResult("gateway", True, f"adapters: {', '.join(names)}")


def probe_dashboard(portfile: Path, *, http_timeout: float = 4.0) -> ProbeResult:
    """The desktop/Ace dashboard backend, via its startup portfile."""
    if not portfile.exists():
        return ProbeResult(
            "dashboard", False, f"no portfile at {portfile} (backend never started?)"
        )
    try:
        data = json.loads(portfile.read_text(encoding="utf-8"))
        port = int(data["port"])
        pid = int(data.get("pid") or 0)
    except Exception as exc:
        return ProbeResult("dashboard", False, f"portfile unreadable: {exc}")
    if pid and not _pid_alive(pid):
        return ProbeResult("dashboard", False, f"backend pid {pid} is gone (port {port})")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/", headers={"User-Agent": "hermes-heartbeat"}
        )
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            code = resp.getcode()
    except Exception as exc:
        return ProbeResult(
            "dashboard", False, f"port {port} not answering: {type(exc).__name__}: {exc}"
        )
    if code != 200:
        return ProbeResult("dashboard", False, f"port {port} answered HTTP {code}")
    return ProbeResult("dashboard", True, f"pid {pid} answering on port {port}")


def probe_nightly_sync(status_file: Path, *, max_age_hours: float, now: float | None = None) -> ProbeResult:
    """The nightly upstream-sync pipeline, via its last-run.json."""
    now = time.time() if now is None else now
    if not status_file.exists():
        return ProbeResult(
            "nightly_sync", False, f"no status file at {status_file} (job never ran?)"
        )
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return ProbeResult("nightly_sync", False, f"status file unreadable: {exc}")
    status = str(data.get("status") or "unknown")
    detail = str(data.get("detail") or "")

    age_h: Optional[float] = None
    finished = data.get("finished_at")
    if finished:
        try:
            from datetime import datetime

            age_h = (now - datetime.fromisoformat(str(finished)).timestamp()) / 3600.0
        except Exception:
            age_h = None
    if age_h is None:
        try:
            age_h = (now - status_file.stat().st_mtime) / 3600.0
        except Exception:
            age_h = 0.0

    if status not in NIGHTLY_OK_STATUSES:
        return ProbeResult(
            "nightly_sync",
            False,
            f"last run {status}: {detail or 'no detail'} ({age_h:.0f}h ago)",
        )
    if age_h > max_age_hours:
        return ProbeResult(
            "nightly_sync",
            False,
            f"stale: last run ({status}) finished {age_h:.0f}h ago (limit {max_age_hours:.0f}h)",
        )
    return ProbeResult("nightly_sync", True, f"{status} ({age_h:.0f}h ago): {detail}")


# ── report + delivery ───────────────────────────────────────────────────────


@dataclass
class HealthReport:
    results: list[ProbeResult] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(r.healthy for r in self.results)

    @property
    def unhealthy_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.results if not r.healthy)

    def render(self, kind: str) -> str:
        icon = {"alert": "🔴", "recovery": "🟢", "heartbeat": "💓", "status": "ℹ️"}.get(kind, "ℹ️")
        title = {
            "alert": "Hermes health ALERT",
            "recovery": "Hermes recovered",
            "heartbeat": "Hermes heartbeat — all healthy",
            "status": "Hermes status",
        }.get(kind, "Hermes status")
        lines = [f"{icon} {title}"]
        for r in self.results:
            lines.append(f"{'✅' if r.healthy else '❌'} {r.name}: {r.detail}")
        return "\n".join(lines)


def parse_channel(raw: str) -> Optional[tuple[str, str]]:
    """``"telegram:-100123"`` → ``("telegram", "-100123")``."""
    if not raw or ":" not in raw:
        return None
    platform, _, chat_id = raw.partition(":")
    platform, chat_id = platform.strip().lower(), chat_id.strip()
    if not platform or not chat_id:
        return None
    return platform, chat_id


def _send_via_platform(channel: str, message: str) -> bool:
    """Deliver one heartbeat message through the standalone platform sender."""
    parsed = parse_channel(channel)
    if parsed is None:
        logger.warning("heartbeat: unusable channel %r", channel)
        return False
    platform_name, chat_id = parsed
    try:
        from gateway.config import Platform, load_gateway_config
        from tools.send_message_tool import _send_to_platform

        platform = Platform(platform_name)
        pconfig = load_gateway_config().platforms.get(platform)
        if pconfig is None:
            logger.warning("heartbeat: platform %s not configured", platform_name)
            return False
        asyncio.run(_send_to_platform(platform, pconfig, chat_id, message))
        return True
    except Exception:
        logger.warning("heartbeat send failed channel=%s", channel, exc_info=True)
        return False


# ── service ─────────────────────────────────────────────────────────────────


class HeartbeatService:
    """Threaded monitor loop; owns alert/recovery/beat cadence state."""

    def __init__(
        self,
        cfg: HeartbeatConfig,
        *,
        adapters_provider: Callable[[], dict] | None = None,
        send: Callable[[str, str], bool] | None = None,
        hermes_home: Optional[Path] = None,
    ) -> None:
        self.cfg = cfg
        self._adapters_provider = adapters_provider
        self._send = send or _send_via_platform
        self._home = Path(hermes_home) if hermes_home is not None else _default_hermes_home()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._was_healthy: Optional[bool] = None
        self._last_alert_at = 0.0
        # Anchor the periodic-beat clock at construction: the first "all
        # healthy" beat is due one full period from startup (the on_start
        # status message covers announcing ourselves at boot).
        self._last_beat_at = time.time()

    # paths -----------------------------------------------------------------

    def _dashboard_portfile(self) -> Path:
        if self.cfg.dashboard_portfile:
            return Path(os.path.expanduser(self.cfg.dashboard_portfile))
        return self._home / PORTFILE_NAME

    def _nightly_status_file(self) -> Path:
        if self.cfg.nightly_status_file:
            return Path(os.path.expanduser(self.cfg.nightly_status_file))
        return self._home / "logs" / "nightly-sync" / "last-run.json"

    # core ------------------------------------------------------------------

    def collect(self) -> HealthReport:
        report = HealthReport()
        report.results.append(probe_gateway(self._adapters_provider))
        report.results.append(probe_dashboard(self._dashboard_portfile()))
        report.results.append(
            probe_nightly_sync(
                self._nightly_status_file(),
                max_age_hours=self.cfg.nightly_max_age_hours,
            )
        )
        return report

    def tick(self, *, force_status: bool = False, now: float | None = None) -> Optional[str]:
        """One evaluation. Returns the message kind sent, or None.

        ``force_status`` (used for the on-start beat) always sends the current
        state so a fresh gateway announces itself once.
        """
        now = time.time() if now is None else now
        report = self.collect()
        kind: Optional[str] = None

        if force_status:
            kind = "status" if report.healthy else "alert"
        elif not report.healthy:
            transitioned = self._was_healthy in (True, None)
            realert_due = (now - self._last_alert_at) >= self.cfg.realert_hours * 3600.0
            if transitioned or realert_due:
                kind = "alert"
        elif self._was_healthy is False:
            kind = "recovery"
        elif (
            self.cfg.heartbeat_hours > 0
            and (now - self._last_beat_at) >= self.cfg.heartbeat_hours * 3600.0
        ):
            kind = "heartbeat"

        if kind is not None:
            sent = self._send(self.cfg.channel, report.render(kind))
            if sent:
                if kind in ("alert",):
                    self._last_alert_at = now
                # Any successful outbound message doubles as the liveness beat.
                self._last_beat_at = now
            else:
                # Failed sends must not swallow the alert: leave cadence state
                # untouched so the next tick retries.
                kind = None
        self._was_healthy = report.healthy
        if not report.healthy:
            logger.warning(
                "heartbeat: unhealthy (%s)", ", ".join(report.unhealthy_names)
            )
        return kind

    # lifecycle -------------------------------------------------------------

    def start(self) -> bool:
        if not self.cfg.enabled:
            return False
        if not parse_channel(self.cfg.channel):
            logger.warning(
                "heartbeat enabled but channel %r is not 'platform:chat_id' — not starting",
                self.cfg.channel,
            )
            return False

        def _loop() -> None:
            if self.cfg.on_start:
                try:
                    self.tick(force_status=True)
                except Exception:
                    logger.debug("heartbeat on-start tick failed", exc_info=True)
            while not self._stop.wait(self.cfg.check_interval_s):
                try:
                    self.tick()
                except Exception:
                    logger.debug("heartbeat tick failed", exc_info=True)

        self._thread = threading.Thread(target=_loop, daemon=True, name="gateway-heartbeat")
        self._thread.start()
        logger.info(
            "heartbeat started: channel=%s interval=%.0fs beat_every=%.0fh",
            self.cfg.channel, self.cfg.check_interval_s, self.cfg.heartbeat_hours,
        )
        return True

    def stop(self) -> None:
        self._stop.set()


def start_heartbeat_service(
    cfg: dict | None,
    *,
    adapters_provider: Callable[[], dict] | None = None,
) -> Optional[HeartbeatService]:
    """Gateway entry point: build from user config and start if enabled."""
    service = HeartbeatService(
        HeartbeatConfig.from_config(cfg), adapters_provider=adapters_provider
    )
    return service if service.start() else None
