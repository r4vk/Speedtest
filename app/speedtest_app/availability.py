"""Availability and quality evaluation (design spec §8).

Three orthogonal notions are kept apart here, exactly as in the spec: the
*outcome* of a single attempt (``ok``/``timeout``/``error``), the *availability*
of the connection (``up``/``down``/``no_data``) and the *quality* of it
(``ok``/``degraded``/``unknown``). ``error`` attempts are never loss and never
success — a window nobody could measure is ``no_data``, not ``down``.

:func:`evaluate` is pure. :class:`AvailabilityTracker` is the only part that
writes: it keeps ``connectivity_periods`` and the recovery e-mail behaving
exactly as the legacy connectivity loop did, plus the new rule that unmeasured
time ends the open period instead of extending it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, Sequence

from . import quality_db
from .config import AppConfig
from .db import (
    end_current_connectivity_period,
    get_current_connectivity_period,
    mark_connectivity_period_expected,
    record_connectivity,
)
from .email_notify import send_outage_notification
from .expected_windows import ExpectedWindow
from .expected_windows import match as match_expected_window
from .expected_windows import parse_windows
from .probe_types import Outcome, ProbeTarget
from .time_utils import parse_dt, to_iso_z, to_local_display, utc_now

log = logging.getLogger(__name__)

Availability = Literal["up", "down", "no_data"]
Quality = Literal["ok", "degraded", "unknown"]

#: Only these target kinds prove that *the internet* works. The gateway
#: answering says nothing about the path beyond the router (spec §8).
AVAILABILITY_KINDS: frozenset[str] = frozenset({"internet", "tcp"})

#: Kind whose incidents are reported as a LAN problem instead of a quality drop.
LAN_KIND = "gateway"


def _setting(values: Mapping[str, str], key: str, default: Any, cast: Callable[[str], Any]) -> Any:
    """Read one setting; missing, empty or unparsable keeps the default."""
    raw = values.get(key)
    if raw is None:
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class AvailabilitySettings:
    """Cadence and window of the availability evaluator (spec §8)."""

    eval_seconds: float = 5.0
    window_seconds: float = 10.0
    #: Shared with the incident engine: fewer measurable attempts → no verdict.
    min_samples: int = 5

    @staticmethod
    def from_settings(values: Mapping[str, str]) -> "AvailabilitySettings":
        defaults = AvailabilitySettings()
        return AvailabilitySettings(
            eval_seconds=_setting(values, "availability_eval_seconds", defaults.eval_seconds, float),
            window_seconds=_setting(
                values, "availability_window_seconds", defaults.window_seconds, float
            ),
            min_samples=_setting(values, "incident_min_samples", defaults.min_samples, int),
        )


def _outcome(row: Any) -> str:
    value = row.get("outcome") if isinstance(row, Mapping) else getattr(row, "outcome", None)
    return str(value) if value is not None else ""


def _started_at(row: Any) -> datetime | None:
    value = row.get("started_at") if isinstance(row, Mapping) else getattr(row, "started_at", None)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        return parse_dt(str(value))
    except ValueError:
        return None


def evaluate(
    recent_by_target: Mapping[int, Sequence[Any]],
    targets: Sequence[ProbeTarget],
    *,
    now: datetime,
    settings: AvailabilitySettings,
) -> Availability:
    """Availability over the last ``window_seconds`` (spec §8).

    Only enabled ``internet``/``tcp`` targets are considered. Rows older than
    the window are ignored; rows the caller believes are recent but carry a
    timestamp slightly ahead of ``now`` are kept, because a clock skew of
    milliseconds must not hide a successful attempt.
    """
    considered = {
        target.id for target in targets if target.enabled and target.kind in AVAILABILITY_KINDS
    }
    if not considered:
        return "no_data"

    cutoff = now.timestamp() - max(float(settings.window_seconds), 0.0)
    ok = 0
    measurable = 0
    for target_id, rows in recent_by_target.items():
        if target_id not in considered:
            continue
        for row in rows:
            started = _started_at(row)
            if started is None or started.timestamp() < cutoff:
                continue
            outcome = _outcome(row)
            if outcome == Outcome.OK:
                ok += 1
                measurable += 1
            elif outcome == Outcome.TIMEOUT:
                measurable += 1

    if ok > 0:
        return "up"
    if measurable >= max(int(settings.min_samples), 1):
        return "down"
    return "no_data"


def quality_state(
    availability: str,
    open_incidents: Sequence[Mapping[str, Any]],
    targets: Sequence[ProbeTarget],
) -> tuple[Quality, bool]:
    """``(quality, lan_degraded)`` from availability and the open incidents (spec §8).

    An open incident on an internet/tcp target is the strongest signal we have,
    so it outranks a missing availability verdict. Gateway incidents never make
    the internet "degraded": they are reported separately as ``lan_degraded``.
    """
    kinds = {target.id: target.kind for target in targets}
    degraded = False
    lan_degraded = False
    for incident in open_incidents:
        target_id = incident.get("target_id")
        kind = kinds.get(int(target_id)) if target_id is not None else None
        if kind in AVAILABILITY_KINDS:
            degraded = True
        elif kind == LAN_KIND:
            lan_degraded = True

    if degraded:
        return "degraded", lan_degraded
    if availability == "no_data":
        return "unknown", lan_degraded
    return "ok", lan_degraded


#: Default budget for an unmeasurable stretch inside an outage; the engine
#: replaces it with the live `incident_no_data_close_seconds` setting (spec §7).
DEFAULT_NO_DATA_CLOSE_SECONDS = 300.0


class AvailabilityTracker:
    """Persists availability transitions and sends the recovery e-mail.

    The last state is adopted from the open ``connectivity_periods`` row at
    construction time, so a restart in the middle of an outage neither
    duplicates the period nor invents a recovery e-mail.

    An outage that fits an expected window is flagged on the period and sent
    no e-mail: it is the reboot somebody scheduled, not news (spec §4).
    """

    def __init__(
        self,
        db_path: str,
        cfg: AppConfig,
        *,
        notifier: Callable[[AppConfig, str, str, float], Any] = send_outage_notification,
        clock: Callable[[], datetime] = utc_now,
        no_data_close_seconds: float = DEFAULT_NO_DATA_CLOSE_SECONDS,
    ) -> None:
        self._db_path = db_path
        self._cfg = cfg
        self._notifier = notifier
        self._clock = clock
        #: Kept in step with `incident_no_data_close_seconds` by the engine.
        self.no_data_close_seconds = float(no_data_close_seconds)
        self._last_state: str | None = None
        self._outage_started_at: str | None = None
        self._no_data_since: str | None = None
        self._notifications: set[asyncio.Task[None]] = set()
        self._adopt_open_period()

    # -- state -------------------------------------------------------------

    @property
    def last_state(self) -> str | None:
        """Last applied state, or ``None`` before the first evaluation."""
        return self._last_state

    def _adopt_open_period(self) -> None:
        try:
            current = get_current_connectivity_period(self._db_path)
        except Exception:
            log.warning("Could not read the current availability period", exc_info=True)
            return
        if current is None:
            return
        is_up = bool(current["is_up"])
        self._last_state = "up" if is_up else "down"
        if not is_up:
            self._outage_started_at = current["started_at"]

    # -- transitions -------------------------------------------------------

    def apply(self, state: str, now: datetime | None = None) -> str | None:
        """Persist ``state`` and return the state it replaced (for logging)."""
        previous = self._last_state
        now_iso = to_iso_z(now if now is not None else self._clock())
        if state == "no_data":
            self._apply_no_data(now_iso)
        elif state == "up":
            record_connectivity(self._db_path, is_up=True, now_iso=now_iso)
            self._on_up(now_iso)
        elif state == "down":
            record_connectivity(self._db_path, is_up=False, now_iso=now_iso)
            self._on_down(now_iso)
        else:  # pragma: no cover - the evaluator only returns the three states
            raise ValueError(f"unknown availability state: {state!r}")
        self._last_state = state
        return previous

    def _apply_no_data(self, now_iso: str) -> None:
        """Close the open period; unobserved time belongs to nobody (spec §8).

        A running outage keeps its start mark across the gap — a few windows
        nobody could measure do not make the outage less real — until the gap
        itself reaches ``no_data_close_seconds``, the same budget at which the
        incident engine gives up on a key (spec §7). Past that we no longer
        know what happened, so the eventual recovery is not reported.
        """
        end_current_connectivity_period(self._db_path, now_iso)
        if self._no_data_since is None:
            self._no_data_since = now_iso
        if self._outage_started_at is None:
            return
        gap_seconds = (parse_dt(now_iso) - parse_dt(self._no_data_since)).total_seconds()
        if gap_seconds < self.no_data_close_seconds:
            return
        log.warning(
            "Availability has been unknown for %.0f s during an outage, "
            "dropping its start mark: the recovery cannot be timed",
            gap_seconds,
        )
        self._outage_started_at = None

    def _on_down(self, now_iso: str) -> None:
        self._no_data_since = None
        if self._outage_started_at is not None:
            return
        self._outage_started_at = now_iso
        log.info("Internet outage detected at %s", to_local_display(parse_dt(now_iso)))

    def _on_up(self, now_iso: str) -> None:
        self._no_data_since = None
        started_at = self._outage_started_at
        if started_at is None:
            return
        self._outage_started_at = None
        ended_local = to_local_display(parse_dt(now_iso))
        started_local = to_local_display(parse_dt(started_at))
        log.info("Internet restored at %s", ended_local)
        window = self._expected_window(started_at, now_iso)
        if window is not None:
            try:
                mark_connectivity_period_expected(
                    self._db_path,
                    started_at_iso=started_at,
                    expected=True,
                    source="rule",
                    rule_id=window.id,
                )
            except Exception:
                log.warning("Could not flag the expected outage period", exc_info=True)
            log.info(
                "Outage %s–%s fits the expected window %r, no e-mail sent",
                started_local,
                ended_local,
                window.name,
            )
            return
        if not self._cfg.smtp_enabled:
            return
        duration_seconds = (parse_dt(now_iso) - parse_dt(started_at)).total_seconds()
        if duration_seconds < self._cfg.smtp_min_outage_seconds:
            log.info(
                "Outage lasted %.0f seconds (< %d), skipping email notification",
                duration_seconds,
                self._cfg.smtp_min_outage_seconds,
            )
            return
        log.info(
            "Outage lasted %.0f seconds (>= %d), sending email notification",
            duration_seconds,
            self._cfg.smtp_min_outage_seconds,
        )
        self._dispatch(started_local, ended_local, duration_seconds)

    def _expected_window(self, started_at: str, ended_at: str) -> ExpectedWindow | None:
        """The rule covering this closed outage, or ``None`` (spec §4).

        The rules are read here rather than cached with the settings: outages
        end rarely, and a window saved a minute ago should already hold.

        A failure to read them is treated as "no rules": the mail goes out and
        the row stays unflagged. Losing the one message that says the internet
        was down is worse than sending one nobody needed, and an unflagged
        outage can still be marked by hand afterwards.

        ``target_id=None`` because a connectivity period is a verdict about the
        connection as a whole, so only an unscoped rule can cover it.
        """
        try:
            windows = parse_windows(
                quality_db.list_expected_windows(self._db_path, enabled_only=True)
            )
            if not windows:
                return None
            return match_expected_window(
                windows, parse_dt(started_at), parse_dt(ended_at), target_id=None
            )
        except Exception:
            log.warning("Could not evaluate the expected windows", exc_info=True)
            return None

    # -- notification ------------------------------------------------------

    def _dispatch(self, started_local: str, ended_local: str, duration_seconds: float) -> None:
        """Send the mail off the event loop; a failure never costs a transition."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._notify(started_local, ended_local, duration_seconds)
            return
        task = loop.create_task(
            asyncio.to_thread(self._notify, started_local, ended_local, duration_seconds)
        )
        self._notifications.add(task)
        task.add_done_callback(self._notifications.discard)

    def _notify(self, started_local: str, ended_local: str, duration_seconds: float) -> None:
        try:
            self._notifier(self._cfg, started_local, ended_local, duration_seconds)
        except Exception:
            log.warning("Outage notification failed", exc_info=True)

    async def drain(self) -> None:
        """Await the notifications still in flight (shutdown, tests)."""
        pending = list(self._notifications)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
