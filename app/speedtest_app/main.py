from __future__ import annotations

import asyncio
import csv
import io
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import AppConfig
from .db import (
    TimeRange,
    ensure_db,
    ensure_default_setting,
    get_current_connectivity_period,
    get_last_speed_test,
    get_last_success_speed_test,
    query_connectivity_periods,
    query_connectivity_checks,
    query_speed_tests,
    set_setting,
    get_settings,
)
from .runtime import get_runtime, init_runtime
from .scheduler import RunningState, connectivity_loop, run_speedtest_once, speedtest_loop
from .telemetry import active_heartbeat_loop, send_startup_event
from .time_utils import parse_dt, parse_range, to_iso_z, to_local_display, to_local_iso, utc_now


DEFAULT_SPEEDTEST_MODE = "speedtest.net"

cfg = AppConfig()
ensure_db(cfg.db_path)

ensure_default_setting(cfg.db_path, "connect_target", cfg.connect_target)
ensure_default_setting(cfg.db_path, "connect_interval_seconds", str(cfg.connect_interval_seconds))
ensure_default_setting(cfg.db_path, "speedtest_mode", DEFAULT_SPEEDTEST_MODE)
ensure_default_setting(cfg.db_path, "speedtest_url", cfg.speedtest_url or "")
ensure_default_setting(cfg.db_path, "speedtest_interval_seconds", str(cfg.speedtest_interval_seconds))
ensure_default_setting(cfg.db_path, "speedtest_duration_seconds", str(cfg.speedtest_duration_seconds))
ensure_default_setting(cfg.db_path, "connectivity_check_buffer_seconds", str(cfg.connectivity_check_buffer_seconds))
ensure_default_setting(cfg.db_path, "connectivity_check_buffer_max", str(cfg.connectivity_check_buffer_max))
ensure_default_setting(cfg.db_path, "ping_enabled", "true")
ensure_default_setting(cfg.db_path, "speed_enabled", "true")
ensure_default_setting(cfg.db_path, "ping_schedules", "[]")
ensure_default_setting(cfg.db_path, "speed_schedules", "[]")
ensure_default_setting(cfg.db_path, "telemetry_enabled", "true" if cfg.telemetry_default_enabled else "false")
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
app = FastAPI(title="Speedtest Monitor", version=APP_VERSION)
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


@app.on_event("startup")
async def _startup() -> None:
    init_runtime()
    state = RunningState(stop=asyncio.Event())
    app.state.running_state = state
    app.state.tasks = [
        asyncio.create_task(connectivity_loop(cfg, state)),
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
    ]


@app.on_event("shutdown")
async def _shutdown() -> None:
    state: RunningState | None = getattr(app.state, "running_state", None)
    if state is None:
        return
    state.stop.set()
    tasks = getattr(app.state, "tasks", [])
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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
    ping_enabled: bool
    speed_enabled: bool
    ping_schedules: str
    speed_schedules: str
    telemetry_enabled: bool
    telemetry_endpoint_configured: bool


class ConfigUpdate(BaseModel):
    connect_target: str | None = Field(default=None, min_length=1, max_length=1024)
    connect_interval_seconds: float | None = Field(default=None, gt=0.1, le=3600)
    speedtest_mode: str | None = Field(default=None)
    speedtest_url: str | None = Field(default=None, max_length=2048)
    speedtest_interval_seconds: float | None = Field(default=None, gt=1, le=7 * 24 * 3600)
    speedtest_duration_seconds: float | None = Field(default=None, ge=5, le=120)
    connectivity_check_buffer_seconds: float | None = Field(default=None, ge=0, le=24 * 3600)
    connectivity_check_buffer_max: int | None = Field(default=None, ge=1, le=100000)
    ping_enabled: bool | None = Field(default=None)
    speed_enabled: bool | None = Field(default=None)
    ping_schedules: str | None = Field(default=None, max_length=8192)
    speed_schedules: str | None = Field(default=None, max_length=8192)
    telemetry_enabled: bool | None = Field(default=None)


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

    ping_enabled = values.get("ping_enabled", "true").lower() == "true"
    speed_enabled = values.get("speed_enabled", "true").lower() == "true"
    ping_schedules = values.get("ping_schedules", "[]")
    speed_schedules = values.get("speed_schedules", "[]")
    telemetry_enabled = values.get(
        "telemetry_enabled",
        "true" if cfg.telemetry_default_enabled else "false",
    ).lower() == "true"

    return ConfigResponse(
        connect_target=connect_target,
        connect_interval_seconds=connect_interval,
        speedtest_mode=speedtest_mode,
        speedtest_url=speedtest_url,
        speedtest_interval_seconds=speedtest_interval,
        speedtest_duration_seconds=speedtest_duration,
        connectivity_check_buffer_seconds=buffer_seconds,
        connectivity_check_buffer_max=buffer_max,
        ping_enabled=ping_enabled,
        speed_enabled=speed_enabled,
        ping_schedules=ping_schedules,
        speed_schedules=speed_schedules,
        telemetry_enabled=telemetry_enabled,
        telemetry_endpoint_configured=True,
    )


@app.get("/api/config", response_model=ConfigResponse)
def api_get_config():
    return _effective_config()


@app.put("/api/config", response_model=ConfigResponse)
def api_update_config(update: ConfigUpdate):
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
) -> dict[str, Any]:
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    rows = query_connectivity_periods(cfg.db_path, tr=tr, is_up=False)

    items: list[dict[str, Any]] = []
    for r in rows:
        started_at = to_local_iso(parse_dt(r["started_at"]))
        ended_at = to_local_iso(parse_dt(r["ended_at"])) if r["ended_at"] else to_local_iso(utc_now())
        items.append({"started_at": started_at, "ended_at": ended_at})

    return {"range": {"from": to_local_iso(pr.start), "to": to_local_iso(pr.end)}, "items": items}


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
) -> dict[str, Any]:
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    down_periods = query_connectivity_periods(cfg.db_path, tr=tr, is_up=False)

    total_seconds = max(0.0, (pr.end - pr.start).total_seconds())
    now = utc_now()
    downtime_seconds = 0.0
    incident_count = 0

    for p in down_periods:
        incident_count += 1
        start_dt = parse_dt(p["started_at"])
        end_iso = p["ended_at"] or to_iso_z(now)
        end_dt = parse_dt(end_iso)
        downtime_seconds += _overlap_seconds(pr.start, pr.end, start_dt, end_dt)

    downtime_percent = (downtime_seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0

    return {
        "range": {"from": to_local_iso(pr.start), "to": to_local_iso(pr.end)},
        "incident_count": incident_count,
        "downtime_seconds": downtime_seconds,
        "total_seconds": total_seconds,
        "downtime_percent": downtime_percent,
    }


def _csv_response(filename: str, rows: list[list[Any]]) -> StreamingResponse:
    def iter_csv():
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in rows:
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
):
    pr = parse_range(from_, to)
    tr = TimeRange(start_iso=to_iso_z(pr.start), end_iso=to_iso_z(pr.end))
    items = query_connectivity_periods(cfg.db_path, tr=tr, is_up=False)
    rows: list[list[Any]] = [["started_at", "ended_at"]]
    now_iso = to_iso_z(utc_now())
    for it in items:
        started_local = to_local_display(parse_dt(it["started_at"]))
        ended_local = to_local_display(parse_dt(it["ended_at"])) if it["ended_at"] else to_local_display(parse_dt(now_iso))
        rows.append([started_local, ended_local])
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
        rows.append([checked_local, it["is_up"], it.get("latency_ms") or ""])
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
