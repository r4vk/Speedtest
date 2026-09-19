from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from .config import AppConfig
from .db import (
    get_settings,
    get_current_connectivity_period,
    record_speed_test,
    start_blocked_period,
    end_blocked_period,
)
from .ookla_speedtest import run_ookla
from .runtime import get_runtime
from .speedtest import run_speed_test
from .time_utils import to_iso_z, utc_now

log = logging.getLogger(__name__)


def _is_blocked_by_schedule(schedules_json: str) -> bool:
    """Check if current time is within any blocking schedule.

    Schedule format: [{"from": "HH:MM", "to": "HH:MM", "days": [0,1,2,3,4]}]
    Days: 0=Monday, 1=Tuesday, ..., 6=Sunday
    """
    try:
        schedules = json.loads(schedules_json) if schedules_json else []
    except (json.JSONDecodeError, TypeError):
        return False

    if not schedules:
        return False

    now = datetime.now().astimezone()
    current_day = now.weekday()  # 0=Monday, 6=Sunday
    current_time = now.strftime("%H:%M")

    for sched in schedules:
        days = sched.get("days", [])
        if not days or current_day not in days:
            continue

        time_from = sched.get("from", "00:00")
        time_to = sched.get("to", "23:59")

        # Handle time range (including overnight ranges)
        if time_from <= time_to:
            # Normal range: e.g., 08:00 - 16:00
            if time_from <= current_time <= time_to:
                return True
        else:
            # Overnight range: e.g., 22:00 - 06:00
            if current_time >= time_from or current_time <= time_to:
                return True

    return False


@dataclass(frozen=True)
class RunningState:
    stop: asyncio.Event


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.001, seconds))
        return True
    except asyncio.TimeoutError:
        return False


def _seconds_until_next_aligned(interval_seconds: float) -> float:
    interval_seconds = max(0.1, float(interval_seconds))
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    remainder = elapsed % interval_seconds
    # jeśli jesteśmy "na granicy" to planuj następny tick, nie natychmiast
    if remainder < 0.01:
        return interval_seconds
    return max(0.001, interval_seconds - remainder)


async def run_speedtest_once(cfg: AppConfig) -> None:
    runtime = get_runtime()
    async with runtime.lock:
        runtime.running = True
        runtime.running_since_iso = to_iso_z(utc_now())
        try:
            values = get_settings(cfg.db_path, ["speedtest_mode", "speedtest_url", "speedtest_duration_seconds"])
            speedtest_mode = (values.get("speedtest_mode") or "speedtest.net").strip()
            if speedtest_mode not in {"url", "speedtest.net", "speedtest.pl"}:
                speedtest_mode = "speedtest.net"
            speedtest_url = (values.get("speedtest_url") or cfg.speedtest_url or "").strip()
            try:
                speedtest_duration = float(values.get("speedtest_duration_seconds", str(cfg.speedtest_duration_seconds)))
            except ValueError:
                speedtest_duration = cfg.speedtest_duration_seconds

            started_at = utc_now()
            started_at_iso = to_iso_z(started_at)

            error: str | None = None
            bytes_downloaded = 0
            duration_seconds = 0.0
            mbps = 0.0

            if cfg.speedtest_skip_if_offline:
                current = get_current_connectivity_period(cfg.db_path)
                if current is not None and not bool(current["is_up"]):
                    error = "offline (skipped)"
                    duration_seconds = 0.0

            if error is None:
                if speedtest_mode in {"speedtest.net", "speedtest.pl"}:
                    result = await asyncio.to_thread(run_ookla, speedtest_mode, cfg.speedtest_timeout_seconds)
                    error = result.error
                    duration_seconds = result.duration_seconds
                    mbps = result.download_mbps
                    bytes_downloaded = 0
                    record_speed_test(
                        cfg.db_path,
                        started_at_iso=started_at_iso,
                        duration_seconds=duration_seconds,
                        bytes_downloaded=bytes_downloaded,
                        mbps=mbps,
                        error=error,
                        speedtest_mode=speedtest_mode,
                        upload_mbps=result.upload_mbps,
                        ping_ms=result.ping_ms,
                        server_name=result.server_name,
                        server_country=result.server_country,
                    )
                    return
                else:
                    if speedtest_url:
                        result = await asyncio.to_thread(
                            run_speed_test,
                            speedtest_url,
                            speedtest_duration,
                            cfg.speedtest_timeout_seconds,
                        )
                        error = result.error
                        bytes_downloaded = result.bytes_downloaded
                        duration_seconds = result.duration_seconds
                        mbps = result.mbps
                    else:
                        error = "speedtest_url not set (skipped)"

            record_speed_test(
                cfg.db_path,
                started_at_iso=started_at_iso,
                duration_seconds=duration_seconds,
                bytes_downloaded=bytes_downloaded,
                mbps=mbps,
                error=error,
                speedtest_mode=speedtest_mode,
            )
        finally:
            runtime.running = False
            runtime.running_since_iso = None


async def speedtest_loop(cfg: AppConfig, state: RunningState) -> None:
    while not state.stop.is_set():
        values = get_settings(
            cfg.db_path,
            ["speedtest_mode", "speedtest_url", "speedtest_interval_seconds", "speed_enabled", "speed_schedules"],
        )

        # Check if speed test is enabled
        speed_enabled = values.get("speed_enabled", "true").lower() == "true"
        if not speed_enabled:
            start_blocked_period(cfg.db_path, "speed", "disabled")
            stopped = await _sleep_or_stop(state.stop, 1.0)
            if stopped:
                return
            continue

        # Check if blocked by schedule
        speed_schedules = values.get("speed_schedules", "[]")
        if _is_blocked_by_schedule(speed_schedules):
            start_blocked_period(cfg.db_path, "speed", "schedule")
            stopped = await _sleep_or_stop(state.stop, 1.0)
            if stopped:
                return
            continue

        # Not blocked - end any active blocked period
        end_blocked_period(cfg.db_path, "speed")

        try:
            interval_seconds = float(values.get("speedtest_interval_seconds", str(cfg.speedtest_interval_seconds)))
        except ValueError:
            interval_seconds = cfg.speedtest_interval_seconds
        interval_seconds = max(1.0, interval_seconds)

        await run_speedtest_once(cfg)

        # Kolejne testy wyrównane do "od północy" zamiast od momentu zapisu ustawień.
        stopped = await _sleep_or_stop(state.stop, _seconds_until_next_aligned(interval_seconds))
        if stopped:
            return
