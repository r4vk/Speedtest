"""Tests for the pure helpers inside `app.js`.

`app.js` needs a browser as a whole, so — like `test_static_quality.py` — each
test slices one helper out of the file and runs it through Node, rather than
re-implementing the logic in Python.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _find_node() -> str | None:
    return shutil.which("node")


NODE = _find_node()
requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed on this machine")


def _run_node(program: str) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=30)


def _format_config_error_source() -> str:
    parts = []
    for pattern in (
        r"function polishFields\(n\) \{.*?\n\}",
        r"function formatConfigError\(status, text, labelFor\) \{.*?\n\}",
    ):
        match = re.search(pattern, APP_JS, re.S)
        assert match, f"helper not found in app.js: {pattern}"
        parts.append(match.group(0))
    return "\n".join(parts)


def _program(body: str) -> str:
    return (
        f"{_format_config_error_source()}\n"
        "const labelFor = (f) => ({\n"
        "  incident_window_seconds: 'Okno oceny (s)',\n"
        "  load_test_port: 'Port',\n"
        "  retention_raw_days: 'Surowe pomiary (dni)',\n"
        "}[f] ?? f);\n"
        f"{body}"
    )


@requires_node
def test_a_validation_error_names_the_field_instead_of_dumping_json():
    detail = {
        "detail": [
            {
                "type": "greater_than_equal",
                "loc": ["body", "incident_window_seconds"],
                "msg": "Input should be greater than or equal to 5",
                "input": 0,
            }
        ]
    }
    program = _program(
        f"console.log(formatConfigError(422, {json.dumps(json.dumps(detail))}, labelFor));"
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    message = result.stdout.strip()

    assert "Okno oceny (s)" in message
    assert "greater than or equal to 5" in message
    assert "1 pole" in message
    # the raw payload must not leak into the panel
    assert "loc" not in message and "{" not in message


@requires_node
def test_many_bad_fields_are_counted_instead_of_listed_in_full():
    detail = {
        "detail": [
            {"loc": ["body", f"field_{i}"], "msg": "Input should be greater than or equal to 1"}
            for i in range(7)
        ]
    }
    program = _program(
        f"console.log(formatConfigError(422, {json.dumps(json.dumps(detail))}, labelFor));"
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    message = result.stdout.strip()

    assert "popraw 7 pól" in message
    assert "i 4 więcej" in message
    assert message.count("field_") == 3


@requires_node
@pytest.mark.parametrize(
    "count, expected",
    [(1, "1 pole"), (2, "2 pola"), (3, "3 pola"), (5, "5 pól"), (12, "12 pól"), (22, "22 pola")],
)
def test_the_field_count_is_declined_the_polish_way(count, expected):
    detail = {"detail": [{"loc": ["body", f"field_{i}"], "msg": "zła wartość"} for i in range(count)]}
    program = _program(
        f"console.log(formatConfigError(422, {json.dumps(json.dumps(detail))}, labelFor));"
    )
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert f"popraw {expected}:" in result.stdout.strip()


@requires_node
def test_a_plain_string_detail_is_shown_as_is():
    body = json.dumps({"detail": "speedtest_mode must be one of: url, speedtest.net, speedtest.pl"})
    program = _program(f"console.log(formatConfigError(400, {json.dumps(body)}, labelFor));")
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "Nie zapisano: speedtest_mode must be one of: url, speedtest.net, speedtest.pl"
    )


@requires_node
def test_a_response_that_is_not_json_still_says_what_happened():
    program = _program('console.log(formatConfigError(502, "<html>Bad Gateway</html>", labelFor));')
    result = _run_node(program)
    assert result.returncode == 0, result.stderr
    message = result.stdout.strip()

    assert "HTTP 502" in message
    assert "Bad Gateway" in message
