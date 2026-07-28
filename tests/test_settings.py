from pathlib import Path

from app.settings import Settings


def test_claude_statusline_defaults_off(monkeypatch):
    for name in (
        "ENABLE_CLAUDE_STATUSLINE",
        "CLAUDE_STATUS_SNAPSHOT_PATH",
        "CLAUDE_STATUS_STALE_AFTER_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.enable_claude_statusline is False
    assert settings.claude_status_snapshot_path == (
        Path.home()
        / "Library"
        / "Caches"
        / "QuotaGlass"
        / "claude-rate-limits.json"
    )
    assert settings.claude_status_stale_after_minutes == 30


def test_claude_statusline_reads_environment(monkeypatch, tmp_path):
    snapshot = tmp_path / "claude.json"
    monkeypatch.setenv("ENABLE_CLAUDE_STATUSLINE", "1")
    monkeypatch.setenv("CLAUDE_STATUS_SNAPSHOT_PATH", str(snapshot))
    monkeypatch.setenv("CLAUDE_STATUS_STALE_AFTER_MINUTES", "45")

    settings = Settings()

    assert settings.enable_claude_statusline is True
    assert settings.claude_status_snapshot_path == snapshot
    assert settings.claude_status_stale_after_minutes == 45


def test_chatgpt_live_defaults_off(monkeypatch):
    monkeypatch.delenv("ENABLE_CHATGPT_LIVE", raising=False)
    settings = Settings()

    assert settings.enable_chatgpt_live is False
    assert settings.chatgpt_live_min_interval_seconds == 300
    assert settings.codex_cli_path == "codex"
    assert settings.codex_cli_timeout_seconds == 20


def test_chatgpt_live_reads_environment(monkeypatch):
    monkeypatch.setenv("ENABLE_CHATGPT_LIVE", "1")
    monkeypatch.setenv("CHATGPT_LIVE_MIN_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("CODEX_CLI_PATH", "/usr/local/bin/codex")
    monkeypatch.setenv("CODEX_CLI_TIMEOUT_SECONDS", "7")
    settings = Settings()

    assert settings.enable_chatgpt_live is True
    assert settings.chatgpt_live_min_interval_seconds == 45
    assert settings.codex_cli_path == "/usr/local/bin/codex"
    assert settings.codex_cli_timeout_seconds == 7


def test_explicit_arguments_beat_environment(monkeypatch):
    monkeypatch.setenv("ENABLE_CHATGPT_LIVE", "1")
    settings = Settings(enable_chatgpt_live=False)

    assert settings.enable_chatgpt_live is False


def test_email_notifications_default_off(monkeypatch):
    for name in (
        "EMAIL_NOTIFICATIONS_ENABLED",
        "SMTP_SECURITY",
        "SMTP_PORT",
        "SMTP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()

    assert settings.email_notifications_enabled is False
    assert settings.smtp_security == "starttls"
    assert settings.smtp_port == 587
    assert settings.smtp_timeout_seconds == 10


def test_email_notifications_read_environment(monkeypatch):
    monkeypatch.setenv("EMAIL_NOTIFICATIONS_ENABLED", "1")
    monkeypatch.setenv("EMAIL_TO", "owner@example.com")
    monkeypatch.setenv("SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_SECURITY", "ssl")
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.setenv("SMTP_TIMEOUT_SECONDS", "15")

    settings = Settings()

    assert settings.email_notifications_enabled is True
    assert settings.email_to == "owner@example.com"
    assert settings.email_from == "sender@example.com"
    assert settings.smtp_username == "sender@example.com"
    assert settings.smtp_password == "app-password"
    assert settings.smtp_host == "smtp.example.com"
    assert settings.smtp_security == "ssl"
    assert settings.smtp_port == 465
    assert settings.smtp_timeout_seconds == 15


def test_burn_rate_defaults_on(monkeypatch):
    """Unlike the live sources this is local arithmetic, so it defaults on."""
    for name in (
        "ENABLE_BURN_RATE",
        "BURN_RATE_WINDOW_MINUTES",
        "BURN_RATE_MIN_SAMPLES",
        "BURN_RATE_MIN_SPAN_SECONDS",
        "PROJECTION_ALERT_ENABLED",
        "PROJECTION_ALERT_MARGIN_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()

    assert settings.enable_burn_rate is True
    assert settings.burn_rate_window_minutes == 60
    assert settings.burn_rate_min_samples == 3
    assert settings.burn_rate_min_span_seconds == 600
    assert settings.projection_alert_enabled is True
    assert settings.projection_alert_margin_seconds == 900


def test_burn_rate_reads_environment(monkeypatch):
    monkeypatch.setenv("ENABLE_BURN_RATE", "0")
    monkeypatch.setenv("BURN_RATE_WINDOW_MINUTES", "120")
    monkeypatch.setenv("BURN_RATE_MIN_SAMPLES", "5")
    monkeypatch.setenv("BURN_RATE_MIN_SPAN_SECONDS", "300")
    monkeypatch.setenv("PROJECTION_ALERT_ENABLED", "0")
    monkeypatch.setenv("PROJECTION_ALERT_MARGIN_SECONDS", "1800")
    settings = Settings()

    assert settings.enable_burn_rate is False
    assert settings.burn_rate_window_minutes == 120
    assert settings.burn_rate_min_samples == 5
    assert settings.burn_rate_min_span_seconds == 300
    assert settings.projection_alert_enabled is False
    assert settings.projection_alert_margin_seconds == 1800


def test_burn_rate_explicit_arguments_beat_environment(monkeypatch):
    monkeypatch.setenv("ENABLE_BURN_RATE", "0")
    monkeypatch.setenv("BURN_RATE_MIN_SAMPLES", "9")
    settings = Settings(enable_burn_rate=True, burn_rate_min_samples=4)

    assert settings.enable_burn_rate is True
    assert settings.burn_rate_min_samples == 4


def test_burn_rate_minimum_sample_count_is_clamped(monkeypatch):
    """Two points are the fewest that can describe a rate at all."""
    monkeypatch.delenv("BURN_RATE_MIN_SAMPLES", raising=False)

    assert Settings(burn_rate_min_samples=1).burn_rate_min_samples == 2
