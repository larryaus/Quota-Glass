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
    ) -> None:
        home = Path.home()
        self.enable_claude_oauth = (
            _bool_env("ENABLE_CLAUDE_OAUTH", False)
            if enable_claude_oauth is None
            else enable_claude_oauth
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
