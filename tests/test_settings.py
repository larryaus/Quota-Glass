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
