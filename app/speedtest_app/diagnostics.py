"""Incident-triggered mtr diagnostics (design spec §11, plan stage 6).

An incident says *that* the path degraded; mtr is the attempt to say *where*.
This module decides whether a diagnostic may run, runs it off the incident
loop, stores the parsed hops with the raw output, and phrases the reading of
those hops as hypotheses — never as a verdict and never as the operator's
fault.

Two constraints shape the design:

- `QualityEngine` awaits its subscribers inline, so `on_incident_event` only
  *decides* (synchronously) and spawns the ~90 s mtr as a tracked task. A
  diagnostic never delays the next incident window.
- A long degradation must not start an avalanche of mtr processes. Every
  refused trigger is written down as `status='error', error='rate_limited'`,
  so the omission is visible instead of silent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from . import quality_db
from .incidents import IncidentEvent
from .network_tools import _run_subprocess
from .probe_types import ProbeTarget
from .time_utils import parse_dt, to_iso_z, utc_now

log = logging.getLogger(__name__)

#: Settings keys read before every trigger (spec §11).
SETTINGS_KEYS: list[str] = [
    "diagnostics_enabled",
    "diagnostics_max_concurrent",
    "diagnostics_min_interval_seconds",
    "diagnostics_max_per_incident",
    "diagnostics_mtr_count",
    "diagnostics_mtr_timeout_seconds",
]

#: An `updated` event only triggers once the incident has been open this long.
UPDATED_TRIGGER_AFTER_SECONDS = 120.0
#: Hop loss from which a hypothesis may be phrased at all.
LOSS_HYPOTHESIS_PCT = 5.0
#: Loss at which a hop is read as "did not answer" rather than "lost traffic".
SILENT_HOP_LOSS_PCT = 99.9

SubprocessRunner = Callable[..., Awaitable[tuple[int, str, str]]]


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def _setting(values: Mapping[str, str], key: str, default: Any, cast: Callable[[str], Any]) -> Any:
    raw = values.get(key)
    if raw is None:
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("Setting %s has an unusable value %r, keeping %r", key, raw, default)
        return default


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class DiagnosticsSettings:
    """The `diagnostics_*` settings of spec §11, with the spec's defaults."""

    enabled: bool = True
    max_concurrent: int = 1
    min_interval_seconds: int = 300
    max_per_incident: int = 3
    mtr_count: int = 10
    mtr_timeout_seconds: int = 90

    @staticmethod
    def from_settings(values: Mapping[str, str]) -> "DiagnosticsSettings":
        """Parse the `diagnostics_*` keys; anything unusable keeps the default.

        Out-of-range numbers are clamped rather than rejected: a limit of 0 or
        a negative one would either disable diagnostics behind the operator's
        back or let them run unbounded.
        """
        d = DiagnosticsSettings()
        return DiagnosticsSettings(
            enabled=_setting(values, "diagnostics_enabled", d.enabled, _as_bool),
            max_concurrent=max(
                1, _setting(values, "diagnostics_max_concurrent", d.max_concurrent, int)
            ),
            min_interval_seconds=max(
                0,
                _setting(
                    values, "diagnostics_min_interval_seconds", d.min_interval_seconds, int
                ),
            ),
            max_per_incident=max(
                1, _setting(values, "diagnostics_max_per_incident", d.max_per_incident, int)
            ),
            mtr_count=min(max(1, _setting(values, "diagnostics_mtr_count", d.mtr_count, int)), 100),
            mtr_timeout_seconds=max(
                1,
                _setting(
                    values, "diagnostics_mtr_timeout_seconds", d.mtr_timeout_seconds, int
                ),
            ),
        )


# ---------------------------------------------------------------------------
# mtr output
# ---------------------------------------------------------------------------


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: object, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    return int(value) if isinstance(value, (int, float)) else default


def parse_mtr_json(stdout: str) -> list[dict[str, Any]]:
    """Parse `mtr --report --json` output into hop dicts, oldest hop first.

    A field mtr did not report stays `None`: an unknown loss is never turned
    into 0 %, and a hop that did not answer has no host instead of `"???"`.
    Unparsable output is no hops at all — the caller records the failure.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return []
    report = data.get("report") if isinstance(data, dict) else None
    hubs = report.get("hubs") if isinstance(report, dict) else None
    if not isinstance(hubs, list):
        return []

    hops: list[dict[str, Any]] = []
    for index, hub in enumerate(hubs, start=1):
        if not isinstance(hub, dict):
            continue
        host = hub.get("host")
        host = host.strip() if isinstance(host, str) else ""
        hops.append(
            {
                "hop": _int(hub.get("count"), index),
                "host": host if host and host not in {"???", "*"} else None,
                "loss_pct": _number(hub.get("Loss%")),
                "sent": _int(hub.get("Snt")),
                "avg_ms": _number(hub.get("Avg")),
                "best_ms": _number(hub.get("Best")),
                "worst_ms": _number(hub.get("Wrst")),
                "stdev_ms": _number(hub.get("StDev")),
            }
        )
    return hops


def _loss_onset(hops: list[dict[str, Any]]) -> int | None:
    """Index of the first hop of the loss that reaches the last hop.

    Loss that does not persist to the end of the path is not end-to-end loss,
    so only the trailing run of lossy hops counts.
    """
    onset: int | None = None
    for index in range(len(hops) - 1, -1, -1):
        loss = hops[index].get("loss_pct")
        if loss is None or loss < LOSS_HYPOTHESIS_PCT:
            break
        onset = index
    return onset


def _is_silent(hop: Mapping[str, Any]) -> bool:
    loss = hop.get("loss_pct")
    return hop.get("host") is None or (loss is not None and loss >= SILENT_HOP_LOSS_PCT)


def _hop_number(hop: Mapping[str, Any], fallback: int) -> int:
    number = hop.get("hop")
    return number if isinstance(number, int) else fallback


def hypotheses(
    hops: list[dict[str, Any]],
    target_host: str,
    gateway_hops: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Read the hops as possibilities, in Polish (spec §11).

    Pure function. Every line starts with "Możliwa przyczyna:" or "Uwaga:",
    because a traceroute from one machine can never prove whose fault the loss
    is — it can only say where it becomes visible.
    """
    if not hops:
        return ["Uwaga: brak kompletnych danych z mtr — nie można wskazać miejsca strat."]

    lines: list[str] = []
    final_loss = hops[-1].get("loss_pct")
    onset = _loss_onset(hops)
    gateway_final_loss = gateway_hops[-1].get("loss_pct") if gateway_hops else None
    gateway_clean = gateway_final_loss is not None and gateway_final_loss < LOSS_HYPOTHESIS_PCT
    gateway_lossy = gateway_final_loss is not None and gateway_final_loss >= LOSS_HYPOTHESIS_PCT

    if final_loss is None:
        lines.append(
            f"Uwaga: mtr nie podał strat dla ostatniego przeskoku do {target_host} — "
            "pomiar jest niepełny i nie pozwala wskazać miejsca problemu."
        )
    elif onset is not None:
        hop_number = _hop_number(hops[onset], onset + 1)
        loss_text = f"{final_loss:.1f}%".replace(".0%", "%")
        if hop_number <= 1:
            lines.append(
                f"Możliwa przyczyna: problem w sieci lokalnej lub na routerze — straty "
                f"({loss_text}) widoczne już od pierwszego przeskoku."
            )
        elif hop_number <= 3:
            if gateway_clean:
                lines.append(
                    f"Możliwa przyczyna: problem po stronie dostawcy lub dalej w trasie — "
                    f"straty ({loss_text}) zaczynają się od przeskoku {hop_number}, "
                    "a pomiar do routera nie pokazuje strat."
                )
            else:
                lines.append(
                    f"Możliwa przyczyna: problem po stronie dostawcy lub dalej w trasie — "
                    f"straty ({loss_text}) zaczynają się od przeskoku {hop_number}."
                )
                if gateway_final_loss is None:
                    lines.append(
                        "Uwaga: bez pomiaru do routera nie można wykluczyć sieci lokalnej."
                    )
        else:
            lines.append(
                f"Możliwa przyczyna: problem dalej w trasie do {target_host} — straty "
                f"({loss_text}) pojawiają się dopiero od przeskoku {hop_number}."
            )
        if (
            final_loss >= SILENT_HOP_LOSS_PCT
            and len(hops) > 1
            and onset == len(hops) - 1
        ):
            lines.append(
                f"Uwaga: 100% strat tylko na ostatnim przeskoku może oznaczać, że "
                f"{target_host} nie odpowiada na ICMP, a nie utratę ruchu."
            )
        if gateway_lossy:
            lines.append(
                "Możliwa przyczyna: problem w sieci lokalnej — straty widać już w pomiarze "
                "do routera."
            )
    else:
        lines.append(
            f"Uwaga: mtr nie pokazał strat na trasie do {target_host} w tym pomiarze — "
            "problem mógł być chwilowy lub leżeć poza zasięgiem tego testu."
        )

    silent = [
        _hop_number(hop, index + 1)
        for index, hop in enumerate(hops[:-1])
        if _is_silent(hop)
    ]
    if silent:
        numbers = ", ".join(str(number) for number in silent)
        label = "przeskok" if len(silent) == 1 else "przeskoki"
        tail = (
            "ostatni przeskok odpowiada bez strat"
            if final_loss is not None and final_loss < LOSS_HYPOTHESIS_PCT
            else "routery tranzytowe często ograniczają ICMP"
        )
        lines.append(
            f"Uwaga: brak odpowiedzi węzła pośredniego ({label} {numbers}) nie dowodzi "
            f"utraty ruchu — {tail}."
        )
    return lines


# ---------------------------------------------------------------------------
# subprocess
# ---------------------------------------------------------------------------


async def _run_mtr(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run mtr; a timeout surfaces as `TimeoutError` (see `_run_subprocess`)."""
    return await _run_subprocess(argv, timeout=timeout)


def _failure_reason(returncode: int, stderr: str) -> str:
    detail = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
    if returncode == 0:
        return f"unreadable mtr report: {detail}" if detail else "unreadable mtr report"
    return f"mtr exited with {returncode}: {detail}" if detail else f"mtr exited with {returncode}"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


class DiagnosticsRunner:
    """Decides on the incident loop, diagnoses off it."""

    def __init__(
        self,
        db_path: str,
        *,
        settings_getter: Callable[[], DiagnosticsSettings],
        gateway_host_getter: Callable[[], str | None],
        subprocess_runner: SubprocessRunner = _run_mtr,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._db_path = db_path
        self._settings_getter = settings_getter
        self._gateway_host_getter = gateway_host_getter
        self._subprocess_runner = subprocess_runner
        self._clock = clock
        self._wall_clock = wall_clock
        self._tasks: set[asyncio.Task[None]] = set()
        self._running = 0
        self._last_run_at: dict[int, float] = {}
        self._last_settings = DiagnosticsSettings()

    @property
    def running(self) -> int:
        """How many diagnostics are in flight right now."""
        return self._running

    def settings(self) -> DiagnosticsSettings:
        """The current settings; a failing getter keeps the last known ones."""
        try:
            self._last_settings = self._settings_getter()
        except Exception:
            log.warning("Could not read the diagnostics settings, keeping the last known ones", exc_info=True)
        return self._last_settings

    # -- trigger (runs on the incident loop, never blocks it) --------------

    async def on_incident_event(self, event: IncidentEvent, target: ProbeTarget | None) -> None:
        """`QualityEngine.subscribe` hook: decide now, diagnose in a task."""
        try:
            self._dispatch(event, target)
        except Exception:
            log.exception("Could not schedule the diagnostics for a %s event", event.type)

    def _dispatch(self, event: IncidentEvent, target: ProbeTarget | None) -> None:
        settings = self.settings()
        if not settings.enabled or event.type not in {"opened", "updated"}:
            return
        incident_id = event.state.incident_id
        if incident_id is None:
            log.debug("Incident event %s has no stored incident to attach to", event.type)
            return
        host = self._host_for(event.target_id, target)
        if not host:
            return
        if event.type == "updated" and not self._updated_is_due(event, incident_id):
            return

        limit = self._limit_hit(event.target_id, incident_id, settings)
        if limit is not None:
            log.info(
                "diagnostics for incident %s skipped (%s)", incident_id, limit
            )
            self._record(
                incident_id=incident_id,
                target_id=event.target_id,
                status="error",
                error="rate_limited",
            )
            return

        self._last_run_at[event.target_id] = self._clock()
        task = asyncio.create_task(
            self._diagnose(incident_id, event.target_id, host, settings),
            name=f"diagnostics-{incident_id}",
        )
        # only a task that exists may hold a slot, and `_dispatch` never awaits,
        # so the done callback cannot run before the slot is taken
        self._running += 1
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._running = max(0, self._running - 1)
        if not task.cancelled() and task.exception() is not None:
            log.error("Diagnostics task failed", exc_info=task.exception())

    def _host_for(self, target_id: int, target: ProbeTarget | None) -> str | None:
        if target is None:
            try:
                target = quality_db.get_target(self._db_path, target_id)
            except Exception:
                log.warning("Could not read target %s for diagnostics", target_id, exc_info=True)
                return None
        if target is None or not target.host:
            return None
        return target.host.strip() or None

    def _updated_is_due(self, event: IncidentEvent, incident_id: int) -> bool:
        """An open incident is re-diagnosed only once, and only after a while.

        Rows written for refused triggers count here too: having already
        decided about this incident is exactly what must not be repeated every
        window.
        """
        started_at = event.state.started_at
        if not started_at:
            return False
        try:
            open_for = (_as_utc(event.window_end) - parse_dt(started_at)).total_seconds()
        except ValueError:
            log.warning("Incident %s has an unusable started_at %r", incident_id, started_at)
            return False
        if open_for < UPDATED_TRIGGER_AFTER_SECONDS:
            return False
        return not self._rows_for(incident_id)

    def _limit_hit(
        self, target_id: int, incident_id: int, settings: DiagnosticsSettings
    ) -> str | None:
        last = self._last_run_at.get(target_id)
        if last is not None and (self._clock() - last) < settings.min_interval_seconds:
            return "min_interval"
        executed = [row for row in self._rows_for(incident_id) if row.get("error") != "rate_limited"]
        if len(executed) >= settings.max_per_incident:
            return "max_per_incident"
        if self._running >= settings.max_concurrent:
            return "max_concurrent"
        return None

    def _rows_for(self, incident_id: int) -> list[dict[str, Any]]:
        try:
            return quality_db.query_diagnostics(self._db_path, incident_id=incident_id)
        except Exception:
            log.warning("Could not read the diagnostics of incident %s", incident_id, exc_info=True)
            return []

    # -- diagnosis (runs in its own task) ----------------------------------

    async def _diagnose(
        self, incident_id: int, target_id: int, host: str, settings: DiagnosticsSettings
    ) -> None:
        """The gateway first, so the target's hypotheses can use its result."""
        gateway_hops: list[dict[str, Any]] | None = None
        gateway = self._gateway_host()
        if gateway and gateway != host:
            gateway_hops = await self._mtr(
                incident_id=incident_id,
                target_id=None,
                host=gateway,
                role="gateway",
                settings=settings,
                gateway_hops=None,
            )
        await self._mtr(
            incident_id=incident_id,
            target_id=target_id,
            host=host,
            role="target",
            settings=settings,
            gateway_hops=gateway_hops,
        )

    def _gateway_host(self) -> str | None:
        try:
            gateway = self._gateway_host_getter()
        except Exception:
            log.warning("Could not read the gateway host for diagnostics", exc_info=True)
            return None
        return gateway.strip() if isinstance(gateway, str) and gateway.strip() else None

    async def _mtr(
        self,
        *,
        incident_id: int,
        target_id: int | None,
        host: str,
        role: str,
        settings: DiagnosticsSettings,
        gateway_hops: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        argv = ["mtr", "--report", "--json", "-c", str(settings.mtr_count), "-n", host]
        started_at = to_iso_z(self._wall_clock())
        began = self._clock()

        try:
            returncode, stdout, stderr = await self._subprocess_runner(
                argv, timeout=float(settings.mtr_timeout_seconds)
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            self._record(
                incident_id=incident_id,
                target_id=target_id,
                status="timeout",
                error=str(exc) or "timeout",
                started_at=started_at,
                duration_ms=(self._clock() - began) * 1000.0,
            )
            return None
        except Exception as exc:
            log.warning("mtr could not be run for %s", host, exc_info=True)
            self._record(
                incident_id=incident_id,
                target_id=target_id,
                status="error",
                error=f"mtr could not be run: {exc}",
                started_at=started_at,
                duration_ms=(self._clock() - began) * 1000.0,
            )
            return None

        duration_ms = (self._clock() - began) * 1000.0
        hops = parse_mtr_json(stdout)
        if not hops:
            self._record(
                incident_id=incident_id,
                target_id=target_id,
                status="error",
                error=_failure_reason(returncode, stderr),
                started_at=started_at,
                duration_ms=duration_ms,
                raw_output=stdout,
            )
            return None

        result = {
            "host": host,
            "role": role,
            "hops": hops,
            "hypotheses": hypotheses(hops, host, gateway_hops),
        }
        self._record(
            incident_id=incident_id,
            target_id=target_id,
            status="ok" if returncode == 0 else "error",
            error=None if returncode == 0 else _failure_reason(returncode, stderr),
            started_at=started_at,
            duration_ms=duration_ms,
            result_json=json.dumps(result, ensure_ascii=False),
            raw_output=stdout,
        )
        return hops

    def _record(
        self,
        *,
        incident_id: int,
        target_id: int | None,
        status: str,
        error: str | None = None,
        started_at: str | None = None,
        duration_ms: float | None = None,
        result_json: str | None = None,
        raw_output: str | None = None,
    ) -> None:
        try:
            quality_db.insert_diagnostic(
                self._db_path,
                incident_id=incident_id,
                target_id=target_id,
                tool="mtr",
                started_at=started_at or to_iso_z(self._wall_clock()),
                duration_ms=duration_ms,
                status=status,
                error=error,
                result_json=result_json,
                raw_output=raw_output,
            )
        except Exception:
            log.exception("Could not store the %s diagnostic of incident %s", status, incident_id)

    # -- lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        """Cancel every diagnostic still running (a stop must not wait 90 s)."""
        tasks, self._tasks = set(self._tasks), set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running = 0

    async def stop(self) -> None:
        await self.close()
