import time

import pytest
from fastapi.testclient import TestClient

from app.main import configured_notifier, create_app
from app.models import ProviderState
from app.notifier import (
    CompositeNotifier,
    MacOSNotifier,
    NotificationConfigurationError,
    NullNotifier,
    SmtpEmailNotifier,
)
from app.settings import Settings


def test_app_refresh_and_lifespan_are_loop_safe(
    monkeypatch,
    tmp_path,
):
    def chatgpt(*args, **kwargs):
        return ProviderState(key="chatgpt", label="ChatGPT", mode="local")

    async def claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", claude)
    settings = Settings(
        enable_claude_oauth=False,
        poll_interval_seconds=60,
        codex_sessions_dir=tmp_path / "chatgpt",
        claude_projects_dir=tmp_path / "claude",
        notifications_enabled=False,
        database_path=tmp_path / "usage.db",
    )
    application = create_app(settings=settings, notifier=NullNotifier())

    with TestClient(application) as client:
        for _ in range(100):
            if client.get("/api/health").json()["poll_count"] > 0:
                break
            time.sleep(0.001)
        assert client.post("/api/refresh").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/state").status_code == 200


def email_settings(tmp_path, **overrides):
    values = {
        "notifications_enabled": False,
        "email_notifications_enabled": True,
        "email_to": "owner@example.com",
        "email_from": "sender@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "",
        "smtp_password": "",
        "smtp_security": "starttls",
        "database_path": tmp_path / "usage.db",
    }
    values.update(overrides)
    return Settings(**values)


def test_configured_notifier_selects_email(tmp_path):
    notifier = configured_notifier(email_settings(tmp_path))

    assert isinstance(notifier, SmtpEmailNotifier)


def test_configured_notifier_keeps_desktop_and_email(tmp_path):
    notifier = configured_notifier(
        email_settings(tmp_path, notifications_enabled=True)
    )

    assert isinstance(notifier, CompositeNotifier)
    assert [type(item) for item in notifier.notifiers] == [
        MacOSNotifier,
        SmtpEmailNotifier,
    ]


def test_configured_notifier_fails_fast_when_email_config_is_missing(
    tmp_path,
):
    with pytest.raises(NotificationConfigurationError, match="SMTP_HOST"):
        configured_notifier(email_settings(tmp_path, smtp_host=""))
