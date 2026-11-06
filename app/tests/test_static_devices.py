"""`devices.js` (stage 9 panel) only looks up ids that exist and is wired after quality.js."""
from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
DEVICES_JS = (STATIC_DIR / "devices.js").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def test_devices_js_loads_after_quality_js():
    assert INDEX_HTML.index("/static/quality.js") < INDEX_HTML.index("/static/devices.js")


def test_every_id_devices_js_looks_up_exists():
    ids = set(re.findall(r'\bel\(\s*"([^"]+)"\s*\)', DEVICES_JS))
    ids |= set(re.findall(r'flash\(\s*"([^"]+)"', DEVICES_JS))
    assert ids, "no lookups found — the pattern no longer matches devices.js"
    missing = sorted(i for i in ids if f'id="{i}"' not in INDEX_HTML)
    assert not missing, f"devices.js references ids missing from index.html: {missing}"


def test_app_js_refreshes_devices_with_quality():
    assert APP_JS.count("Devices.refresh(params)") == APP_JS.count("Quality.refresh(params)") == 2
