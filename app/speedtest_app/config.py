from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class AppConfig:
    data_dir: str = os.getenv("DATA_DIR", "/data")

    # Użytkownik może to nadpisać przez UI (zapisywane w SQLite).
    connect_target: str = os.getenv("CONNECT_TARGET", "google.com")
    connect_default_port: int = int(os.getenv("CONNECT_DEFAULT_PORT", "443"))
    connect_timeout_seconds: float = float(os.getenv("CONNECT_TIMEOUT_SECONDS", "1"))
    connect_interval_seconds: float = float(os.getenv("CONNECT_INTERVAL_SECONDS", "5"))

    # Domyślny plik (user może zmienić przez UI).
    speedtest_url: str | None = os.getenv(
        "SPEEDTEST_URL",
        "https://webmail.psm.pulawy.pl/debian-12.9.0-amd64-DVD-1.iso",
    )
    speedtest_duration_seconds: float = float(os.getenv("SPEEDTEST_DURATION_SECONDS", "10"))
    speedtest_interval_seconds: float = float(os.getenv("SPEEDTEST_INTERVAL_SECONDS", "900"))
    speedtest_timeout_seconds: float = float(os.getenv("SPEEDTEST_TIMEOUT_SECONDS", "10"))
    speedtest_skip_if_offline: bool = _env_bool("SPEEDTEST_SKIP_IF_OFFLINE", True)

    # SMTP configuration for email notifications (all optional).
    smtp_host: str | None = os.getenv("SMTP_HOST") or None
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str | None = os.getenv("SMTP_USER") or None
    smtp_password: str | None = os.getenv("SMTP_PASSWORD") or None
    smtp_from: str | None = os.getenv("SMTP_FROM") or None
    smtp_to: str | None = os.getenv("SMTP_TO") or None
    smtp_use_tls: bool = _env_bool("SMTP_USE_TLS", True)
    smtp_min_outage_seconds: int = int(os.getenv("SMTP_MIN_OUTAGE_SECONDS", "60"))

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password and self.smtp_to)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "app.db")
