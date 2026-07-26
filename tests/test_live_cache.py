from datetime import datetime, timedelta, timezone

from app.providers.live_cache import LiveSourceCache


def _now():
    return datetime(2026, 7, 26, 5, 0, 0, tzinfo=timezone.utc)


def test_first_attempt_is_allowed():
    cache = LiveSourceCache("Codex CLI", min_interval_seconds=300)
    assert cache.should_attempt(_now()) is True


def test_success_suppresses_attempts_until_min_interval():
    cache = LiveSourceCache("Codex CLI", min_interval_seconds=300)
    current = _now()
    cache.record_success({"ok": True}, current)

    assert cache.should_attempt(current + timedelta(seconds=299)) is False
    assert cache.should_attempt(current + timedelta(seconds=300)) is True
    assert cache.age_seconds(current + timedelta(seconds=60)) == 60
    assert cache.payload == {"ok": True}


def test_failure_backs_off_and_reports_reason():
    cache = LiveSourceCache("Codex CLI", min_interval_seconds=300)
    current = _now()
    cache.record_failure(RuntimeError("boom"), current)

    assert cache.is_backed_off(current + timedelta(seconds=10)) is True
    assert "Codex CLI" in cache.backoff_reason
    assert "boom" in cache.backoff_reason
    assert cache.should_attempt(current + timedelta(seconds=10)) is False
    assert cache.should_attempt(current + timedelta(seconds=300)) is True


def test_repeated_failures_grow_the_delay():
    cache = LiveSourceCache(
        "Codex CLI",
        min_interval_seconds=100,
        max_backoff_seconds=400,
    )
    current = _now()
    for _ in range(4):
        cache.record_failure(RuntimeError("boom"), current)
    delay = (cache.next_retry_at - current).total_seconds()

    assert delay == 400


def test_success_clears_backoff():
    cache = LiveSourceCache("Codex CLI", min_interval_seconds=300)
    current = _now()
    cache.record_failure(RuntimeError("boom"), current)
    cache.record_success({"ok": True}, current)

    assert cache.next_retry_at is None
    assert cache.backoff_reason is None
    assert cache.is_backed_off(current) is False


def test_claude_non_rate_limit_failure_does_not_escalate_backoff():
    import httpx

    from app.providers.claude import ClaudeOAuthCache

    cache = ClaudeOAuthCache(min_interval_seconds=300, max_backoff_seconds=3600)
    current = _now()

    def rate_limited():
        response = httpx.Response(
            429,
            request=httpx.Request("GET", "https://example.test"),
        )
        return httpx.HTTPStatusError(
            "429",
            request=response.request,
            response=response,
        )

    cache.record_failure(rate_limited(), current)
    first = (cache.next_retry_at - current).total_seconds()
    cache.record_failure(rate_limited(), current)
    second = (cache.next_retry_at - current).total_seconds()
    cache.record_failure(RuntimeError("network down"), current)
    plain = (cache.next_retry_at - current).total_seconds()
    cache.record_failure(rate_limited(), current)
    third = (cache.next_retry_at - current).total_seconds()

    assert first == 300
    assert second == 600
    assert plain == 300, "a non-429 failure retries at the floor interval"
    assert third == 1200, "a non-429 failure must not reset 429 escalation"
