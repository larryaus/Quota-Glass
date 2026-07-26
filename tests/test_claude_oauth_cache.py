from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.alerting import AlertEngine
from app.database import Database
from app.notifier import NullNotifier
from app.providers.claude import ClaudeOAuthCache, parse_claude
from app.settings import Settings


START = datetime(2026, 7, 26, 2, 0, tzinfo=timezone.utc)


def oauth_payload(used_pct=50.0):
    return {
        "five_hour": {
            "utilization": used_pct,
            "resets_at": "2026-07-26T07:00:00Z",
        }
    }


def rate_limit(headers=None):
    request = httpx.Request(
        "GET",
        "https://api.anthropic.com/api/oauth/usage",
    )
    response = httpx.Response(
        429,
        headers=headers,
        request=request,
    )
    return httpx.HTTPStatusError(
        "rate limited",
        request=request,
        response=response,
    )


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_oauth_cache_skips_within_ttl_and_refetches_at_ttl(
    monkeypatch,
    fixture_dir,
):
    keychain_reads = []
    http_tokens = []

    def read_token():
        token = "token-%d" % (len(keychain_reads) + 1)
        keychain_reads.append(token)
        return token

    async def fetch(token):
        http_tokens.append(token)
        return oauth_payload(40.0 + len(http_tokens))

    monkeypatch.setattr("app.providers.claude._read_keychain_token", read_token)
    monkeypatch.setattr("app.providers.claude._fetch_oauth_usage", fetch)
    cache = ClaudeOAuthCache(
        min_interval_seconds=300,
        max_backoff_seconds=3600,
    )

    first = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=True,
        now=START,
        oauth_cache=cache,
    )
    cached = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=True,
        now=START + timedelta(seconds=299),
        oauth_cache=cache,
    )
    refreshed = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=True,
        now=START + timedelta(seconds=300),
        oauth_cache=cache,
    )

    assert first.meters[0].used_pct == 41.0
    assert cached.meters[0].used_pct == 41.0
    assert cached.meters[0].stale is False
    assert cached.oauth_cache_age_seconds == 299
    assert cached.last_updated == iso(START)
    assert refreshed.meters[0].used_pct == 42.0
    assert refreshed.oauth_cache_age_seconds == 0
    assert keychain_reads == ["token-1", "token-2"]
    assert http_tokens == keychain_reads


@pytest.mark.asyncio
async def test_retry_after_seconds_blocks_requests_until_due(
    monkeypatch,
    fixture_dir,
):
    http_calls = []

    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        lambda: "token",
    )

    async def fetch(token):
        http_calls.append(token)
        if len(http_calls) == 2:
            raise rate_limit({"Retry-After": "120"})
        return oauth_payload()

    monkeypatch.setattr("app.providers.claude._fetch_oauth_usage", fetch)
    cache = ClaudeOAuthCache(
        min_interval_seconds=60,
        max_backoff_seconds=3600,
    )

    await parse_claude(
        fixture_dir / "claude",
        True,
        START,
        cache,
    )
    limited = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=60),
        cache,
    )
    waiting = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=179),
        cache,
    )

    assert len(http_calls) == 2
    assert limited.oauth_backed_off is True
    assert limited.oauth_next_retry_at == iso(START + timedelta(seconds=180))
    assert limited.meters[0].stale is True
    assert waiting.meters[0].stale is True

    recovered = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=180),
        cache,
    )

    assert len(http_calls) == 3
    assert recovered.oauth_backed_off is False
    assert recovered.meters[0].stale is False


@pytest.mark.asyncio
async def test_retry_after_http_date_is_honored(
    monkeypatch,
    fixture_dir,
):
    calls = []
    retry_at = START + timedelta(seconds=120)

    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        lambda: "token",
    )

    async def fetch(token):
        calls.append(token)
        raise rate_limit(
            {"Retry-After": retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT")}
        )

    monkeypatch.setattr("app.providers.claude._fetch_oauth_usage", fetch)
    cache = ClaudeOAuthCache(60, 3600)

    state = await parse_claude(
        fixture_dir / "claude",
        True,
        START,
        cache,
    )
    await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=119),
        cache,
    )

    assert len(calls) == 1
    assert state.oauth_next_retry_at == iso(retry_at)


@pytest.mark.asyncio
async def test_rate_limit_backoff_grows_exponentially_and_caps(
    monkeypatch,
    fixture_dir,
):
    calls = []

    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        lambda: "token",
    )

    async def fetch(token):
        calls.append(token)
        raise rate_limit()

    monkeypatch.setattr("app.providers.claude._fetch_oauth_usage", fetch)
    cache = ClaudeOAuthCache(300, 1000)

    first = await parse_claude(
        fixture_dir / "claude",
        True,
        START,
        cache,
    )
    await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=299),
        cache,
    )
    second = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=300),
        cache,
    )
    third = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=900),
        cache,
    )
    fourth = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=1900),
        cache,
    )

    assert len(calls) == 4
    assert first.oauth_next_retry_at == iso(START + timedelta(seconds=300))
    assert second.oauth_next_retry_at == iso(START + timedelta(seconds=900))
    assert third.oauth_next_retry_at == iso(START + timedelta(seconds=1900))
    assert fourth.oauth_next_retry_at == iso(START + timedelta(seconds=2900))


@pytest.mark.asyncio
async def test_success_after_backoff_resets_exponential_interval(
    monkeypatch,
    fixture_dir,
):
    calls = []

    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        lambda: "token",
    )

    async def fetch(token):
        calls.append(token)
        if len(calls) in (1, 3):
            raise rate_limit()
        return oauth_payload()

    monkeypatch.setattr("app.providers.claude._fetch_oauth_usage", fetch)
    cache = ClaudeOAuthCache(300, 3600)

    await parse_claude(
        fixture_dir / "claude",
        True,
        START,
        cache,
    )
    recovered = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=300),
        cache,
    )
    limited_again = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=600),
        cache,
    )

    assert len(calls) == 3
    assert recovered.oauth_backed_off is False
    assert limited_again.oauth_next_retry_at == iso(
        START + timedelta(seconds=900)
    )


@pytest.mark.asyncio
async def test_stale_cached_oauth_meter_does_not_change_alert_latch(
    monkeypatch,
    fixture_dir,
    tmp_path,
):
    calls = []

    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        lambda: "token",
    )

    async def fetch(token):
        calls.append(token)
        if len(calls) == 2:
            raise rate_limit()
        return oauth_payload(100.0)

    monkeypatch.setattr("app.providers.claude._fetch_oauth_usage", fetch)
    cache = ClaudeOAuthCache(300, 3600)

    await parse_claude(
        fixture_dir / "claude",
        True,
        START,
        cache,
    )
    stale_state = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=300),
        cache,
    )

    stale_full = stale_state.meters[0]
    assert stale_state.mode == "oauth"
    assert stale_state.error is not None
    assert "Next attempt at" in stale_state.error
    assert stale_full.source == "oauth"
    assert stale_full.has_quota is True
    assert stale_full.stale is True

    database = Database(tmp_path / "alerts.db")
    alerts = AlertEngine(database, NullNotifier(), 100.0, 5.0)
    fresh_ninety = stale_full.model_copy(
        update={"used_pct": 90.0, "stale": False}
    )
    fresh_full = stale_full.model_copy(update={"stale": False})
    stale_empty = stale_full.model_copy(
        update={"used_pct": 0.0, "stale": True}
    )

    assert alerts.process(fresh_ninety, now=100) == []
    assert alerts.process(stale_full, now=101) == []
    assert alerts.process(fresh_full, now=102) == ["EXHAUSTED"]
    assert alerts.process(stale_empty, now=103) == []
    assert alerts.process(fresh_full, now=104) == []
    assert [event["event_type"] for event in database.get_events()] == [
        "EXHAUSTED"
    ]


@pytest.mark.asyncio
async def test_never_succeeded_failure_stays_local_and_waits_to_retry(
    monkeypatch,
    fixture_dir,
):
    keychain_reads = []

    def broken_keychain():
        keychain_reads.append(True)
        raise RuntimeError("locked")

    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        broken_keychain,
    )
    cache = ClaudeOAuthCache(300, 3600)

    failed = await parse_claude(
        fixture_dir / "claude",
        True,
        START,
        cache,
    )
    waiting = await parse_claude(
        fixture_dir / "claude",
        True,
        START + timedelta(seconds=299),
        cache,
    )

    assert len(keychain_reads) == 1
    assert failed.mode == "local"
    assert failed.oauth_backed_off is True
    assert failed.oauth_cache_age_seconds is None
    assert "OAuth unavailable" in failed.error
    assert "locked" in failed.error
    assert waiting.mode == "local"


def test_oauth_interval_settings_defaults_and_environment(monkeypatch):
    monkeypatch.delenv("OAUTH_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("OAUTH_MAX_BACKOFF_SECONDS", raising=False)

    defaults = Settings()
    assert defaults.oauth_min_interval_seconds == 300
    assert defaults.oauth_max_backoff_seconds == 3600

    monkeypatch.setenv("OAUTH_MIN_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("OAUTH_MAX_BACKOFF_SECONDS", "900")
    configured = Settings()
    assert configured.oauth_min_interval_seconds == 120
    assert configured.oauth_max_backoff_seconds == 900
