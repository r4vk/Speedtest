"""The printable report for the ISP (design spec §13).

Every number here comes from the functions that serve `/api/quality/*`
(`quality_views.target_stats_entries`, `stats.bucket_rows`, `coverage.coverage`,
the `quality_db` queries), so the report cannot drift from the panel or the
CSV exports. The page is self-contained: inline CSS, inline SVG, no CDN, no
script — it has to survive being printed to PDF and mailed to an operator.

What the report must never do is fill a gap: a range without measurements says
"brak danych", and a range whose raw rows were pruned says so and falls back
to the hourly aggregates, labelled as such.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from jinja2 import Environment, PackageLoader, select_autoescape

from . import quality_db
from .coverage import clip_to_observed, coverage, observed_intervals
from .db import TimeRange, query_connectivity_periods
from .quality_settings import INCIDENT_THRESHOLD_KEYS, parse_settings, read_quality_settings
from .quality_views import (
    MEASURED_FROM,
    MIN_BUCKET_SECONDS,
    incident_span,
    incident_windows,
    legacy_tcp_counters,
    load_test_summaries,
    local_iso,
    range_payload,
    retention_cutoff,
    target_payload,
    target_stats_entries,
    tz_name,
)
from .stats import bucket_rows, compute_stats
from .time_utils import parse_dt, to_iso_z, to_local_display

#: The device these measurements describe (spec §13.1).
DEVICE = "NAS, kabel"

#: How the link is probed — printed under the header so the reader knows what
#: the numbers are made of.
PROBE_METHOD = (
    "Sondy wykonywane z NAS-a po kablu: ICMP echo, zestawienie połączenia TCP, "
    "zapytanie DNS i żądanie HTTPS. Każda próba jest zapisywana osobno; straty "
    "liczone są wyłącznie z prób zakończonych odpowiedzią lub timeoutem."
)

#: Fixed limitations of the measurement (spec §13.7). They are part of the
#: report: a number without them invites a conclusion the data cannot support.
LIMITATIONS: tuple[str, ...] = (
    "Rozdzielczość pomiaru wyznacza interwał sondy — zdarzenia krótsze niż interwał "
    "mogą pozostać niewidoczne.",
    "Pomiar wykonywany jest z NAS-a po kablu i opisuje ścieżkę NAS → router → internet; "
    "sam w sobie nie jest dowodem winy dostawcy.",
    "Brak odpowiedzi pojedynczego przeskoku w MTR nie oznacza awarii — routery często "
    "nie odpowiadają na ICMP lub traktują go niskim priorytetem.",
    "Pomiar nie rozstrzyga kierunku utraty pakietów: nie wiadomo, czy pakiet przepadł "
    "w drodze do celu, czy w drodze powrotnej.",
    "Timeout oznacza brak odpowiedzi w skonfigurowanym progu, a nie absolutny brak "
    "odpowiedzi; progi podano w tabeli konfiguracji.",
)

#: Polish label of the data source of a table row (spec §14).
DATA_SOURCE_LABELS = {
    "raw": "pomiar surowy",
    "aggregates": "agregaty godzinowe",
    "none": "brak danych",
}

#: Charts stay readable in print: at most this many points per series.
MAX_CHART_POINTS = 300

#: Colour-blind friendly, prints legibly in greyscale too.
CHART_COLORS = ("#1b6ca8", "#c1440e", "#2e7d32", "#6a1b9a", "#b58900", "#00838f")

#: How far before a load test the undisturbed baseline is taken (spec §10).
LOAD_TEST_BASELINE_SECONDS = 300.0


# ---------------------------------------------------------------------------
# SVG charts (pure string building — no data access, no clock)
# ---------------------------------------------------------------------------

def _segments(points: Sequence[tuple[str, float | None]]) -> list[list[tuple[int, float]]]:
    """Split a series on its missing values: a gap must break the line."""
    segments: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for index, (_label, value) in enumerate(points):
        if value is None:
            if current:
                segments.append(current)
                current = []
            continue
        current.append((index, float(value)))
    if current:
        segments.append(current)
    return segments


def svg_line_chart(
    series: Sequence[Mapping[str, Any]],
    *,
    width: int,
    height: int,
    y_label: str,
    y_max: float | None = None,
) -> str:
    """An inline SVG line chart of ``series`` — pure, deterministic, no assets.

    ``series`` is a list of ``{"label": str, "points": [(x_label, value|None)]}``.
    A ``None`` value is a hole in the measurement and breaks the polyline; it
    is never interpolated over.
    """
    left, right, top = 52, 12, 14
    legend_columns = max(1, (width - left - right) // 200)
    legend_rows = -(-len(series) // legend_columns) if series else 0
    bottom = 26 + legend_rows * 15
    plot_width = max(1, width - left - right)
    plot_height = max(1, height - top - bottom)

    all_values = [
        float(value)
        for entry in series
        for _label, value in entry.get("points", [])
        if value is not None
    ]
    count = max((len(entry.get("points", [])) for entry in series), default=0)
    top_value = y_max if y_max is not None else (max(all_values) if all_values else 1.0)
    if top_value <= 0:
        top_value = 1.0

    def x_of(index: int) -> float:
        if count <= 1:
            return left + plot_width / 2
        return left + plot_width * index / (count - 1)

    def y_of(value: float) -> float:
        return top + plot_height * (1 - min(value, top_value) / top_value)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" class="chart">'
    ]
    # frame and y axis ticks
    parts.append(
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" '
        f'class="chart-plot"/>'
    )
    for fraction in (0.0, 0.5, 1.0):
        value = top_value * fraction
        y = y_of(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" class="chart-grid"/>')
        parts.append(
            f'<text x="{left - 6}" y="{y + 3:.1f}" text-anchor="end" class="chart-tick">'
            f"{html.escape(_format_number(value))}</text>"
        )
    parts.append(
        f'<text x="4" y="{top + 8}" class="chart-axis">{html.escape(y_label)}</text>'
    )

    if not all_values:
        parts.append(
            f'<text x="{left + plot_width / 2:.1f}" y="{top + plot_height / 2:.1f}" '
            f'text-anchor="middle" class="chart-empty">brak danych</text>'
        )

    for index, entry in enumerate(series):
        color = CHART_COLORS[index % len(CHART_COLORS)]
        for segment in _segments(entry.get("points", [])):
            coordinates = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in segment)
            parts.append(
                f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="1.6"/>'
            )
        legend_x = left + (index % legend_columns) * 200
        legend_y = top + plot_height + 30 + (index // legend_columns) * 15
        parts.append(f'<rect x="{legend_x}" y="{legend_y - 8}" width="10" height="10" fill="{color}"/>')
        parts.append(
            f'<text x="{legend_x + 14}" y="{legend_y}" class="chart-legend">'
            f"{html.escape(str(entry.get('label', '')))}</text>"
        )

    # x axis labels: first, middle and last measured moment
    labels = _x_labels(series, count)
    for index, label in labels:
        anchor = "start" if index == 0 else ("end" if index == count - 1 else "middle")
        parts.append(
            f'<text x="{x_of(index):.1f}" y="{top + plot_height + 14}" text-anchor="{anchor}" '
            f'class="chart-tick">{html.escape(label)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _x_labels(series: Sequence[Mapping[str, Any]], count: int) -> list[tuple[int, str]]:
    if count == 0:
        return []
    points = next((entry["points"] for entry in series if entry.get("points")), [])
    if not points:
        return []
    indexes = sorted({0, count // 2, count - 1})
    return [(index, str(points[index][0])) for index in indexes if index < len(points)]


def _format_number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

def _chart_bucket_seconds(start: datetime, end: datetime) -> float:
    span = max(0.0, (end - start).total_seconds())
    needed = span / MAX_CHART_POINTS if span > 0 else 0.0
    return float(max(MIN_BUCKET_SECONDS, needed))


def _chart_label(dt: datetime, *, include_date: bool) -> str:
    """``HH:MM``, or ``MM-DD HH:MM`` once the span exceeds 24 h (finding 6):

    a bare time is ambiguous once a chart's x axis crosses midnight more than
    once, and the report is exactly the document that gets printed and read
    without the page it came from.
    """
    text = to_local_display(dt)  # "YYYY-MM-DD HH:MM:SS"
    return text[5:16] if include_date else text[11:16]


def _chart_series(
    db_path: str,
    start: datetime,
    end: datetime,
    targets: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
    """Loss and p95 series per target, from the timeline's own bucket function."""
    bucket = _chart_bucket_seconds(start, end)
    include_date = (end - start) > timedelta(hours=24)
    loss: list[dict[str, Any]] = []
    p95: list[dict[str, Any]] = []
    for target in targets:
        rows = quality_db.query_probe_results(
            db_path, to_iso_z(start), to_iso_z(end), target_id=target.id
        )
        # `include_partial=True`: the chart covers the whole `[start, end]`
        # exactly like the stats and the timeline (finding 1), so a reader
        # comparing the picture with the numbers above it sees the same range.
        points = bucket_rows(rows, bucket, start, end, include_partial=True)
        labels = [_chart_label(parse_dt(point["t"]), include_date=include_date) for point in points]
        loss.append(
            {
                "label": target.name,
                "points": [(label, point["loss_pct"]) for label, point in zip(labels, points)],
            }
        )
        p95.append(
            {
                "label": target.name,
                "points": [(label, point["p95"]) for label, point in zip(labels, points)],
            }
        )
    return loss, p95, bucket


def _availability(db_path: str, start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
    """Downtime from `connectivity_periods`, clipped to the observed time (§9)."""
    periods = query_connectivity_periods(
        db_path, tr=TimeRange(start_iso=to_iso_z(start), end_iso=to_iso_z(end)), is_up=False
    )
    observed = observed_intervals(db_path, start, end)
    downtime = 0.0
    items: list[dict[str, Any]] = []
    for period in periods:
        period_start = parse_dt(period["started_at"])
        period_end = parse_dt(period["ended_at"]) if period["ended_at"] else now
        seconds = 0.0
        for piece_start, piece_end in clip_to_observed([(period_start, period_end)], observed):
            seconds += (piece_end - piece_start).total_seconds()
        downtime += seconds
        items.append(
            {
                "started_at": local_iso(period_start),
                "ended_at": local_iso(period_end) if period["ended_at"] else None,
                "observed_seconds": seconds,
            }
        )
    observed_seconds = sum((right - left).total_seconds() for left, right in observed)
    return {
        "observed_seconds": observed_seconds,
        "downtime_seconds": downtime,
        "downtime_pct": (downtime / observed_seconds * 100.0) if observed_seconds > 0 else None,
        "incident_count": len(items),
        "periods": items,
    }


def _incident_entries(
    db_path: str, start: datetime, end: datetime, now: datetime, names: Mapping[int, str]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in quality_db.query_incidents(db_path, to_iso_z(start), to_iso_z(end)):
        span_start, span_end = incident_span(row, now)
        entries.append(
            {
                "id": row["id"],
                "target_name": names.get(int(row["target_id"]), str(row["target_id"])),
                "protocol": row["protocol"],
                "kind": row["kind"],
                "started_at": local_iso(row["started_at"]),
                "ended_at": local_iso(row["ended_at"]),
                "closed_at": local_iso(row["closed_at"]),
                "close_reason": row["close_reason"],
                "peak_loss_pct": row["peak_loss_pct"],
                "peak_p95_rtt_ms": row["peak_p95_rtt_ms"],
                "longest_fail_streak": row["longest_fail_streak"],
                "windows_degraded": row["windows_degraded"],
                "windows": incident_windows(row),
                "diagnostics": [
                    {
                        "tool": diag["tool"],
                        "started_at": local_iso(diag["started_at"]),
                        "status": diag["status"],
                        "error": diag["error"],
                    }
                    for diag in quality_db.query_diagnostics(db_path, incident_id=int(row["id"]))
                ],
                "annotations": [
                    {
                        "at": local_iso(annotation["at"]),
                        "label": annotation["label"],
                        "note": annotation["note"],
                        "source": annotation["source"],
                    }
                    for annotation in quality_db.query_annotations(
                        db_path, to_iso_z(span_start), to_iso_z(span_end)
                    )
                ],
            }
        )
    return entries


def _latency_under_load(db_path: str, row: Mapping[str, Any], targets: Sequence[Any]) -> list[dict[str, Any]]:
    """ICMP p95 during the test against the five minutes before it (spec §10)."""
    if not row.get("started_at"):
        return []
    start = parse_dt(str(row["started_at"]))
    end = parse_dt(str(row["ended_at"])) if row.get("ended_at") else start
    baseline_start = start - timedelta(seconds=LOAD_TEST_BASELINE_SECONDS)
    # `query_probe_results` is inclusive on both ends, so ending the baseline
    # exactly at `start` would count a probe sitting on that instant in both
    # windows (finding 5): pull the baseline's end back by one millisecond.
    baseline_end = start - timedelta(milliseconds=1)
    comparison: list[dict[str, Any]] = []
    for target in targets:
        if str(target.protocol) != "icmp":
            continue
        during = compute_stats(
            quality_db.query_probe_results(db_path, to_iso_z(start), to_iso_z(end), target_id=target.id)
        )
        before = compute_stats(
            quality_db.query_probe_results(
                db_path, to_iso_z(baseline_start), to_iso_z(baseline_end), target_id=target.id
            )
        )
        if during.attempts == 0 and before.attempts == 0:
            continue
        comparison.append(
            {
                "target_name": target.name,
                "p95_during_ms": during.rtt_p95_ms,
                "p95_baseline_ms": before.rtt_p95_ms,
                "loss_during_pct": during.loss_pct,
                "loss_baseline_pct": before.loss_pct,
            }
        )
    return comparison


def build_report_model(
    db_path: str,
    start: datetime,
    end: datetime,
    *,
    app_version: str,
    now: datetime,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble every number of the report from the API's own functions (§13)."""
    # A caller may pass a partial mapping; the spec's defaults fill the rest,
    # so a missing key cannot turn into a KeyError halfway through a report.
    values = read_quality_settings(db_path) if settings is None else {**parse_settings({}), **settings}
    targets = quality_db.list_targets(db_path)
    names = {target.id: target.name for target in targets}

    entries = target_stats_entries(db_path, start, end, targets)
    for entry in entries:
        entry["data_source_label"] = DATA_SOURCE_LABELS[entry["data_source"]]

    coverage_result = coverage(db_path, start, end)
    coverage_result["gaps"] = [
        {"from": local_iso(gap["from"]), "to": local_iso(gap["to"]), "reason": gap["reason"]}
        for gap in coverage_result["gaps"]
    ]

    cutoff = retention_cutoff(db_path, now, values)
    retention_note = None
    if start < cutoff:
        retention_note = (
            f"Surowe dane niedostępne (retencja) przed {to_local_display(cutoff)} — "
            "dla wcześniejszej części zakresu tabele pochodzą z agregatów godzinowych."
        )

    load_tests = []
    for row in quality_db.query_load_tests(db_path, to_iso_z(start), to_iso_z(end)):
        load_tests.append(
            {
                "id": row["id"],
                "started_at": local_iso(row["started_at"]),
                "ended_at": local_iso(row["ended_at"]),
                "kind": row["kind"],
                "server": row["server"],
                "status": row["status"],
                "error": row["error"],
                "summaries": load_test_summaries(row),
                "latency_under_load": _latency_under_load(db_path, row, targets),
            }
        )

    loss_series, p95_series, chart_bucket = _chart_series(db_path, start, end, targets)

    return {
        "generated_at": local_iso(now),
        "range": range_payload(start, end),
        "tz": tz_name(),
        "app_version": app_version,
        "device": DEVICE,
        "measured_from": MEASURED_FROM,
        "probe_method": PROBE_METHOD,
        "config": {
            "targets": [target_payload(target) for target in targets],
            "incident_thresholds": {key: values[key] for key in INCIDENT_THRESHOLD_KEYS},
            "probe_method": PROBE_METHOD,
        },
        "coverage": coverage_result,
        "retention_note": retention_note,
        "availability": _availability(db_path, start, end, now),
        "targets": entries,
        "legacy_tcp": legacy_tcp_counters(db_path, start, end),
        "incidents": _incident_entries(db_path, start, end, now, names),
        "load_tests": load_tests,
        "annotations": [
            {
                "at": local_iso(row["at"]),
                "label": row["label"],
                "note": row["note"],
                "source": row["source"],
            }
            for row in quality_db.query_annotations(db_path, to_iso_z(start), to_iso_z(end))
        ],
        "charts": {
            "bucket_seconds": chart_bucket,
            "loss": svg_line_chart(loss_series, width=900, height=240, y_label="strata [%]", y_max=100.0),
            "p95": svg_line_chart(p95_series, width=900, height=240, y_label="p95 RTT [ms]"),
        },
        "limitations": list(LIMITATIONS),
        "has_data": any(entry["stats"]["attempts"] > 0 for entry in entries),
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("speedtest_app", "templates"),
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["number"] = _format_number
    env.filters["seconds"] = _format_seconds
    return env


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    seconds = int(round(float(value)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min {secs} s"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def render_report(model: Mapping[str, Any]) -> str:
    """Render the model into the self-contained HTML page (spec §13)."""
    return _environment().get_template("report.html").render(**model)
