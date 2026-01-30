from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from .config import AppConfig
from .connectivity import check_target
from .db import (
    get_settings,
    get_current_connectivity_period,
    record_connectivity,
    record_connectivity_check,
    record_connectivity_checks_batch,
    record_speed_test,
)
from .email_notify import send_outage_notification
from .ookla_speedtest import run_ookla
from .runtime import get_runtime
from .speedtest import run_speed_test
from .time_utils import to_iso_z, to_local_display, parse_dt, utc_now

log = logging.getLogger(__name__)


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
            speedtest_mode = (values.get("speedtest_mode") or "url").strip()
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


async def connectivity_loop(cfg: AppConfig, state: RunningState) -> None:
    last_is_up: bool | None = None
    outage_started_at: str | None = None
    pending_checks: list[tuple[str, bool, float | None]] = []
    last_flush_ts = time.monotonic()

    def _flush_pending() -> None:
        nonlocal pending_checks, last_flush_ts
        if not pending_checks:
            return
        record_connectivity_checks_batch(cfg.db_path, pending_checks)
        pending_checks = []
        last_flush_ts = time.monotonic()

    # Get initial state from DB.
    current_period = get_current_connectivity_period(cfg.db_path)
    if current_period is not None:
        last_is_up = bool(current_period["is_up"])
        if not last_is_up:
            outage_started_at = current_period["started_at"]

    try:
        while not state.stop.is_set():
            values = get_settings(
                cfg.db_path,
                [
                    "connect_target",
                    "connect_interval_seconds",
                    "connectivity_check_buffer_seconds",
                    "connectivity_check_buffer_max",
                ],
            )
            connect_target = values.get("connect_target", cfg.connect_target)
            try:
                interval_seconds = float(values.get("connect_interval_seconds", str(cfg.connect_interval_seconds)))
            except ValueError:
                interval_seconds = cfg.connect_interval_seconds
            interval_seconds = max(0.1, interval_seconds)
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
            buffer_seconds = max(0.0, buffer_seconds)
            buffer_max = max(1, buffer_max)

            t0 = time.perf_counter()
            is_up = await asyncio.to_thread(
                check_target,
                connect_target,
                cfg.connect_default_port,
                cfg.connect_timeout_seconds,
            )
            dt_ms = (time.perf_counter() - t0) * 1000.0
            now_iso = to_iso_z(utc_now())
            if buffer_seconds <= 0 and buffer_max <= 1:
                record_connectivity_check(cfg.db_path, is_up=is_up, checked_at_iso=now_iso, latency_ms=dt_ms)
            else:
                pending_checks.append((now_iso, is_up, dt_ms))
                if len(pending_checks) >= buffer_max or (time.monotonic() - last_flush_ts) >= buffer_seconds:
                    _flush_pending()
            record_connectivity(cfg.db_path, is_up=is_up, now_iso=now_iso)

            # Detect state changes and send email notifications.
            if last_is_up is not None and is_up != last_is_up:
                if not is_up:
                    # Internet went down - remember outage start time.
                    outage_started_at = now_iso
                    log.info("Internet outage detected at %s", to_local_display(parse_dt(now_iso)))
                else:
                    # Internet restored - send notification if outage was long enough.
                    ended_local = to_local_display(parse_dt(now_iso))
                    started_local = to_local_display(parse_dt(outage_started_at)) if outage_started_at else "?"
                    log.info("Internet restored at %s", ended_local)

                    if cfg.smtp_enabled and outage_started_at:
                        outage_start_dt = parse_dt(outage_started_at)
                        outage_end_dt = parse_dt(now_iso)
                        outage_duration_seconds = (outage_end_dt - outage_start_dt).total_seconds()

                        if outage_duration_seconds >= cfg.smtp_min_outage_seconds:
                            log.info(
                                "Outage lasted %.0f seconds (>= %d), sending email notification",
                                outage_duration_seconds,
                                cfg.smtp_min_outage_seconds,
                            )
                            await asyncio.to_thread(
                                send_outage_notification, cfg, started_local, ended_local, outage_duration_seconds
                            )
                        else:
                            log.info(
                                "Outage lasted %.0f seconds (< %d), skipping email notification",
                                outage_duration_seconds,
                                cfg.smtp_min_outage_seconds,
                            )

                    outage_started_at = None

            last_is_up = is_up

            # Interwał liczony od początku doby (lokalna strefa czasowa kontenera/hosta).
            stopped = await _sleep_or_stop(state.stop, _seconds_until_next_aligned(interval_seconds))
            if stopped:
                return
    finally:
        _flush_pending()


async def speedtest_loop(cfg: AppConfig, state: RunningState) -> None:
    while not state.stop.is_set():
        values = get_settings(cfg.db_path, ["speedtest_mode", "speedtest_url", "speedtest_interval_seconds"])
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
