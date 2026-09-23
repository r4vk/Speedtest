from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import api_quality, api_quality_exports, quality_db, retention
from .api_quality import ExpectedMark
from .api_retention import router as retention_router
from .config import AppConfig
from .coverage import SessionTracker, clip_to_observed, coverage, observed_intervals
from .csv_utils import csv_response as _csv_response
from .db import (
    TimeRange,
    ensure_db,
    ensure_default_setting,
    get_connectivity_period,
    get_current_connectivity_period,
    get_last_speed_test,
    get_last_success_speed_test,
    integrity_quick_check,
    mark_connectivity_period_expected_by_id,
    query_connectivity_periods,
    query_connectivity_checks,
    query_speed_tests,
    set_setting,
    setting_was_set_by_user,
    get_settings,
)
from .network_tools import _validate_hostname
from .probe_types import ProbeTarget
from .quality_engine import QualityEngine
from .quality_settings import (
    QUALITY_SETTING_SPECS,
    ensure_quality_defaults,
    read_quality_settings,
    serialize_setting,
)
from .runtime import get_runtime, init_runtime
from .scheduler import RunningState, run_speedtest_once, speedtest_loop
from .telemetry import active_heartbeat_loop, send_startup_event
from .time_utils import parse_dt, parse_range, to_iso_z, to_local_display, to_local_iso, utc_now


log = logging.getLogger(__name__)

DEFAULT_SPEEDTEST_MODE = "speedtest.net"
SESSION_HEARTBEAT_SECONDS = 30.0

cfg = AppConfig()
#: Measuring device of this instance (spec §3: `devices` is seeded with 'nas').
DEVICE_ID = cfg.device_id
ensure_db(cfg.db_path)

ensure_default_setting(cfg.db_path, "connect_target", cfg.connect_target)
ensure_default_setting(cfg.db_path, "connect_interval_seconds", str(cfg.connect_interval_seconds))
ensure_default_setting(cfg.db_path, "speedtest_mode", DEFAULT_SPEEDTEST_MODE)
ensure_default_setting(cfg.db_path, "speedtest_url", cfg.speedtest_url or "")
ensure_default_setting(cfg.db_path, "speedtest_interval_seconds", str(cfg.speedtest_interval_seconds))
ensure_default_setting(cfg.db_path, "speedtest_duration_seconds", str(cfg.speedtest_duration_seconds))
ensure_default_setting(cfg.db_path, "connectivity_check_buffer_seconds", str(cfg.connectivity_check_buffer_seconds))
ensure_default_setting(cfg.db_path, "connectivity_check_buffer_max", str(cfg.connectivity_check_buffer_max))
ensure_default_setting(cfg.db_path, "ping_timeout_ms", str(cfg.ping_timeout_ms))
ensure_default_setting(cfg.db_path, "ping_enabled", "true")
ensure_default_setting(cfg.db_path, "speed_enabled", "true")
ensure_default_setting(cfg.db_path, "ping_schedules", "[]")
ensure_default_setting(cfg.db_path, "speed_schedules", "[]")
ensure_default_setting(cfg.db_path, "telemetry_enabled", "true" if cfg.telemetry_default_enabled else "false")
def _env_gateway_host() -> str:
    """`GATEWAY_HOST`, validated — an unusable value is dropped with a warning.

    The variable reaches the `gateway` probe target *and* the `gateway_host`
    setting the diagnostics trace to, so it must not enter the database
    unchecked: a value starting with `-` would become an mtr flag rather than
    a host (review finding I7).
    """
    raw = (os.getenv("GATEWAY_HOST") or "").strip()
    if not raw:
        return ""
    try:
        return _validate_hostname(raw)
    except ValueError as exc:
        log.warning("GATEWAY_HOST=%r is unusable (%s); the gateway stays unconfigured", raw, exc)
        return ""


# Quality settings (spec §7, §8, §10, §11, §14) come from one table, so every
# reader of a key sees the same default.
ensure_quality_defaults(cfg.db_path, {"gateway_host": _env_gateway_host()})
_telemetry_install_id = get_settings(cfg.db_path, ["telemetry_install_id"]).get("telemetry_install_id", "").strip()
if not _telemetry_install_id:
    set_setting(cfg.db_path, "telemetry_install_id", uuid.uuid4().hex, now_iso=to_iso_z(utc_now()))

def _read_version() -> str:
    """Odczytaj wersję z pliku VERSION osadzonego w aplikacji."""
    version_file = Path(__file__).resolve().parent / "VERSION"
    try:
        return version_file.read_text().strip()
    except FileNotFoundError:
        return "dev"

APP_VERSION = _read_version()


def _retention_settings() -> retention.RetentionSettings:
    values = get_settings(cfg.db_path, list(retention.RETENTION_SETTINGS_KEYS))
    return retention.RetentionSettings.from_settings(values)


def _retention_targets() -> list[ProbeTarget]:
    return quality_db.list_targets(cfg.db_path)


def _heartbeat_once(tracker: SessionTracker) -> bool:
    """One heartbeat. A failure costs one heartbeat, never the whole loop."""
    try:
        tracker.heartbeat(to_iso_z(utc_now()))
        return True
    except Exception:
        log.warning("Monitor session heartbeat failed", exc_info=True)
        return False


async def _session_heartbeat_loop(
    tracker: SessionTracker,
    state: RunningState,
    interval_seconds: float = SESSION_HEARTBEAT_SECONDS,
) -> None:
    """Keep `last_seen_at` fresh so that coverage knows the monitor was alive."""
    while not state.stop.is_set():
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=interval_seconds)
            return
        except asyncio.TimeoutError:
            pass
        _heartbeat_once(tracker)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the monitor with the app and stop it before the app goes away."""
    init_runtime()
    integrity = integrity_quick_check(cfg.db_path)
    if integrity != "ok":
        log.warning("SQLite integrity check reported problems: %s", integrity)
    _reconcile_gateway_host_from_env()
    state = RunningState(stop=asyncio.Event())
    app.state.running_state = state
    tracker = SessionTracker()
    tracker.start(cfg.db_path, DEVICE_ID, APP_VERSION, to_iso_z(utc_now()))
    app.state.session_tracker = tracker
    engine = QualityEngine(cfg, app_version=APP_VERSION, device_id=DEVICE_ID)
    app.state.quality_engine = engine
    try:
        await engine.start()
        app.state.tasks = [
            asyncio.create_task(_session_heartbeat_loop(tracker, state)),
            asyncio.create_task(speedtest_loop(cfg, state)),
            asyncio.create_task(
                send_startup_event(
                    db_path=cfg.db_path,
                    app_version=APP_VERSION,
                    default_enabled=cfg.telemetry_default_enabled,
                    timeout_seconds=cfg.telemetry_timeout_seconds,
                )
            ),
            asyncio.create_task(
                active_heartbeat_loop(
                    db_path=cfg.db_path,
                    app_version=APP_VERSION,
                    default_enabled=cfg.telemetry_default_enabled,
                    timeout_seconds=cfg.telemetry_timeout_seconds,
                )
            ),
            asyncio.create_task(
                retention.retention_loop(cfg.db_path, _retention_settings, _retention_targets)
            ),
        ]
        yield
    finally:
        state.stop.set()
        try:
            await engine.stop()
        except Exception:
            # The session still has to be closed cleanly, or the next start
            # would report this run as `unclean` and lose its coverage.
            log.exception("Stopping the quality engine failed")
        tasks = getattr(app.state, "tasks", [])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tracker.stop(to_iso_z(utc_now()))


app = FastAPI(title="Speedtest Monitor", version=APP_VERSION, lifespan=lifespan)
app.include_router(retention_router)
#: The quality router resolves the database through the app, not through a
#: module-level import, so tests can point a fresh app at a temporary file.
app.state.db_path = cfg.db_path
app.include_router(api_quality.router)
app.include_router(api_quality_exports.router)
_BASE_DIR = Path(__file__).resolve().parent.parent
_STATIC_DIR = _BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(_STATIC_DIR / "index.html"))


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/api/version")
def api_version():
    return {"version": APP_VERSION}


def _quality_status() -> dict[str, Any]:
    """Minimal quality block for `/api/status`; the full one is `/api/quality/status`."""
    engine: QualityEngine | None = getattr(app.state, "quality_engine", None)
    if engine is None:
        return {"availability": "no_data", "quality": "unknown", "icmp_method": "unknown"}
    status = engine.status()
    return {key: status[key] for key in ("availability", "quality", "icmp_method")}


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    current = get_current_connectivity_period(cfg.db_path)
    last_speed = get_last_speed_test(cfg.db_path)
    last_speed_ok = get_last_success_speed_test(cfg.db_path)
    eff = _effective_config()
    rt = get_runtime()

    if current:
        current = dict(current)
        current["started_at"] = to_local_iso(parse_dt(current["started_at"]))
        if current.get("ended_at"):
            current["ended_at"] = to_local_iso(parse_dt(current["ended_at"]))
    if last_speed:
        last_speed = dict(last_speed)
        last_speed["started_at"] = to_local_iso(parse_dt(last_speed["started_at"]))
    if last_speed_ok:
        last_speed_ok = dict(last_speed_ok)
        last_speed_ok["started_at"] = to_local_iso(parse_dt(last_speed_ok["started_at"]))

    return {
        "now": to_local_iso(utc_now()),
        "connectivity": current,
        "last_speed_test": last_speed,
        "last_speed_test_ok": last_speed_ok,
        "speedtest_running": bool(rt.running),
        "speedtest_running_since": to_local_iso(parse_dt(rt.running_since_iso)) if rt.running_since_iso else None,
        "quality": _quality_status(),
        "config": {
            "connect_target": eff.connect_target,
            "connect_interval_seconds": eff.connect_interval_seconds,
            "speedtest_mode": eff.speedtest_mode,
            "speedtest_interval_seconds": eff.speedtest_interval_seconds,
            "speedtest_duration_seconds": eff.speedtest_duration_seconds,
            "speedtest_skip_if_offline": cfg.speedtest_skip_if_offline,
            "speedtest_url_configured": bool(eff.speedtest_url.strip()),
            "connectivity_check_buffer_seconds": eff.connectivity_check_buffer_seconds,
            "connectivity_check_buffer_max": eff.connectivity_check_buffer_max,
            "ping_timeout_ms": eff.ping_timeout_ms,
        },
    }


class ConfigResponse(BaseModel):
    connect_target: str
    connect_interval_seconds: float
    speedtest_mode: str
    speedtest_url: str
    speedtest_interval_seconds: float
    speedtest_duration_seconds: float
    connectivity_check_buffer_seconds: float
    connectivity_check_buffer_max: int
    ping_timeout_ms: int
    ping_enabled: bool
    speed_enabled: bool
    ping_schedules: str
    speed_schedules: str
    telemetry_enabled: bool
    telemetry_endpoint_configured: bool

    # Quality monitoring (spec §7, §8, §10, §11, §14); defaults and types live
    # in `quality_settings.QUALITY_SETTING_SPECS`.
    incident_window_seconds: int
    incident_min_samples: int
    incident_loss_pct_threshold: float
    incident_outage_loss_pct: float
    incident_rtt_p95_ms_threshold: float
    incident_fail_streak_threshold: int
    incident_open_windows: int
    incident_stabilization_seconds: int
    incident_no_data_close_seconds: int
    availability_eval_seconds: int
    availability_window_seconds: int
    load_test_enabled: bool
    load_test_interval_seconds: int
    load_test_kind: str
    load_test_server: str
    load_test_port: int
    load_test_udp_bitrate: str
    load_test_duration_seconds: int
    load_test_datagram_len: int
    load_test_directions: str
    diagnostics_enabled: bool
    diagnostics_min_interval_seconds: int
    diagnostics_max_per_incident: int
    diagnostics_max_concurrent: int
    diagnostics_mtr_count: int
    diagnostics_mtr_timeout_seconds: int
    retention_raw_days: int
    retention_aggregate_days: int
    retention_incident_days: int
    retention_load_test_raw_days: int
    retention_diagnostics_days: int
    diagnostic_mode: bool
    gateway_host: str


class ConfigUpdate(BaseModel):
    connect_target: str | None = Field(default=None, min_length=1, max_length=1024)
    connect_interval_seconds: float | None = Field(default=None, gt=0.1, le=3600)
    speedtest_mode: str | None = Field(default=None)
    speedtest_url: str | None = Field(default=None, max_length=2048)
    speedtest_interval_seconds: float | None = Field(default=None, gt=1, le=7 * 24 * 3600)
    speedtest_duration_seconds: float | None = Field(default=None, ge=5, le=120)
    connectivity_check_buffer_seconds: float | None = Field(default=None, ge=0, le=24 * 3600)
    connectivity_check_buffer_max: int | None = Field(default=None, ge=1, le=100000)
    ping_timeout_ms: int | None = Field(default=None, ge=50, le=60000)
    ping_enabled: bool | None = Field(default=None)
    speed_enabled: bool | None = Field(default=None)
    ping_schedules: str | None = Field(default=None, max_length=8192)
    speed_schedules: str | None = Field(default=None, max_length=8192)
    telemetry_enabled: bool | None = Field(default=None)

    # Quality monitoring. Every bound is a guard against a setting that would
    # silently disable the measurement or flood the link.
    incident_window_seconds: int | None = Field(default=None, ge=5, le=300)
    incident_min_samples: int | None = Field(default=None, ge=1, le=1000)
    incident_loss_pct_threshold: float | None = Field(default=None, ge=0, le=100)
    incident_outage_loss_pct: float | None = Field(default=None, ge=0, le=100)
    incident_rtt_p95_ms_threshold: float | None = Field(default=None, ge=1, le=10000)
    incident_fail_streak_threshold: int | None = Field(default=None, ge=1, le=1000)
    incident_open_windows: int | None = Field(default=None, ge=1, le=100)
    incident_stabilization_seconds: int | None = Field(default=None, ge=0, le=86400)
    incident_no_data_close_seconds: int | None = Field(default=None, ge=0, le=86400)
    availability_eval_seconds: int | None = Field(default=None, ge=1, le=60)
    availability_window_seconds: int | None = Field(default=None, ge=5, le=300)
    load_test_enabled: bool | None = Field(default=None)
    load_test_interval_seconds: int | None = Field(default=None, ge=60, le=604800)
    load_test_kind: Literal["iperf_udp", "iperf_tcp"] | None = Field(default=None)
    load_test_server: str | None = Field(default=None, max_length=253)
    load_test_port: int | None = Field(default=None, ge=1, le=65535)
    load_test_udp_bitrate: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?[KMG]?$")
    load_test_duration_seconds: int | None = Field(default=None, ge=1, le=60)
    load_test_datagram_len: int | None = Field(default=None, ge=64, le=65507)
    load_test_directions: Literal["upload", "download", "both"] | None = Field(default=None)
    diagnostics_enabled: bool | None = Field(default=None)
    diagnostics_min_interval_seconds: int | None = Field(default=None, ge=30, le=86400)
    diagnostics_max_per_incident: int | None = Field(default=None, ge=1, le=20)
    # More than a handful of concurrent mtr runs would itself load the link
    # the diagnostics are trying to describe (spec §11).
    diagnostics_max_concurrent: int | None = Field(default=None, ge=1, le=4)
    diagnostics_mtr_count: int | None = Field(default=None, ge=1, le=100)
    diagnostics_mtr_timeout_seconds: int | None = Field(default=None, ge=10, le=600)
    retention_raw_days: int | None = Field(default=None, ge=1, le=3650)
    retention_aggregate_days: int | None = Field(default=None, ge=1, le=3650)
    retention_incident_days: int | None = Field(default=None, ge=1, le=3650)
    retention_load_test_raw_days: int | None = Field(default=None, ge=1, le=3650)
    retention_diagnostics_days: int | None = Field(default=None, ge=1, le=3650)
    diagnostic_mode: bool | None = Field(default=None)
    gateway_host: str | None = Field(default=None, max_length=253)


def _effective_config() -> ConfigResponse:
    values = get_settings(
        cfg.db_path,
        [
            "connect_target",
            "connect_interval_seconds",
            "speedtest_mode",
            "speedtest_url",
            "speedtest_interval_seconds",
            "speedtest_duration_seconds",
            "connectivity_check_buffer_seconds",
            "connectivity_check_buffer_max",
            "ping_timeout_ms",
            "ping_enabled",
            "speed_enabled",
            "ping_schedules",
            "speed_schedules",
            "telemetry_enabled",
        ],
    )
    connect_target = values.get("connect_target", cfg.connect_target)
    try:
        connect_interval = float(values.get("connect_interval_seconds", str(cfg.connect_interval_seconds)))
    except ValueError:
        connect_interval = cfg.connect_interval_seconds

    speedtest_mode = (values.get("speedtest_mode") or DEFAULT_SPEEDTEST_MODE).strip()
    if speedtest_mode not in {"url", "speedtest.net", "speedtest.pl"}:
        speedtest_mode = DEFAULT_SPEEDTEST_MODE

    speedtest_url = values.get("speedtest_url", cfg.speedtest_url or "")
    try:
        speedtest_interval = float(values.get("speedtest_interval_seconds", str(cfg.speedtest_interval_seconds)))
    except ValueError:
        speedtest_interval = cfg.speedtest_interval_seconds

    try:
        speedtest_duration = float(values.get("speedtest_duration_seconds", str(cfg.speedtest_duration_seconds)))
    except ValueError:
        speedtest_duration = cfg.speedtest_duration_seconds

    try:
        buffer_seconds = float(
            values.get("connectivity_check_buffer_seconds", str(cfg.connectivity_check_buffer_seconds))
        )
    except ValueError:
        buffer_seconds = cfg.connectivity_check_buffer_seconds

    try:
        buffer_max = int(values.get("connectivity_check_buffer_max", str(cfg.connectivity_check_buffer_max)))
    except ValueError:
        buffer_max = cfg.connectivity_check_buffer_max

    try:
        ping_timeout_ms = int(values.get("ping_timeout_ms", str(cfg.ping_timeout_ms)))
    except ValueError:
        ping_timeout_ms = cfg.ping_timeout_ms

    ping_enabled = values.get("ping_enabled", "true").lower() == "true"
    speed_enabled = values.get("speed_enabled", "true").lower() == "true"
    ping_schedules = values.get("ping_schedules", "[]")
    speed_schedules = values.get("speed_schedules", "[]")
    telemetry_enabled = values.get(
        "telemetry_enabled",
        "true" if cfg.telemetry_default_enabled else "false",
    ).lower() == "true"

    quality = read_quality_settings(cfg.db_path)
    return ConfigResponse(
        **{key: value for key, value in quality.items() if key in ConfigResponse.model_fields},
        connect_target=connect_target,
        connect_interval_seconds=connect_interval,
        speedtest_mode=speedtest_mode,
        speedtest_url=speedtest_url,
        speedtest_interval_seconds=speedtest_interval,
        speedtest_duration_seconds=speedtest_duration,
        connectivity_check_buffer_seconds=buffer_seconds,
        connectivity_check_buffer_max=buffer_max,
        ping_timeout_ms=ping_timeout_ms,
        ping_enabled=ping_enabled,
        speed_enabled=speed_enabled,
        ping_schedules=ping_schedules,
        speed_schedules=speed_schedules,
        telemetry_enabled=telemetry_enabled,
        telemetry_endpoint_configured=True,
    )


def _reconcile_gateway_host_from_env() -> None:
    """Let `GATEWAY_HOST` reach the gateway target on *every* start (§3.1).

    The env used to be read only by `db._seed_probe_targets`, which returns
    early once `probe_targets` has a row, and by `ensure_default_setting`,
    which is a no-op once the key exists. An operator who upgraded, saw the
    gateway target disabled, added `GATEWAY_HOST` to `docker-compose.yml` and
    restarted therefore got nothing at all (review finding I3).

    A host configured through the UI or the API always wins: the env only
    fills a value nobody has ever set, so restarting never undoes a change the
    operator made in the panel — including *clearing* the host, which is a
    decision as much as setting one and must not be re-applied at the next
    start. `config_changes` is what remembers that decision.
    `_env_gateway_host` validates the value, so an unusable one is logged and
    leaves the target disabled rather than becoming an mtr/ping argument.
    """
    host = _env_gateway_host()
    if not host:
        return
    try:
        if setting_was_set_by_user(cfg.db_path, "gateway_host"):
            log.debug("GATEWAY_HOST ignored: the gateway host was configured by hand")
            return
        targets = quality_db.list_targets(cfg.db_path)
    except Exception:
        log.warning("Could not read the probe targets to apply GATEWAY_HOST", exc_info=True)
        return
    target = next((t for t in targets if t.name == "gateway"), None)
    if target is None or (target.host or "").strip():
        return
    now_iso = to_iso_z(utc_now())
    quality_db.update_target(cfg.db_path, target.id, now_iso=now_iso, host=host, enabled=True)
    set_setting(cfg.db_path, "gateway_host", host, now_iso=now_iso, source="env")
    log.info("gateway target enabled from GATEWAY_HOST=%s", host)


def _apply_gateway_host(host: str, now_iso: str) -> None:
    """Keep the `gateway` target and the `gateway_host` setting in step (§3.1).

    An empty host disables the target instead of guessing the container's
    default route, which is not the home router.
    """
    if host:
        host = api_quality.validate_host("icmp", host)
    for target in quality_db.list_targets(cfg.db_path):
        if target.name == "gateway":
            quality_db.update_target(
                cfg.db_path, target.id, now_iso=now_iso, host=host, enabled=bool(host)
            )
            break
    set_setting(cfg.db_path, "gateway_host", host, now_iso=now_iso)


@app.get("/api/config", response_model=ConfigResponse)
def api_get_config():
    return _effective_config()


@app.put("/api/config", response_model=ConfigResponse)
def api_update_config(update: ConfigUpdate):
    # Validate before writing anything: a bad `gateway_host` used to be
    # checked last (it is the last key of `QUALITY_SETTING_SPECS`), so a 422
    # on it still left every other field of the same request written.
    if update.gateway_host is not None and update.gateway_host.strip():
        api_quality.validate_host("icmp", update.gateway_host.strip())

    now_iso = to_iso_z(utc_now())
    if update.connect_target is not None:
        set_setting(cfg.db_path, "connect_target", update.connect_target.strip(), now_iso=now_iso)
    if update.connect_interval_seconds is not None:
        set_setting(cfg.db_path, "connect_interval_seconds", str(update.connect_interval_seconds), now_iso=now_iso)
    if update.speedtest_mode is not None:
        mode = update.speedtest_mode.strip()
        if mode not in {"url", "speedtest.net", "speedtest.pl"}:
            raise HTTPException(status_code=400, detail="speedtest_mode must be one of: url, speedtest.net, speedtest.pl")
        set_setting(cfg.db_path, "speedtest_mode", mode, now_iso=now_iso)
    if update.speedtest_url is not None:
        set_setting(cfg.db_path, "speedtest_url", update.speedtest_url.strip(), now_iso=now_iso)
    if update.speedtest_interval_seconds is not None:
        set_setting(cfg.db_path, "speedtest_interval_seconds", str(update.speedtest_interval_seconds), now_iso=now_iso)
    if update.speedtest_duration_seconds is not None:
        set_setting(cfg.db_path, "speedtest_duration_seconds", str(update.speedtest_duration_seconds), now_iso=now_iso)
    if update.connectivity_check_buffer_seconds is not None:
        set_setting(
            cfg.db_path,
            "connectivity_check_buffer_seconds",
            str(update.connectivity_check_buffer_seconds),
            now_iso=now_iso,
        )
    if update.connectivity_check_buffer_max is not None:
        set_setting(
            cfg.db_path,
            "connectivity_check_buffer_max",
            str(update.connectivity_check_buffer_max),
            now_iso=now_iso,
        )
    if update.ping_timeout_ms is not None:
        set_setting(cfg.db_path, "ping_timeout_ms", str(update.ping_timeout_ms), now_iso=now_iso)
    if update.ping_enabled is not None:
        set_setting(cfg.db_path, "ping_enabled", "true" if update.ping_enabled else "false", now_iso=now_iso)
    if update.speed_enabled is not None:
        set_setting(cfg.db_path, "speed_enabled", "true" if update.speed_enabled else "false", now_iso=now_iso)
    if update.ping_schedules is not None:
        set_setting(cfg.db_path, "ping_schedules", update.ping_schedules, now_iso=now_iso)
    if update.speed_schedules is not None:
        set_setting(cfg.db_path, "speed_schedules", update.speed_schedules, now_iso=now_iso)
    if update.telemetry_enabled is not None:
        set_setting(cfg.db_path, "telemetry_enabled", "true" if update.telemetry_enabled else "false", now_iso=now_iso)

    for key in QUALITY_SETTING_SPECS:
        if key not in ConfigUpdate.model_fields:
            continue
        value = getattr(update, key)
        if value is None:
            continue
        if key == "gateway_host":
            _apply_gateway_host(str(value).strip(), now_iso)
            continue
        set_setting(cfg.db_path, key, serialize_setting(key, value), now_iso=now_iso)

    cfg2 = _effective_config()
    if cfg2.connect_interval_seconds <= 0:
        raise HTTPException(status_code=400, detail="connect_interval_seconds must be > 0")
    if cfg2.speedtest_interval_seconds <= 0:
        raise HTTPException(status_code=400, detail="speedtest_interval_seconds must be > 0")
    return cfg2


@app.post("/api/speedtest/run")
async def api_speedtest_run():
    rt = get_runtime()
    if rt.running:
        return {"started": False, "running": True}

    async def _run():
        await run_speedtest_once(cfg)

    asyncio.create_task(_run())
    return {"started": True, "running": True}


@app.get("/api/speed")
def api_speed(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    items = query_speed_tests(cfg.db_path, tr)
    for it in items:
        it["started_at"] = to_local_iso(parse_dt(it["started_at"]))
    return {"range": {"from": to_local_iso(pr.start), "to": to_local_iso(pr.end)}, "items": items}


def _overlap_seconds(start: datetime, end: datetime, a: datetime, b: datetime) -> float:
    left = max(start, a)
    right = min(end, b)
    seconds = (right - left).total_seconds()
    return seconds if seconds > 0 else 0.0


@app.get("/api/outages")
def api_outages(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    expected: str = Query(default="all"),
) -> dict[str, Any]:
    """Outage periods in the range, optionally without the expected ones (§10).

    `expected_hidden` is counted rather than inferred, so a filtered list can
    say how many rows it is not showing — an empty list otherwise reads as
    "nothing happened" when it means "everything was planned".
    """
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    mode = quality_db.expected_mode(expected)
    rows = query_connectivity_periods(cfg.db_path, tr=tr, is_up=False, expected=mode)
    total = (
        len(rows)
        if mode == "all"
        else len(query_connectivity_periods(cfg.db_path, tr=tr, is_up=False))
    )

    # The same payload the PATCH answers with, so a row never changes shape
    # between being listed and being marked.
    items = [_outage_payload(r) for r in rows]

    return {
        "range": {"from": to_local_iso(pr.start), "to": to_local_iso(pr.end)},
        "expected_filter": mode,
        "expected_hidden": total - len(rows),
        "items": items,
    }


def _outage_payload(row: dict[str, Any]) -> dict[str, Any]:
    """One availability period in the shape `/api/outages` lists them in.

    An outage still running has no end, but the list has always shown one — the
    current time — so a row can be drawn without a special case. That reading is
    kept, and `open` says which of the two it is: without it a caller cannot
    tell "ended a moment ago" from "still going", and would offer to mark a
    running outage expected, which `PATCH .../expected` refuses with 409.
    """
    ended_at = parse_dt(row["ended_at"]) if row["ended_at"] else utc_now()
    return {
        "id": row["id"],
        "started_at": to_local_iso(parse_dt(row["started_at"])),
        "ended_at": to_local_iso(ended_at),
        "open": row["ended_at"] is None,
        "expected": row["expected"],
        "expected_source": row["expected_source"],
        "expected_rule_id": row["expected_rule_id"],
    }


@app.patch("/api/outages/{period_id}/expected")
def api_mark_outage_expected(period_id: int, body: ExpectedMark) -> dict[str, Any]:
    """A person's verdict on one closed outage period; it outranks any rule (§6).

    The mirror of `PATCH /api/quality/incidents/{id}/expected`: `manual` is
    written for a `false` too, so "somebody looked and this was real" stays
    distinguishable from "nobody has looked" (Review Focus #5). An "up" period
    is not an outage, so it answers 404 rather than pretending to mark it.
    """
    row = get_connectivity_period(cfg.db_path, period_id)
    if row is None or bool(row["is_up"]):
        raise HTTPException(status_code=404, detail="nie ma takiej awarii")
    if not row["ended_at"]:
        # Nothing to weigh against a window yet, and the tracker will judge it
        # itself the moment connectivity returns.
        raise HTTPException(status_code=409, detail="awaria jeszcze trwa")

    # The helper writes all three columns, so the rule that had matched is
    # passed back in rather than dropped: spec §6 keeps it, because it says
    # which window claimed this outage — the thing a hand verdict overrules.
    mark_connectivity_period_expected_by_id(
        cfg.db_path,
        period_id,
        expected=body.expected,
        source="manual",
        rule_id=row["expected_rule_id"],
    )
    note = (body.note or "").strip()
    if note:
        quality_db.insert_annotation(cfg.db_path, str(row["started_at"]), "expected", note=note)
    updated = get_connectivity_period(cfg.db_path, period_id)
    return {"outage": _outage_payload(updated)}


@app.get("/api/pings")
def api_pings(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    rows = query_connectivity_checks(cfg.db_path, tr=tr)
    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "checked_at": to_local_iso(parse_dt(r["checked_at"])),
                "is_up": r["is_up"],
                "latency_ms": r.get("latency_ms"),
            }
        )
    return {"range": {"from": to_local_iso(pr.start), "to": to_local_iso(pr.end)}, "items": items}


@app.get("/api/blocked-periods")
def api_blocked_periods(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    test_type: str = Query(default="speed"),
) -> dict[str, Any]:
    """Return blocked periods for a given test type (ping or speed) in the specified range."""
    from .db import query_blocked_periods

    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    rows = query_blocked_periods(cfg.db_path, tr=tr, test_type=test_type)

    items: list[dict[str, Any]] = []
    for r in rows:
        started_at = to_local_iso(parse_dt(r["started_at"]))
        ended_at = to_local_iso(parse_dt(r["ended_at"])) if r["ended_at"] else to_local_iso(utc_now())
        items.append({
            "started_at": started_at,
            "ended_at": ended_at,
            "reason": r["reason"],
        })

    return {"range": {"from": to_local_iso(pr.start), "to": to_local_iso(pr.end)}, "items": items}


@app.get("/api/report/quality")
def api_report_quality(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    expected: str = Query(default="all"),
) -> dict[str, Any]:
    """The dashboard's downtime figures, optionally ignoring expected outages.

    This — not `/api/outages` — is what fills `q-downtime` and `q-percent`, so
    the `expected` filter has to bite here for "pomiń spodziewane" to change
    the number a person reads off the panel (spec §10).
    """
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    mode = quality_db.expected_mode(expected)
    down_periods = query_connectivity_periods(cfg.db_path, tr=tr, is_up=False, expected=mode)
    hidden = (
        0
        if mode == "all"
        else len(query_connectivity_periods(cfg.db_path, tr=tr, is_up=False)) - len(down_periods)
    )

    cov = coverage(cfg.db_path, pr.start, pr.end, test_type="ping")
    coverage_known = bool(cov["coverage_known"])
    observed = observed_intervals(cfg.db_path, pr.start, pr.end) if coverage_known else []

    total_seconds = max(0.0, (pr.end - pr.start).total_seconds())
    now = utc_now()
    downtime_of_range_seconds = 0.0
    downtime_observed_seconds = 0.0
    incident_count = 0

    for p in down_periods:
        incident_count += 1
        start_dt = parse_dt(p["started_at"])
        end_iso = p["ended_at"] or to_iso_z(now)
        end_dt = parse_dt(end_iso)
        downtime_of_range_seconds += _overlap_seconds(pr.start, pr.end, start_dt, end_dt)
        for observed_start, observed_end in clip_to_observed([(start_dt, end_dt)], observed):
            downtime_observed_seconds += (observed_end - observed_start).total_seconds()

    downtime_percent_of_range = (
        (downtime_of_range_seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0
    )
    if coverage_known:
        observed_seconds = float(cov["observed_seconds"])
        downtime_seconds = downtime_observed_seconds
        downtime_percent = (
            (downtime_seconds / observed_seconds * 100.0) if observed_seconds > 0 else 0.0
        )
    else:
        # Pre-v2 history: no session was ever recorded, so the observed period is
        # unknown and the legacy numbers are reported as they were.
        observed_seconds = None
        downtime_seconds = downtime_of_range_seconds
        downtime_percent = downtime_percent_of_range

    return {
        "range": {"from": to_local_iso(pr.start), "to": to_local_iso(pr.end)},
        "expected_filter": mode,
        "expected_hidden": hidden,
        "incident_count": incident_count,
        "downtime_seconds": downtime_seconds,
        "total_seconds": total_seconds,
        "downtime_percent": downtime_percent,
        "downtime_percent_of_range": downtime_percent_of_range,
        "observed_seconds": observed_seconds,
        "coverage_pct": cov["coverage_pct"],
        "coverage_known": coverage_known,
        "gaps": [
            {
                "from": to_local_iso(parse_dt(gap["from"])),
                "to": to_local_iso(parse_dt(gap["to"])),
                "reason": gap["reason"],
            }
            for gap in cov["gaps"]
        ],
    }


@app.get("/api/export/speed.csv")
def export_speed_csv(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    items = query_speed_tests(cfg.db_path, tr)
    rows: list[list[Any]] = [
        [
            "started_at",
            "download_mbps",
            "upload_mbps",
            "ping_ms",
            "server_name",
            "server_country",
            "speedtest_mode",
            "duration_seconds",
            "bytes_downloaded",
            "error",
        ]
    ]
    for it in items:
        started_local = to_local_display(parse_dt(it["started_at"]))
        rows.append(
            [
                started_local,
                it["mbps"],
                it.get("upload_mbps"),
                it.get("ping_ms"),
                it.get("server_name") or "",
                it.get("server_country") or "",
                it.get("speedtest_mode") or "",
                it["duration_seconds"],
                it["bytes_downloaded"],
                it["error"] or "",
            ]
        )
    return _csv_response("speed.csv", rows)


@app.get("/api/export/outages.csv")
def export_outages_csv(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    expected: str = Query(default="all"),
):
    """Outage periods as CSV, carrying the expected-window verdict (spec §10).

    The flag is a column, not a silent omission: this file is the copy handed
    to an ISP, so a planned outage stays visible *and* labelled.
    """
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    items = query_connectivity_periods(
        cfg.db_path, tr=tr, is_up=False, expected=quality_db.expected_mode(expected)
    )
    rows: list[list[Any]] = [["started_at", "ended_at", "expected", "expected_source"]]
    now_iso = to_iso_z(utc_now())
    for it in items:
        started_local = to_local_display(parse_dt(it["started_at"]))
        ended_local = to_local_display(parse_dt(it["ended_at"])) if it["ended_at"] else to_local_display(parse_dt(now_iso))
        rows.append([started_local, ended_local, it["expected"], it["expected_source"] or ""])
    return _csv_response("outages.csv", rows)


@app.get("/api/export/pings.csv")
def export_pings_csv(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    items = query_connectivity_checks(cfg.db_path, tr=tr)
    rows: list[list[Any]] = [["checked_at", "is_up", "latency_ms"]]
    for it in items:
        checked_local = to_local_display(parse_dt(it["checked_at"]))
        latency = it.get("latency_ms")
        latency_ms = round(latency) if latency is not None else ""
        rows.append([checked_local, it["is_up"], latency_ms])
    return _csv_response("pings.csv", rows)


# ---------------------------------------------------------------------------
# Network diagnostic tools (on-demand, no persistence)
# ---------------------------------------------------------------------------

from .network_tools import run_tool as _run_network_tool, TOOL_DEFINITIONS as _TOOL_DEFS


@app.get("/api/tools")
def api_tools_list():
    return {"tools": _TOOL_DEFS}


@app.post("/api/tools/{tool_name}")
async def api_run_tool(tool_name: str, params: dict = {}):
    try:
        return await _run_network_tool(tool_name, params)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
