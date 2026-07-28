import os
from pathlib import Path
from typing import Optional


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


class Settings:
    def __init__(
        self,
        enable_claude_oauth: Optional[bool] = None,
        enable_claude_statusline: Optional[bool] = None,
        claude_status_snapshot_path: Optional[Path] = None,
        claude_status_stale_after_minutes: Optional[int] = None,
        poll_interval_seconds: Optional[int] = None,
        alert_threshold_pct: Optional[float] = None,
        alert_reset_pct: Optional[float] = None,
        codex_sessions_dir: Optional[Path] = None,
        claude_projects_dir: Optional[Path] = None,
        notifications_enabled: Optional[bool] = None,
        stale_after_minutes: Optional[int] = None,
        database_path: Optional[Path] = None,
        chatgpt_candidate_files: Optional[int] = None,
        history_retention_days: Optional[int] = None,
        max_notification_attempts: Optional[int] = None,
        oauth_min_interval_seconds: Optional[int] = None,
        oauth_max_backoff_seconds: Optional[int] = None,
        enable_chatgpt_live: Optional[bool] = None,
        chatgpt_live_min_interval_seconds: Optional[int] = None,
        codex_cli_path: Optional[str] = None,
        codex_cli_timeout_seconds: Optional[int] = None,
        email_notifications_enabled: Optional[bool] = None,
        email_to: Optional[str] = None,
        email_from: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_username: Optional[str] = None,
        smtp_password: Optional[str] = None,
        smtp_security: Optional[str] = None,
        smtp_timeout_seconds: Optional[int] = None,
        enable_burn_rate: Optional[bool] = None,
        burn_rate_window_minutes: Optional[int] = None,
        burn_rate_min_samples: Optional[int] = None,
        burn_rate_min_span_seconds: Optional[int] = None,
        projection_alert_enabled: Optional[bool] = None,
        projection_alert_margin_seconds: Optional[int] = None,
    ) -> None:
        home = Path.home()
        self.enable_claude_oauth = (
            _bool_env("ENABLE_CLAUDE_OAUTH", False)
            if enable_claude_oauth is None
            else enable_claude_oauth
        )
        self.enable_claude_statusline = (
            _bool_env("ENABLE_CLAUDE_STATUSLINE", False)
            if enable_claude_statusline is None
            else enable_claude_statusline
        )
        self.claude_status_snapshot_path = (
            Path(
                os.path.expanduser(
                    os.getenv(
                        "CLAUDE_STATUS_SNAPSHOT_PATH",
                        "~/Library/Caches/QuotaGlass/"
                        "claude-rate-limits.json",
                    )
                )
            )
            if claude_status_snapshot_path is None
            else Path(claude_status_snapshot_path)
        )
        self.claude_status_stale_after_minutes = max(
            1,
            _int_env("CLAUDE_STATUS_STALE_AFTER_MINUTES", 30)
            if claude_status_stale_after_minutes is None
            else claude_status_stale_after_minutes,
        )
        self.poll_interval_seconds = max(
            1,
            _int_env("POLL_INTERVAL_SECONDS", 60)
            if poll_interval_seconds is None
            else poll_interval_seconds,
        )
        self.alert_threshold_pct = (
            _float_env("ALERT_THRESHOLD_PCT", 100.0)
            if alert_threshold_pct is None
            else alert_threshold_pct
        )
        self.alert_reset_pct = (
            _float_env("ALERT_RESET_PCT", 5.0)
            if alert_reset_pct is None
            else alert_reset_pct
        )
        self.codex_sessions_dir = (
            Path(os.path.expanduser(os.getenv("CODEX_SESSIONS_DIR", "~/.codex/sessions")))
            if codex_sessions_dir is None
            else Path(codex_sessions_dir)
        )
        self.claude_projects_dir = (
            Path(os.path.expanduser(os.getenv("CLAUDE_PROJECTS_DIR", "~/.claude/projects")))
            if claude_projects_dir is None
            else Path(claude_projects_dir)
        )
        self.notifications_enabled = (
            _bool_env("NOTIFICATIONS_ENABLED", True)
            if notifications_enabled is None
            else notifications_enabled
        )
        self.stale_after_minutes = max(
            1,
            _int_env("STALE_AFTER_MINUTES", 30)
            if stale_after_minutes is None
            else stale_after_minutes,
        )
        self.database_path = (
            Path(os.getenv("DATABASE_PATH", "./data/usage.db"))
            if database_path is None
            else Path(database_path)
        )
        self.chatgpt_candidate_files = max(
            1,
            _int_env("CHATGPT_CANDIDATE_FILES", 5)
            if chatgpt_candidate_files is None
            else chatgpt_candidate_files,
        )
        self.history_retention_days = max(
            1,
            _int_env("HISTORY_RETENTION_DAYS", 30)
            if history_retention_days is None
            else history_retention_days,
        )
        self.max_notification_attempts = max(
            1,
            _int_env("MAX_NOTIFICATION_ATTEMPTS", 3)
            if max_notification_attempts is None
            else max_notification_attempts,
        )
        self.oauth_min_interval_seconds = max(
            1,
            _int_env("OAUTH_MIN_INTERVAL_SECONDS", 300)
            if oauth_min_interval_seconds is None
            else oauth_min_interval_seconds,
        )
        self.oauth_max_backoff_seconds = max(
            self.oauth_min_interval_seconds,
            _int_env("OAUTH_MAX_BACKOFF_SECONDS", 3600)
            if oauth_max_backoff_seconds is None
            else oauth_max_backoff_seconds,
        )
        self.enable_chatgpt_live = (
            _bool_env("ENABLE_CHATGPT_LIVE", False)
            if enable_chatgpt_live is None
            else enable_chatgpt_live
        )
        self.chatgpt_live_min_interval_seconds = max(
            1,
            _int_env("CHATGPT_LIVE_MIN_INTERVAL_SECONDS", 300)
            if chatgpt_live_min_interval_seconds is None
            else chatgpt_live_min_interval_seconds,
        )
        self.codex_cli_path = (
            os.getenv("CODEX_CLI_PATH", "codex")
            if codex_cli_path is None
            else codex_cli_path
        )
        self.codex_cli_timeout_seconds = max(
            1,
            _int_env("CODEX_CLI_TIMEOUT_SECONDS", 20)
            if codex_cli_timeout_seconds is None
            else codex_cli_timeout_seconds,
        )
        self.email_notifications_enabled = (
            _bool_env("EMAIL_NOTIFICATIONS_ENABLED", False)
            if email_notifications_enabled is None
            else email_notifications_enabled
        )
        self.email_to = (
            os.getenv("EMAIL_TO", "")
            if email_to is None
            else email_to
        )
        self.smtp_username = (
            os.getenv("SMTP_USERNAME", "")
            if smtp_username is None
            else smtp_username
        )
        self.smtp_password = (
            os.getenv("SMTP_PASSWORD", "")
            if smtp_password is None
            else smtp_password
        )
        self.email_from = (
            os.getenv("EMAIL_FROM", "") or self.smtp_username
            if email_from is None
            else email_from
        )
        self.smtp_host = (
            os.getenv("SMTP_HOST", "")
            if smtp_host is None
            else smtp_host
        )
        self.smtp_security = (
            os.getenv("SMTP_SECURITY", "starttls")
            if smtp_security is None
            else smtp_security
        ).strip().lower()
        default_smtp_port = 465 if self.smtp_security == "ssl" else 587
        self.smtp_port = max(
            1,
            _int_env("SMTP_PORT", default_smtp_port)
            if smtp_port is None
            else smtp_port,
        )
        self.smtp_timeout_seconds = max(
            1,
            _int_env("SMTP_TIMEOUT_SECONDS", 10)
            if smtp_timeout_seconds is None
            else smtp_timeout_seconds,
        )
        # Burn rate is arithmetic over samples already on disk -- no network
        # call, subprocess, or Keychain read -- so unlike the live sources it
        # defaults on.
        self.enable_burn_rate = (
            _bool_env("ENABLE_BURN_RATE", True)
            if enable_burn_rate is None
            else enable_burn_rate
        )
        self.burn_rate_window_minutes = max(
            1,
            _int_env("BURN_RATE_WINDOW_MINUTES", 60)
            if burn_rate_window_minutes is None
            else burn_rate_window_minutes,
        )
        self.burn_rate_min_samples = max(
            2,
            _int_env("BURN_RATE_MIN_SAMPLES", 3)
            if burn_rate_min_samples is None
            else burn_rate_min_samples,
        )
        self.burn_rate_min_span_seconds = max(
            1,
            _int_env("BURN_RATE_MIN_SPAN_SECONDS", 600)
            if burn_rate_min_span_seconds is None
            else burn_rate_min_span_seconds,
        )
        self.projection_alert_enabled = (
            _bool_env("PROJECTION_ALERT_ENABLED", True)
            if projection_alert_enabled is None
            else projection_alert_enabled
        )
        self.projection_alert_margin_seconds = max(
            0,
            _int_env("PROJECTION_ALERT_MARGIN_SECONDS", 900)
            if projection_alert_margin_seconds is None
            else projection_alert_margin_seconds,
        )
