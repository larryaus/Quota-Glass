import asyncio
import threading
import time

import pytest

from app.alerting import AlertEngine
from app.database import Database
from app.models import Meter, ProviderState
from app.notifier import NullNotifier
from app.poller import UsagePoller
from app.settings import Settings


def quota_meter(
    provider="chatgpt",
    pct=100.0,
    resets_at=1_800_000_000,
):
    return Meter(
        key="%s.primary" % provider,
        provider=provider,
        label="Primary limit",
        used_pct=pct,
        window_minutes=300,
        resets_at=resets_at,
        has_quota=True,
        source="rollout",
        stale=False,
    )


def poller_settings(tmp_path, **kwargs):
    values = {
        "enable_claude_oauth": False,
        "poll_interval_seconds": 60,
        "codex_sessions_dir": tmp_path / "chatgpt",
        "claude_projects_dir": tmp_path / "claude",
        "notifications_enabled": False,
        "database_path": tmp_path / "usage.db",
    }
    values.update(kwargs)
    return Settings(**values)


@pytest.mark.asyncio
async def test_provider_failure_does_not_block_other_alerts_or_samples(
    monkeypatch,
    tmp_path,
):
    def broken_chatgpt(*args, **kwargs):
        raise RuntimeError("broken rollout")

    async def working_claude(*args, **kwargs):
        return ProviderState(
            key="claude",
            label="Claude",
            mode="oauth",
            meters=[quota_meter("claude", 100)],
        )

    monkeypatch.setattr("app.poller.parse_chatgpt", broken_chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", working_claude)
    settings = poller_settings(tmp_path)
    database = Database(settings.database_path)
    alerts = AlertEngine(database, NullNotifier())
    alerts.process(quota_meter("claude", 90), now=100)
    poller = UsagePoller(settings, database, alerts)

    state = await poller.refresh()

    assert state.providers[0].error == "ChatGPT polling failed: broken rollout"
    assert state.providers[1].meters[0].used_pct == 100
    assert database.get_events()[0]["meter_key"] == "claude.primary"
    sample_count = database._connection.execute(
        "SELECT COUNT(*) FROM samples WHERE meter_key = 'claude.primary'"
    ).fetchone()[0]
    assert sample_count == 1
    assert state.poller.status == "degraded"


class SleepingNotifier:
    def __init__(self):
        self.started = None
        self.completed = None

    def notify(self, title, subtitle, message):
        self.started = time.monotonic()
        time.sleep(0.15)
        self.completed = time.monotonic()


@pytest.mark.asyncio
async def test_notification_delivery_does_not_block_event_loop(
    monkeypatch,
    tmp_path,
):
    notifier = SleepingNotifier()

    def chatgpt(*args, **kwargs):
        return ProviderState(
            key="chatgpt",
            label="ChatGPT",
            mode="local",
            meters=[quota_meter("chatgpt", 100)],
        )

    async def claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", claude)
    settings = poller_settings(tmp_path)
    database = Database(settings.database_path)
    alerts = AlertEngine(database, notifier)
    alerts.process(quota_meter("chatgpt", 90), now=100)
    poller = UsagePoller(settings, database, alerts)

    refresh_task = asyncio.create_task(poller.refresh())
    while notifier.started is None:
        await asyncio.sleep(0.001)
    heartbeat = time.monotonic()
    await refresh_task

    assert notifier.started < heartbeat < notifier.completed


class AlwaysFailNotifier:
    def notify(self, title, subtitle, message):
        raise RuntimeError("notifications disabled by system")


@pytest.mark.asyncio
async def test_notification_failure_is_exposed_in_poller_health(
    monkeypatch,
    tmp_path,
):
    def chatgpt(*args, **kwargs):
        return ProviderState(
            key="chatgpt",
            label="ChatGPT",
            mode="local",
            meters=[quota_meter("chatgpt", 100)],
        )

    async def claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", claude)
    settings = poller_settings(tmp_path)
    database = Database(settings.database_path)
    alerts = AlertEngine(database, AlwaysFailNotifier())
    alerts.process(quota_meter("chatgpt", 90), now=100)
    poller = UsagePoller(settings, database, alerts)

    state = await poller.refresh()

    assert state.poller.status == "degraded"
    assert "notifications disabled by system" in state.poller.last_error


@pytest.mark.asyncio
async def test_background_task_liveness_and_completion_age(
    monkeypatch,
    tmp_path,
):
    def chatgpt(*args, **kwargs):
        return ProviderState(key="chatgpt", label="ChatGPT", mode="local")

    async def claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", claude)
    settings = poller_settings(tmp_path)
    database = Database(settings.database_path)
    poller = UsagePoller(
        settings,
        database,
        AlertEngine(database, NullNotifier()),
    )

    task = asyncio.create_task(poller.run())
    while poller.health.poll_count == 0:
        await asyncio.sleep(0.001)
    state = poller.state()
    assert state.poller.running is False
    assert state.poller.background_task_alive is True
    assert state.poller.last_poll_completed_age_seconds is not None

    poller.stop()
    await task
    assert poller.state().poller.background_task_alive is False


@pytest.mark.asyncio
async def test_chatgpt_keeps_polling_while_claude_oauth_is_cached(
    monkeypatch,
    tmp_path,
):
    chatgpt_calls = []
    keychain_reads = []
    oauth_calls = []

    def chatgpt(*args, **kwargs):
        chatgpt_calls.append(True)
        return ProviderState(key="chatgpt", label="ChatGPT", mode="local")

    def read_token():
        keychain_reads.append(True)
        return "token"

    async def oauth_usage(token):
        oauth_calls.append(token)
        return {
            "five_hour": {
                "utilization": 50,
                "resets_at": "2026-07-26T07:00:00Z",
            }
        }

    monkeypatch.setattr("app.poller.parse_chatgpt", chatgpt)
    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        read_token,
    )
    monkeypatch.setattr(
        "app.providers.claude._fetch_oauth_usage",
        oauth_usage,
    )
    settings = poller_settings(
        tmp_path,
        enable_claude_oauth=True,
        oauth_min_interval_seconds=300,
    )
    database = Database(settings.database_path)
    poller = UsagePoller(
        settings,
        database,
        AlertEngine(database, NullNotifier()),
    )

    await poller.refresh()
    await poller.refresh()

    assert len(chatgpt_calls) == 2
    assert len(keychain_reads) == 1
    assert oauth_calls == ["token"]


@pytest.mark.asyncio
async def test_poller_passes_live_settings_to_chatgpt(monkeypatch, tmp_path):
    captured = {}

    def fake_chatgpt(*args, **kwargs):
        captured.update(kwargs)
        return ProviderState(key="chatgpt", label="ChatGPT", mode="local")

    async def working_claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", fake_chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", working_claude)
    settings = poller_settings(
        tmp_path,
        enable_chatgpt_live=True,
        codex_cli_path="/fake/codex",
        codex_cli_timeout_seconds=9,
    )
    database = Database(settings.database_path)
    alerts = AlertEngine(database, NullNotifier())
    poller = UsagePoller(settings, database, alerts)

    await poller.refresh()

    assert captured["enable_live"] is True
    assert captured["cli_path"] == "/fake/codex"
    assert captured["cli_timeout_seconds"] == 9
    assert captured["live_cache"] is poller._chatgpt_live_cache


@pytest.mark.asyncio
async def test_concurrent_refresh_returns_current_state_instead_of_queueing(
    monkeypatch,
    tmp_path,
):
    """Waiting for the refresh lock must never occupy a worker thread.

    The lock holder needs workers of its own for parse_chatgpt, alerting, and
    sample persistence, and the ChatGPT live source can hold it for ~25s (a 20s
    CLI deadline plus subprocess termination). Callers parked on the lock can
    therefore starve the holder, which then never releases it and never
    recovers. A refresh that arrives mid-poll returns the current state.
    """
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_chatgpt(*args, **kwargs):
        calls.append(True)
        entered.set()
        release.wait(30)
        return ProviderState(
            key="chatgpt",
            label="ChatGPT",
            mode="local",
            meters=[quota_meter("chatgpt", 42)],
        )

    async def claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", blocking_chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", claude)
    settings = poller_settings(tmp_path)
    database = Database(settings.database_path)
    poller = UsagePoller(settings, database, AlertEngine(database, NullNotifier()))

    in_flight = asyncio.create_task(poller.refresh())
    try:
        while not entered.is_set():
            await asyncio.sleep(0.005)
        state = await asyncio.wait_for(poller.refresh(), timeout=5)
    finally:
        release.set()
    await in_flight

    assert len(calls) == 1, "a concurrent refresh must not queue behind the poll"
    assert state.poller is poller.health
    assert poller._refresh_lock.acquire(blocking=False), "lock was left held"
    poller._refresh_lock.release()


@pytest.mark.asyncio
async def test_refresh_releases_the_lock_when_it_raises(monkeypatch, tmp_path):
    def chatgpt(*args, **kwargs):
        return ProviderState(key="chatgpt", label="ChatGPT", mode="local")

    async def claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    def boom():
        raise RuntimeError("state building failed")

    monkeypatch.setattr("app.poller.parse_chatgpt", chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", claude)
    settings = poller_settings(tmp_path)
    database = Database(settings.database_path)
    poller = UsagePoller(settings, database, AlertEngine(database, NullNotifier()))
    monkeypatch.setattr(poller, "state", boom)

    with pytest.raises(RuntimeError):
        await poller.refresh()

    assert poller._refresh_lock.acquire(blocking=False), (
        "a failed refresh must not leave the poller locked out forever"
    )
    poller._refresh_lock.release()


@pytest.mark.asyncio
async def test_live_cache_survives_across_polls(monkeypatch, tmp_path):
    seen = []

    def fake_chatgpt(*args, **kwargs):
        seen.append(kwargs["live_cache"])
        return ProviderState(key="chatgpt", label="ChatGPT", mode="local")

    async def working_claude(*args, **kwargs):
        return ProviderState(key="claude", label="Claude", mode="local")

    monkeypatch.setattr("app.poller.parse_chatgpt", fake_chatgpt)
    monkeypatch.setattr("app.poller.parse_claude", working_claude)
    settings = poller_settings(tmp_path, enable_chatgpt_live=True)
    database = Database(settings.database_path)
    alerts = AlertEngine(database, NullNotifier())
    poller = UsagePoller(settings, database, alerts)

    await poller.refresh()
    await poller.refresh()

    assert seen[0] is seen[1], "cache must persist so backoff state is not lost"
