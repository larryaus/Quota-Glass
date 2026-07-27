import time

import pytest
from fastapi.testclient import TestClient

from app.main import configured_notifier, create_app
from app.models import Meter, ProviderState
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


def history_client(tmp_path, monkeypatch):
    """An app whose providers return nothing, so only seeded rows exist."""
    def chatgpt(*args, **kwargs):
        return ProviderState(key="chatgpt", label="ChatGPT", mode="local")

    async def claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", claude)
    settings = Settings(
        enable_claude_oauth=False,
        codex_sessions_dir=tmp_path / "chatgpt",
        claude_projects_dir=tmp_path / "claude",
        notifications_enabled=False,
        database_path=tmp_path / "usage.db",
    )
    return create_app(settings=settings, notifier=NullNotifier())


def seed(application, key, sampled_at, used_pct):
    application.state.database.add_samples(
        [
            Meter(
                key=key,
                provider=key.split(".", 1)[0],
                label="Primary limit",
                used_pct=used_pct,
                window_minutes=300,
                resets_at=1_800_000_000,
                has_quota=True,
                source="rollout",
            )
        ],
        sampled_at=sampled_at,
    )


def test_history_returns_seeded_samples(tmp_path, monkeypatch):
    application = history_client(tmp_path, monkeypatch)
    now = int(time.time())

    with TestClient(application) as client:
        seed(application, "chatgpt.primary", now - 600, 40.0)
        seed(application, "claude.primary", now - 300, 70.0)
        body = client.get("/api/history?hours=1").json()

    assert body["hours"] == 1
    assert body["bucket_seconds"] == 0
    assert [row["meter_key"] for row in body["samples"]] == [
        "chatgpt.primary",
        "claude.primary",
    ]
    assert body["samples"][0]["used_pct"] == 40.0
    assert body["samples"][0]["stale"] is False


def test_history_filters_by_meter_key(tmp_path, monkeypatch):
    application = history_client(tmp_path, monkeypatch)
    now = int(time.time())

    with TestClient(application) as client:
        seed(application, "chatgpt.primary", now - 600, 40.0)
        seed(application, "claude.primary", now - 300, 70.0)
        body = client.get("/api/history?meter_key=claude.primary").json()

    assert [row["meter_key"] for row in body["samples"]] == ["claude.primary"]


def test_history_buckets_when_asked(tmp_path, monkeypatch):
    application = history_client(tmp_path, monkeypatch)
    base = ((int(time.time()) - 3000) // 300) * 300

    with TestClient(application) as client:
        seed(application, "chatgpt.primary", base, 10.0)
        seed(application, "chatgpt.primary", base + 60, 18.0)
        seed(application, "chatgpt.primary", base + 300, 25.0)
        body = client.get(
            "/api/history?hours=1&bucket_seconds=300&meter_key=chatgpt.primary"
        ).json()

    assert body["bucket_seconds"] == 300
    assert [(row["sampled_at"], row["used_pct"]) for row in body["samples"]] == [
        (base, 18.0),
        (base + 300, 25.0),
    ]


def test_history_rejects_out_of_range_arguments(tmp_path, monkeypatch):
    application = history_client(tmp_path, monkeypatch)

    with TestClient(application) as client:
        assert client.get("/api/history?hours=0").status_code == 422
        assert client.get("/api/history?hours=721").status_code == 422
        assert client.get("/api/history?bucket_seconds=-1").status_code == 422
        assert client.get("/api/history?bucket_seconds=86401").status_code == 422
