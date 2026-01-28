from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class SpeedtestRuntime:
    lock: asyncio.Lock
    running: bool = False
    running_since_iso: str | None = None


_runtime: SpeedtestRuntime | None = None


def init_runtime() -> SpeedtestRuntime:
    global _runtime
    _runtime = SpeedtestRuntime(lock=asyncio.Lock())
    return _runtime


def get_runtime() -> SpeedtestRuntime:
    global _runtime
    if _runtime is None:
        # fallback (np. jeśli ktoś zaimportuje bez startup event)
        _runtime = SpeedtestRuntime(lock=asyncio.Lock())
    return _runtime

