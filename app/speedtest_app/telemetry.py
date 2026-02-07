from __future__ import annotations

import uuid

import httpx

from .db import get_settings, set_setting
from .time_utils import to_iso_z, utc_now


TELEMETRY_ENABLED_KEY = "telemetry_enabled"
TELEMETRY_INSTALL_ID_KEY = "telemetry_install_id"


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


async def send_startup_event(
    *,
    db_path: str,
    endpoint: str | None,
    auth_token: str | None,
    app_version: str,
    default_enabled: bool,
    timeout_seconds: float,
) -> None:
    if not endpoint or not endpoint.strip():
        return
    if not is_enabled(db_path, default_enabled):
        return

    started_at = to_iso_z(utc_now())
    install_id = ensure_install_id(db_path, now_iso=started_at)
    payload = {
        "event": "app_started",
        "install_id": install_id,
        "version": app_version,
        "started_at": started_at,
    }
    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            await client.post(endpoint, json=payload, headers=headers)
    except Exception:
        # Telemetry is best-effort and must never impact app startup.
        return
