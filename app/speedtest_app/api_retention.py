"""`GET /api/quality/retention` — DB growth estimate and the active retention
window (design spec §14).

The db path is read from `request.app.state.quality_engine.cfg.db_path`
(set up by `main.py`'s `lifespan`) rather than from a module-level `AppConfig()`
here: `AppConfig`'s fields default to `os.getenv(...)` evaluated once, when the
*class* is defined, not on every `AppConfig()` call — the `client` test fixture
reloads `config`/`db`/`main` to pick up a fresh `DATA_DIR` per test, but never
reloads this module, so an `AppConfig` imported here at first collection would
keep resolving to the very first test's (by then torn down) database. Going
through the engine mirrors how `main._quality_status()` already does it, and
always reflects whichever app instance is actually serving the request.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request

from .config import AppConfig
from .db import get_settings
from .retention import RETENTION_SETTINGS_KEYS, RetentionSettings, estimate_growth
from .time_utils import to_iso_z, utc_now

router = APIRouter(prefix="/api")


def _db_path(request: Request) -> str:
    engine = getattr(request.app.state, "quality_engine", None)
    if engine is not None:
        return engine.cfg.db_path
    return AppConfig().db_path  # pragma: no cover - defensive, mirrors _quality_status()


@router.get("/quality/retention")
def get_retention_status(request: Request) -> dict[str, Any]:
    db_path = _db_path(request)
    now = utc_now()
    values = get_settings(db_path, list(RETENTION_SETTINGS_KEYS))
    settings = RetentionSettings.from_settings(values)
    growth = estimate_growth(db_path, now=now, raw_days=settings.raw_days)
    return {
        **growth,
        "settings": asdict(settings),
        "raw_available_from": to_iso_z(now - timedelta(days=settings.raw_days)),
    }
