from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timedelta

import httpx

from .db import get_settings, set_setting
from .time_utils import parse_dt, to_iso_z, utc_now


DEFAULT_TELEMETRY_ENDPOINT = "https://speedtest-telemetry.kawecki-r.workers.dev/collect"

TELEMETRY_ENABLED_KEY = "telemetry_enabled"
TELEMETRY_INSTALL_ID_KEY = "telemetry_install_id"
TELEMETRY_LAST_ACTIVE_AT_KEY = "telemetry_last_active_at"

_ACTIVE_MIN_INTERVAL = timedelta(hours=23)
_ACTIVE_CHECK_INTERVAL_SECONDS = 3600
_ACTIVE_INITIAL_JITTER_SECONDS_MAX = 5 * 60


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def ensure_install_id(db_path: str, now_iso: str | None = None) -> str:
    current = get_settings(db_path, [TELEMETRY_INSTALL_ID_KEY]).get(TELEMETRY_INSTALL_ID_KEY, "").strip()
    if current:
        return current
    new_id = uuid.uuid4().hex
    set_setting(db_path, TELEMETRY_INSTALL_ID_KEY, new_id, now_iso=now_iso)
    return new_id


def is_enabled(db_path: str, default_enabled: bool) -> bool:
    raw = get_settings(db_path, [TELEMETRY_ENABLED_KEY]).get(TELEMETRY_ENABLED_KEY)
    return _as_bool(raw, default_enabled)


def _should_send_active(db_path: str, now: datetime) -> bool:
    raw = get_settings(db_path, [TELEMETRY_LAST_ACTIVE_AT_KEY]).get(TELEMETRY_LAST_ACTIVE_AT_KEY, "").strip()
    if not raw:
        return True
    try:
        last_dt = parse_dt(raw)
    except Exception:
        return True
    return (now - last_dt) >= _ACTIVE_MIN_INTERVAL


async def _send_event(
    *,
    db_path: str,
    app_version: str,
    default_enabled: bool,
    timeout_seconds: float,
    event: str,
) -> None:
    if not is_enabled(db_path, default_enabled):
        return

    now_iso = to_iso_z(utc_now())
    install_id = ensure_install_id(db_path, now_iso=now_iso)
    payload = {
        "event": event,
        "install_id": install_id,
        "version": app_version,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(DEFAULT_TELEMETRY_ENDPOINT, json=payload)
            if 200 <= resp.status_code < 300 and event == "app_active":
                set_setting(db_path, TELEMETRY_LAST_ACTIVE_AT_KEY, now_iso, now_iso=now_iso)
    except Exception:
        # Telemetry is best-effort and must never impact app startup.
        return


async def send_startup_event(
    *,
    db_path: str,
    app_version: str,
    default_enabled: bool,
    timeout_seconds: float,
) -> None:
    await _send_event(
        db_path=db_path,
        app_version=app_version,
        default_enabled=default_enabled,
        timeout_seconds=timeout_seconds,
        event="app_started",
    )


async def active_heartbeat_loop(
    *,
    db_path: str,
    app_version: str,
    default_enabled: bool,
    timeout_seconds: float,
) -> None:
    await asyncio.sleep(random.uniform(0.0, float(_ACTIVE_INITIAL_JITTER_SECONDS_MAX)))
    while True:
        now = utc_now()
        if is_enabled(db_path, default_enabled) and _should_send_active(db_path, now):
            await _send_event(
                db_path=db_path,
                app_version=app_version,
                default_enabled=default_enabled,
                timeout_seconds=timeout_seconds,
                event="app_active",
            )
        await asyncio.sleep(_ACTIVE_CHECK_INTERVAL_SECONDS)
