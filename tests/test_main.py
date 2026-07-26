import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import ProviderState
from app.notifier import NullNotifier
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
