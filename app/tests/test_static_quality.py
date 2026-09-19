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
    # `["q-cfg-…", "klucz"]` oraz `["q-cfg-…", "klucz", { allowEmpty: true }]`
    re.compile(r'\[\s*"(q-cfg-[a-zA-Z0-9-]+)"\s*,\s*"[a-zA-Z0-9_]+"\s*[,\]]'),
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


# ---------------------------------------------------------------------------
# _escHtml is safe in an attribute (review minor)
# ---------------------------------------------------------------------------


def _esc_html_source() -> str:
    """The `_escHtml` helper alone: app.js as a whole needs a browser."""
    match = re.search(r"const _ESC_HTML = .*?\nfunction _escHtml\(str\) \{.*?\n\}", APP_JS, re.S)
    assert match, "the _escHtml helper could not be located in app.js"
    return match.group(0)


@requires_node
def test_esc_html_escapes_quotes_so_attribute_use_is_safe():
    # quality.js puts the result inside title="…", so a bare `"` would end
    # the attribute; the old textContent/innerHTML trick did not escape it.
    payload = '<b>a</b> "quoted" \'single\' & more'
    program = (
        f"{_esc_html_source()}\n"
        f"console.log(JSON.stringify(_escHtml({json.dumps(payload)})));"
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == (
        "&lt;b&gt;a&lt;/b&gt; &quot;quoted&quot; &#39;single&#39; &amp; more"
    )


@requires_node
def test_esc_html_turns_nullish_into_an_empty_string():
    program = (
        f"{_esc_html_source()}\n"
        "console.log(JSON.stringify([_escHtml(null), _escHtml(undefined), _escHtml(0)]));"
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == ["", "", "0"]


# ---------------------------------------------------------------------------
# settings form: an unpopulated field must never become a saved value
#
# `app.js` starts `loadConfig()` while the browser is still fetching
# `quality.js`, so `Quality.applyConfig()` can be skipped entirely (the
# `typeof Quality !== "undefined"` guard loses the race). The form then holds
# empty inputs, and a plain `Number("")` turns each of them into a `0` that
# the API either rejects (422) or — worse, for the thresholds whose lower
# bound is 0 — stores as a real setting.
# ---------------------------------------------------------------------------


def _payload_program(values: dict[str, str], checked: dict[str, bool] | None = None) -> str:
    """Run `buildConfigPayload` over a table of raw field values."""
    return (
        f"{QUALITY_JS}\n"
        f"const values = {json.dumps(values)};\n"
        f"const checked = {json.dumps(checked or {})};\n"
        "const payload = Quality.buildConfigPayload(\n"
        "  (id) => (id in values ? values[id] : undefined),\n"
        "  (id) => (id in checked ? checked[id] : undefined),\n"
        ");\n"
        "console.log(JSON.stringify(payload));"
    )


@requires_node
def test_an_empty_number_field_is_left_out_instead_of_being_sent_as_zero():
    program = _payload_program(
        {
            "q-cfg-incident-window-seconds": "",
            "q-cfg-incident-loss-pct-threshold": "",
            "q-cfg-load-test-port": "",
            "q-cfg-retention-raw-days": "14",
        }
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert "incident_window_seconds" not in payload
    assert "load_test_port" not in payload
    # `ge=0`, so a zero would pass validation and silently disable the
    # threshold instead of being rejected.
    assert "incident_loss_pct_threshold" not in payload
    assert payload["retention_raw_days"] == 14


@requires_node
def test_a_cleared_optional_text_field_is_still_sent_so_it_can_be_cleared():
    program = _payload_program(
        {
            "q-cfg-gateway-host": "",
            "q-cfg-load-test-server": "",
            "q-cfg-load-test-udp-bitrate": "",
            "q-cfg-load-test-kind": "iperf_udp",
        }
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    # Clearing these two is how the panel turns the gateway probe and the
    # load tests off, so an empty string is a real value here.
    assert payload["gateway_host"] == ""
    assert payload["load_test_server"] == ""
    # The bitrate has a format the API enforces; "" only ever means
    # "this form was never filled in".
    assert "load_test_udp_bitrate" not in payload
    assert payload["load_test_kind"] == "iperf_udp"


@requires_node
def test_a_field_missing_from_the_page_is_not_invented():
    result = _run_node(_payload_program({"q-cfg-retention-raw-days": "14"}))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"retention_raw_days": 14}


def _pending_config_program(tail: str) -> str:
    """Stub the DOM helpers `quality.js` shares with `app.js`, then run `tail`."""
    return (
        "const store = {};\n"
        # Atrapa pola formularza: prawdziwy <input> rzutuje przypisaną wartość
        # na tekst, więc stub musi robić to samo — inaczej test przepuściłby
        # kod, który wpisuje do pola liczbę.
        "const makeField = () => ({ _v: '', checked: false,\n"
        "  get value() { return this._v; },\n"
        "  set value(v) { this._v = String(v); } });\n"
        "globalThis.qs = (id) => (store[id] ||= makeField());\n"
        "globalThis.lastLoadedConfig = {\n"
        "  incident_window_seconds: 10,\n"
        "  load_test_port: 5201,\n"
        "  load_test_udp_bitrate: '10M',\n"
        "  diagnostics_enabled: true,\n"
        "};\n"
        f"{QUALITY_JS}\n"
        f"{tail}"
    )


@requires_node
def test_a_config_that_arrived_before_quality_js_loaded_is_still_applied():
    program = _pending_config_program(
        "console.log(JSON.stringify({\n"
        "  applied: Quality.ensureConfigApplied(),\n"
        "  window: qs('q-cfg-incident-window-seconds').value,\n"
        "  port: qs('q-cfg-load-test-port').value,\n"
        "  bitrate: qs('q-cfg-load-test-udp-bitrate').value,\n"
        "  diagnostics: qs('q-cfg-diagnostics-enabled').checked,\n"
        "}));"
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "applied": True,
        "window": "10",
        "port": "5201",
        "bitrate": "10M",
        "diagnostics": True,
    }


@requires_node
def test_reapplying_the_pending_config_never_discards_what_the_user_typed():
    program = _pending_config_program(
        "Quality.ensureConfigApplied();\n"
        "qs('q-cfg-load-test-port').value = '5301';\n"
        "const again = Quality.ensureConfigApplied();\n"
        "console.log(JSON.stringify({ again, port: qs('q-cfg-load-test-port').value }));"
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"again": False, "port": "5301"}
