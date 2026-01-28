from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import AppConfig

log = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    """Format duration in human-readable form."""
    if seconds < 60:
        return f"{int(seconds)} sek."
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes} min {secs} sek." if secs else f"{minutes} min"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours} godz. {minutes} min" if minutes else f"{hours} godz."


def send_outage_notification(
    cfg: AppConfig,
    started_at: str,
    ended_at: str,
    duration_seconds: float,
) -> bool:
    """Send email notification about completed internet outage.

    Returns True if email was sent successfully, False otherwise.
    """
    if not cfg.smtp_enabled:
        return False

    duration_str = _format_duration(duration_seconds)
    subject = f"Awaria internetu ({duration_str})"

    body_text = (
        f"Wykryto awarię internetu.\n\n"
        f"Początek awarii: {started_at}\n"
        f"Koniec awarii: {ended_at}\n"
        f"Czas trwania: {duration_str}"
    )
    body_html = f"""
    <html>
    <body>
    <h2 style="color: #dc2626;">Awaria internetu</h2>
    <table style="border-collapse: collapse;">
      <tr><td style="padding: 4px 12px 4px 0;"><strong>Początek:</strong></td><td>{started_at}</td></tr>
      <tr><td style="padding: 4px 12px 4px 0;"><strong>Koniec:</strong></td><td>{ended_at}</td></tr>
      <tr><td style="padding: 4px 12px 4px 0;"><strong>Czas trwania:</strong></td><td><strong>{duration_str}</strong></td></tr>
    </table>
    </body>
    </html>
    """

    return _send_email(cfg, subject, body_text, body_html)


def _send_email(
    cfg: AppConfig,
    subject: str,
    body_text: str,
    body_html: str,
) -> bool:
    """Send email via SMTP."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = cfg.smtp_from or cfg.smtp_user
        msg["To"] = cfg.smtp_to

        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        if cfg.smtp_use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
                server.starttls(context=context)
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.sendmail(cfg.smtp_from or cfg.smtp_user, [cfg.smtp_to], msg.as_string())
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.sendmail(cfg.smtp_from or cfg.smtp_user, [cfg.smtp_to], msg.as_string())

        log.info("Email notification sent: %s", subject)
        return True

    except Exception as e:
        log.error("Failed to send email notification: %s", e)
        return False
