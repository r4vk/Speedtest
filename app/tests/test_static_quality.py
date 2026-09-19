"""Static-asset tests for the network-quality panel (Task 10, plan stage 7).

`quality.js` ships as a plain script with no build step and no test runner of
its own. These tests are the guardrail that keeps it wired to `index.html`
and syntactically valid, and exercise its pure helpers (picking the timeline
bucket size, building the stats sum row, treating an empty bucket as a gap)
the same way a browser would run them — through Node, not by re-implementing
the logic in Python.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
QUALITY_JS = (STATIC_DIR / "quality.js").read_text(encoding="utf-8")

# Every pattern below is a shape in quality.js that names an HTML element id
# as a string literal: direct qs()/getElementById() lookups, the generic
# setMsg() status-message helper, and the [id, config-key] tuples that drive
# the settings form. A typo in any of them is a silently dead control, so the
# contract test below checks all four shapes, not just the narrowest one.
_ID_PATTERNS = [
    re.compile(r'\bqs\(\s*"([^"]+)"\s*\)'),
    re.compile(r'getElementById\(\s*"([^"]+)"\s*\)'),
    re.compile(r'setMsg\(\s*"([^"]+)"'),
    re.compile(r'\[\s*"([a-zA-Z0-9-]+)"\s*,\s*"[a-zA-Z0-9_]+"\s*\]'),
]


def _referenced_ids(source: str) -> set[str]:
    ids: set[str] = set()
    for pattern in _ID_PATTERNS:
        ids.update(pattern.findall(source))
    return ids


def _html_ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    fallback = Path("/opt/homebrew/bin/node")
    if fallback.exists():
        return str(fallback)
    return None


NODE = _find_node()
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed on this machine")


def _run_node(program: str) -> subprocess.CompletedProcess:
    assert NODE is not None
    return subprocess.run([NODE, "-e", program], capture_output=True, text=True)


# ---------------------------------------------------------------------------
# wiring: quality.js loads after app.js and only references real ids
# ---------------------------------------------------------------------------


def test_quality_js_is_loaded_after_app_js():
    app_pos = INDEX_HTML.index("/static/app.js")
    quality_pos = INDEX_HTML.index("/static/quality.js")
    assert app_pos < quality_pos


def test_every_id_quality_js_looks_up_exists_in_index_html():
    referenced = _referenced_ids(QUALITY_JS)
    # A regression here (e.g. the extraction patterns stop matching anything
    # because the file was rewritten) would make the assertion below
    # vacuously true, so guard against that first.
    assert len(referenced) > 20
    html_ids = _html_ids(INDEX_HTML)
    missing = sorted(referenced - html_ids)
    assert not missing, f"quality.js references ids missing from index.html: {missing}"


# ---------------------------------------------------------------------------
# syntax
# ---------------------------------------------------------------------------


@requires_node
def test_quality_js_is_syntactically_valid():
    result = subprocess.run([NODE, "--check", str(STATIC_DIR / "quality.js")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@requires_node
def test_app_js_is_syntactically_valid():
    result = subprocess.run([NODE, "--check", str(STATIC_DIR / "app.js")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# served page
# ---------------------------------------------------------------------------


def test_served_page_mentions_measurement_source_and_panel_title(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "pomiar z NAS-a po kablu" in body
    assert "Jakość łącza" in body


# ---------------------------------------------------------------------------
# pure helpers, exercised through Node exactly as a browser would run them
# ---------------------------------------------------------------------------


@requires_node
@pytest.mark.parametrize(
    "range_seconds, expected_bucket_seconds",
    [
        (3600, 60),  # 1 h -> 60 s buckets (60 points)
        (86400, 180),  # 24 h -> 3 min buckets (480 points)
        (2592000, 4320),  # 30 d -> 72 min buckets (600 points, the cap)
    ],
)
def test_pick_bucket_seconds_matches_the_600_point_budget(range_seconds, expected_bucket_seconds):
    program = f"{QUALITY_JS}\nconsole.log(Quality.pickBucketSeconds({range_seconds}));"
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == expected_bucket_seconds
    assert range_seconds / expected_bucket_seconds <= 600


@requires_node
def test_pick_bucket_seconds_never_goes_below_one_minute():
    program = f"{QUALITY_JS}\nconsole.log(JSON.stringify([Quality.pickBucketSeconds(0), Quality.pickBucketSeconds(-5), Quality.pickBucketSeconds(1)]));"
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == [60, 60, 60]


@requires_node
def test_build_sum_row_uses_counters_not_the_mean_of_percentages():
    # Target A: 1000 attempts, 1 % loss. Target B: 10 attempts, 50 % loss.
    # Averaging the two percentages (a bug this guards against) gives
    # 25.5 %; deriving loss from summed counters gives ~1.49 %, which is
    # what the spec (Sigma timeouts / Sigma(ok+timeouts)) requires.
    entries = [
        {"stats": {"attempts": 1000, "ok": 990, "timeouts": 10, "errors": 0}},
        {"stats": {"attempts": 10, "ok": 5, "timeouts": 5, "errors": 0}},
    ]
    program = f"{QUALITY_JS}\nconsole.log(JSON.stringify(Quality.buildSumRow({json.dumps(entries)})));"
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    sum_row = json.loads(result.stdout.strip())
    assert sum_row["attempts"] == 1010
    assert sum_row["ok"] == 995
    assert sum_row["timeouts"] == 15
    assert sum_row["errors"] == 0
    assert sum_row["loss_pct"] == pytest.approx(15 / 1010 * 100, rel=1e-9)
    assert sum_row["loss_pct"] < 2.0  # nowhere near the naive 25.5 % mean


@requires_node
def test_build_sum_row_of_no_entries_is_no_data_not_zero():
    program = f"{QUALITY_JS}\nconsole.log(JSON.stringify(Quality.buildSumRow([])));"
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    sum_row = json.loads(result.stdout.strip())
    assert sum_row == {"attempts": 0, "ok": 0, "timeouts": 0, "errors": 0, "loss_pct": None}


@requires_node
def test_loss_value_for_point_treats_zero_attempts_as_a_gap():
    program = (
        f"{QUALITY_JS}\n"
        "console.log(JSON.stringify([\n"
        "  Quality.lossValueForPoint({attempts: 0, loss_pct: 0}),\n"
        "  Quality.lossValueForPoint({attempts: 4, loss_pct: 25}),\n"
        "  Quality.lossValueForPoint(null),\n"
        "]));"
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == [None, 25, None]


# ---------------------------------------------------------------------------
# lost measurements are visible in the panel (review finding I5)
# ---------------------------------------------------------------------------


@requires_node
def test_scheduler_warnings_report_dropped_rows_and_loop_restarts():
    status = {
        "scheduler": {
            "dropped_rows": 7,
            "flush_errors": 2,
            "skipped_ticks": {"1": 3, "2": 4},
            "restarts": {"1": 1},
        }
    }
    program = f"{QUALITY_JS}\nconsole.log(JSON.stringify(Quality.schedulerWarnings({json.dumps(status)})));"
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    warnings = json.loads(result.stdout.strip())
    assert warnings["lost"] == "utracone pomiary: 7 (błędy zapisu: 2)"
    assert warnings["loops"] == "pominięte ticki: 7 · restarty pętli: 1"


@requires_node
def test_scheduler_warnings_stay_silent_when_nothing_was_lost():
    cases = [
        {"scheduler": {"dropped_rows": 0, "flush_errors": 0, "skipped_ticks": {}, "restarts": {}}},
        {"scheduler": {}},
        {},
    ]
    program = f"{QUALITY_JS}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(Quality.schedulerWarnings)));"
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == [{"lost": None, "loops": None}] * 3


@requires_node
def test_a_write_failure_alone_is_enough_to_warn():
    status = {"scheduler": {"dropped_rows": 0, "flush_errors": 3}}
    program = f"{QUALITY_JS}\nconsole.log(JSON.stringify(Quality.schedulerWarnings({json.dumps(status)})));"
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip())["lost"] == "utracone pomiary: 0 (błędy zapisu: 3)"
