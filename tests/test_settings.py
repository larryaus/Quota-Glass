from app.settings import Settings


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
