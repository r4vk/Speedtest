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

from speedtest_app.quality_settings import QUALITY_SETTING_SPECS

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
# settings dialog: expected maintenance windows (Task 10, design spec §7)
# ---------------------------------------------------------------------------


def test_expected_window_settings_markup_exists():
    for element_id in (
        "q-expected-windows-tbody", "q-expected-window-form", "q-expected-window-name",
        "q-expected-window-from", "q-expected-window-to", "q-expected-window-days",
        "q-expected-window-target", "q-expected-window-note", "q-expected-window-add",
    ):
        assert f'id="{element_id}"' in INDEX_HTML


def test_expected_window_settings_are_wired():
    assert "/api/quality/expected-windows" in QUALITY_JS
    for element_id in ("q-expected-windows-tbody", "q-expected-window-add"):
        assert element_id in QUALITY_JS


def test_expected_window_help_text_explains_containment():
    assert "w całości" in INDEX_HTML        # the containment rule, stated to the user


def test_expected_window_day_checkboxes_use_monday_zero():
    """The rule editor must speak the same weekday dialect as the schedule
    editor in `app.js` (`DAYS = ["Pn", ...]`, 0 = Monday), or a rule saved on
    "Pn" would fire on Sunday."""
    block = INDEX_HTML[INDEX_HTML.index('id="q-expected-window-days"'):]
    block = block[: block.index("</div>")]
    values = re.findall(r'value="(\d)"', block)
    labels = re.findall(r"</label>", block)
    assert values == ["0", "1", "2", "3", "4", "5", "6"]
    assert len(labels) == 7
    assert "Pn" in block and "Nd" in block


# ---------------------------------------------------------------------------
# settings dialog: an empty field still shows its default, every load-test /
# diagnostics / retention field explains itself (user-visible UX complaint:
# "pola ustawień ... powinny mieć wartości domyślne i informacje co i jak
# ustawiać")
# ---------------------------------------------------------------------------

#: The same `["q-cfg-…", "klucz"]` shape `_ID_PATTERNS` matches, but keeping
#: the key too so a placeholder can be checked against its spec default.
_ID_KEY_PATTERN = re.compile(r'\[\s*"(q-cfg-[a-zA-Z0-9-]+)"\s*,\s*"([a-zA-Z0-9_]+)"')


def _config_id_key_pairs(source: str) -> dict[str, str]:
    return dict(_ID_KEY_PATTERN.findall(source))


def _input_tag(input_id: str, html: str) -> str | None:
    match = re.search(r'<input\b[^>]*\bid="' + re.escape(input_id) + r'"[^>]*>', html)
    return match.group(0) if match else None


def _is_select(input_id: str, html: str) -> bool:
    return re.search(r'<select\b[^>]*\bid="' + re.escape(input_id) + r'"', html) is not None


def _placeholder_default_str(kind: type, default: object) -> str:
    """Render a `QUALITY_SETTING_SPECS` default the way a placeholder shows it.

    Every numeric default in the table is a whole number (see
    `quality_settings.py`), so this renders `20.0` as `"20"` the way an
    `<input type="number">` would, not Python's `"20.0"`.
    """
    if kind in (int, float):
        return str(int(default))
    return str(default)


def test_every_number_or_text_config_input_has_a_placeholder_matching_its_default():
    """A field left empty must still tell the operator what will be used.

    Reads the expected value straight from `QUALITY_SETTING_SPECS` — the
    single source of truth for every quality setting's default — instead of
    hardcoding numbers here, so a future change to a default is caught as a
    stale placeholder instead of silently drifting from what the UI promises.
    """
    id_key_pairs = _config_id_key_pairs(QUALITY_JS)
    # Guards the extraction pattern itself: a rewrite that stops matching
    # anything must not make the loop below vacuously pass.
    assert len(id_key_pairs) >= 25

    checked: list[str] = []
    for input_id, key in id_key_pairs.items():
        spec = QUALITY_SETTING_SPECS.get(key)
        if spec is None:
            continue
        kind, default = spec
        if kind is bool:
            continue  # checkboxes: a placeholder has no meaning here
        if _is_select(input_id, INDEX_HTML):
            continue  # <select>: its <option> order already encodes the default
        if kind is str and default == "":
            continue  # nothing useful to promise; keeps its illustrative example instead
        tag = _input_tag(input_id, INDEX_HTML)
        assert tag is not None, f'{input_id}: no matching <input id="{input_id}"> in index.html'
        expected = _placeholder_default_str(kind, default)
        assert f'placeholder="{expected}"' in tag, (
            f"{input_id} (key={key!r}): expected placeholder={expected!r}, got {tag!r}"
        )
        checked.append(input_id)

    # Guards against every candidate being skipped by the exemptions above.
    assert len(checked) >= 20


_HINTED_SECTION_IDS: dict[str, str] = {
    "q-loadtest-section": "Testy obciążeniowe",
    "q-diagnostics-section": "Diagnostyka",
    "q-retention-section": "Retencja",
}


def _section_html(section_id: str, html: str) -> str:
    """The inner HTML of one `.settings-section`, up to the next section."""
    start_match = re.search(
        r'<div class="settings-section"[^>]*\bid="' + re.escape(section_id) + r'"[^>]*>', html
    )
    assert start_match, f"section id={section_id!r} not found in index.html"
    start = start_match.end()
    end_match = re.search(r'<!--\s*Sekcja:|<div class="modal-actions"', html[start:])
    end = start + end_match.start() if end_match else len(html)
    return html[start:end]


def test_every_field_in_loadtest_diagnostics_and_retention_has_a_hint():
    """Every q-cfg-* field in these three sections explains itself.

    The header enable/disable toggle of each section is exempt: it is
    covered by the section-level `<p class="hint">` note instead (the same
    pattern "Cele pomiarowe" already uses for "Tryb diagnostyczny"), which
    this test also requires to exist.
    """
    checked_ids: set[str] = set()
    for section_id, title in _HINTED_SECTION_IDS.items():
        section_html = _section_html(section_id, INDEX_HTML)
        assert '<p class="hint">' in section_html, f"section {title!r} has no section-level note"
        labels = re.findall(r"<label\b.*?</label>", section_html, re.S)
        assert labels, f"section {title!r}: no <label> blocks found"
        for label in labels:
            if "toggle-switch" in label:
                continue  # header enable/disable toggle: covered by the section note above
            ids_in_label = re.findall(r'id="(q-cfg-[a-zA-Z0-9-]+)"', label)
            if not ids_in_label:
                continue
            assert "hint-inline" in label, (
                f"{title!r}: field(s) {ids_in_label} have no hint-inline: {label!r}"
            )
            checked_ids.update(ids_in_label)

    # 8 (load test) + 5 (diagnostics) + 5 (retention) fields, minus some slack.
    assert len(checked_ids) >= 15


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


def test_served_page_has_panel_title_and_claims_no_measurement_host(client):
    """The panel must not name the machine it runs on: nothing detects it.

    The old status strip signed every reading "pomiar z NAS-a po kablu" even
    when the container ran on a laptop over Wi-Fi.
    """
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "NAS" not in body
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
    assert warnings["loops"] == "niewykonane pomiary: 7 · wznowienia sondy: 1"


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
