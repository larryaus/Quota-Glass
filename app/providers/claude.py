"""
Claude OAuth usage uses an undocumented internal endpoint.

It is off by default, may break on any Claude Code update, and must always
degrade safely to local JSONL totals. The rotating Keychain token is re-read
for every actual HTTP request and is never cached. Only successful usage
response payloads are cached in memory.
"""

import asyncio
import getpass
import json
import math
import os
import subprocess
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.models import (
    Credits,
    LocalUsageWindow,
    Meter,
    ModelUsage,
    ModelUsageWindow,
    ProviderState,
)
from app.providers.live_cache import LiveSourceCache


OAUTH_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_WINDOWS = {
    "five_hour": ("Session limit (5h)", 300),
    "seven_day": ("Weekly limit", 10080),
    "seven_day_opus": ("Opus weekly", 10080),
    "seven_day_sonnet": ("Sonnet weekly", 10080),
    "seven_day_overage_included": ("Fable 5 limit", 10080),
    "overage": ("Usage credit limit", None),
}

# USD per million tokens: input, cache write, cache read, output.
# These are deliberately small, documented estimates rather than billing data.
MODEL_PRICES = {
    "opus": (15.0, 18.75, 1.50, 75.0),
    "sonnet": (3.0, 3.75, 0.30, 15.0),
    "haiku": (0.80, 1.0, 0.08, 4.0),
}
DEFAULT_PRICE = MODEL_PRICES["sonnet"]


def _timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _price_for_model(model: str) -> Tuple[float, float, float, float]:
    lowered = model.lower()
    for key, price in MODEL_PRICES.items():
        if key in lowered:
            return price
    return DEFAULT_PRICE


def _usage_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _empty_window(label: str) -> Dict[str, Any]:
    return {
        "label": label,
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
    }


def _model_usage_window(
    label: str,
    window_minutes: int,
    counts: Dict[str, int],
) -> ModelUsageWindow:
    total = sum(counts.values())
    models = [
        ModelUsage(
            model=model,
            tokens=tokens,
            percentage=round(tokens * 100.0 / total, 1) if total else 0.0,
        )
        for model, tokens in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if tokens > 0
    ]
    return ModelUsageWindow(
        label=label,
        window_minutes=window_minutes,
        total_tokens=total,
        models=models,
    )


def parse_claude_local(
    projects_dir: Path,
    now: Optional[datetime] = None,
) -> ProviderState:
    current = now or datetime.now(timezone.utc)
    windows = [
        (timedelta(hours=5), _empty_window("Last 5 hours")),
        (timedelta(days=7), _empty_window("Last 7 days")),
    ]
    model_counts: List[Dict[str, int]] = [{}, {}]
    newest: Optional[datetime] = None
    found_records = 0

    paths = (
        list(Path(projects_dir).glob("**/*.jsonl"))
        if Path(projects_dir).exists()
        else []
    )
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    message = record.get("message")
                    if record.get("type") != "assistant" or not isinstance(message, dict):
                        continue
                    usage = message.get("usage")
                    observed_at = _timestamp(record.get("timestamp"))
                    if not isinstance(usage, dict) or observed_at is None:
                        continue
                    found_records += 1
                    if newest is None or observed_at > newest:
                        newest = observed_at
                    model = str(message.get("model", "unknown"))
                    input_tokens = _usage_count(usage.get("input_tokens"))
                    cache_write = _usage_count(
                        usage.get("cache_creation_input_tokens")
                    )
                    cache_read = _usage_count(usage.get("cache_read_input_tokens"))
                    output_tokens = _usage_count(usage.get("output_tokens"))
                    prices = _price_for_model(model)
                    cost = (
                        input_tokens * prices[0]
                        + cache_write * prices[1]
                        + cache_read * prices[2]
                        + output_tokens * prices[3]
                    ) / 1_000_000.0
                    age = current - observed_at
                    total_tokens = (
                        input_tokens + cache_write + cache_read + output_tokens
                    )
                    for index, (duration, counter) in enumerate(windows):
                        if timedelta(0) <= age <= duration:
                            counter["input_tokens"] += input_tokens
                            counter["cache_creation_input_tokens"] += cache_write
                            counter["cache_read_input_tokens"] += cache_read
                            counter["output_tokens"] += output_tokens
                            counter["total_tokens"] += total_tokens
                            counter["estimated_cost_usd"] += cost
                            model_counts[index][model] = (
                                model_counts[index].get(model, 0) + total_tokens
                            )
        except (OSError, UnicodeDecodeError):
            continue

    local_usage = []
    meters = []
    keys = ("five_hour", "seven_day")
    minutes = (300, 10080)
    for index, (_, raw) in enumerate(windows):
        raw["estimated_cost_usd"] = round(raw["estimated_cost_usd"], 6)
        local_usage.append(LocalUsageWindow(**raw))
        meters.append(
            Meter(
                key="claude.%s" % keys[index],
                provider="claude",
                label=raw["label"],
                used_pct=None,
                window_minutes=minutes[index],
                resets_at=None,
                has_quota=False,
                source="local",
                stale=False,
            )
        )
    error = None
    if not paths or found_records == 0:
        error = "No Claude Code assistant usage records found."
    return ProviderState(
        key="claude",
        label="Claude",
        mode="local",
        meters=meters,
        plan_type="consumer",
        error=error,
        last_updated=newest.isoformat().replace("+00:00", "Z") if newest else None,
        local_usage=local_usage,
        model_usage=[
            _model_usage_window("Last 5 hours", 300, model_counts[0]),
            _model_usage_window("Last 7 days", 10080, model_counts[1]),
        ],
    )


def _read_keychain_token() -> str:
    account = os.environ.get("USER") or getpass.getuser()
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-w",
            "-s",
            "Claude Code-credentials",
            "-a",
            account,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    credentials = json.loads(result.stdout)
    token = credentials.get("claudeAiOauth", {}).get("accessToken")
    if not isinstance(token, str) or not token:
        raise ValueError("Claude OAuth access token was missing from Keychain data")
    return token


async def _fetch_oauth_usage(token: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            OAUTH_URL,
            headers={
                "Authorization": "Bearer %s" % token,
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Claude OAuth response was not an object")
        return data


def _utc_datetime(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso_datetime(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _retry_after_seconds(
    exc: Exception,
    current: datetime,
) -> Optional[int]:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    if exc.response.status_code != 429:
        return None
    value = exc.response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value.strip())
        if math.isfinite(seconds) and seconds >= 0:
            return int(math.ceil(seconds))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at.astimezone(timezone.utc) - current).total_seconds()
        return max(0, int(math.ceil(seconds)))
    except (OverflowError, TypeError, ValueError):
        return None


class ClaudeOAuthCache(LiveSourceCache):
    def __init__(
        self,
        min_interval_seconds: int = 300,
        max_backoff_seconds: int = 3600,
    ) -> None:
        super().__init__(
            "Claude OAuth",
            min_interval_seconds=min_interval_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )

    def _classify_failure(
        self,
        exc: Exception,
        current: datetime,
    ) -> Tuple[int, str]:
        is_rate_limit = (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code == 429
        )
        if not is_rate_limit:
            # Non-429 failures retry at the floor interval and must NOT touch
            # the counter, matching the behaviour this class had before the
            # base class existed.
            return (
                self.min_interval_seconds,
                "Claude OAuth request failed: %s" % exc,
            )
        self._consecutive_failures += 1
        retry_after = _retry_after_seconds(exc, current)
        if retry_after is None:
            delay = self._exponential_backoff_seconds()
            retry_detail = "exponential backoff %ds" % delay
        else:
            delay = retry_after
            retry_detail = "Retry-After %ds" % delay
        return (
            delay,
            "Rate limited by Claude OAuth (429 Too Many Requests; %s): %s"
            % (retry_detail, exc),
        )


def _normalize_reset_at(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        if abs(numeric) >= 100_000_000_000:
            numeric /= 1000.0
        return int(numeric)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return int(parsed.timestamp())
    except (OverflowError, ValueError):
        return None


def _utilization(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            "Claude OAuth field %s has invalid utilization" % field
        )
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(
            "Claude OAuth field %s has invalid utilization" % field
        )
    if not math.isfinite(numeric):
        raise ValueError(
            "Claude OAuth field %s has invalid utilization" % field
        )
    return numeric


def _oauth_meters(data: Dict[str, Any]) -> List[Meter]:
    windows: List[Tuple[str, str, Optional[int], Dict[str, Any], float]] = []
    all_utilizations: List[float] = []

    # Unit selection is payload-wide. Unknown usage windows still participate
    # when they expose a utilization value, avoiding per-field ambiguity at 1.
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        try:
            utilization = _utilization(value.get("utilization"), str(key))
        except ValueError:
            continue
        known_window = OAUTH_WINDOWS.get(key)
        if known_window is None:
            label = str(key).replace("_", " ").strip().capitalize()
            if not label:
                label = "Unknown limit"
            minutes = None
        else:
            label, minutes = known_window
        windows.append((str(key), label, minutes, value, utilization))
        all_utilizations.append(utilization)

    if not windows:
        raise ValueError("Claude OAuth response contained no usable quota windows")

    percentage_scale = 1.0 if any(
        utilization > 1.0 for utilization in all_utilizations
    ) else 100.0
    meters: List[Meter] = []
    for key, label, minutes, value, utilization in windows:
        meters.append(
            Meter(
                key="claude.%s" % key,
                provider="claude",
                label=label,
                used_pct=max(
                    0.0,
                    min(100.0, utilization * percentage_scale),
                ),
                window_minutes=minutes,
                resets_at=_normalize_reset_at(value.get("resets_at")),
                has_quota=True,
                source="oauth",
                stale=False,
            )
        )
    return meters


def _scaled_credit_amount(value: Any, decimal_places: Any) -> Optional[str]:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or isinstance(decimal_places, bool)
        or not isinstance(decimal_places, (int, float))
    ):
        return None
    if isinstance(decimal_places, float):
        if not math.isfinite(decimal_places) or not decimal_places.is_integer():
            return None
    exponent = int(decimal_places)
    if exponent < 0 or exponent > 12:
        return None
    try:
        numeric = Decimal(str(value))
    except InvalidOperation:
        return None
    if not numeric.is_finite():
        return None
    return format(numeric.scaleb(-exponent), ".%df" % exponent)


def _currency(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper()


def _oauth_credits(data: Dict[str, Any]) -> Credits:
    extra_usage = data.get("extra_usage")
    if not isinstance(extra_usage, dict):
        extra_usage = {}
    spend = data.get("spend")
    if not isinstance(spend, dict):
        spend = {}
    spend_used = spend.get("used")
    if not isinstance(spend_used, dict):
        spend_used = {}

    balance = _scaled_credit_amount(
        spend_used.get("amount_minor"),
        spend_used.get("exponent"),
    )
    currency = _currency(spend_used.get("currency"))
    if balance is None:
        balance = _scaled_credit_amount(
            extra_usage.get("used_credits"),
            extra_usage.get("decimal_places"),
        )
        currency = _currency(extra_usage.get("currency"))
    elif currency is None:
        currency = _currency(extra_usage.get("currency"))

    raw_is_enabled = extra_usage.get("is_enabled")
    is_enabled = raw_is_enabled if isinstance(raw_is_enabled, bool) else None
    raw_limit_reached = extra_usage.get("spend_limit_reached")
    spend_limit_reached = (
        raw_limit_reached if isinstance(raw_limit_reached, bool) else False
    )
    return Credits(
        has_credits=balance is not None or is_enabled is True,
        balance=balance or "0",
        currency=currency,
        is_enabled=is_enabled,
        spend_limit_reached=spend_limit_reached,
    )


async def parse_claude(
    projects_dir: Path,
    enable_oauth: bool = False,
    now: Optional[datetime] = None,
    oauth_cache: Optional[ClaudeOAuthCache] = None,
) -> ProviderState:
    try:
        local = await asyncio.to_thread(
            parse_claude_local,
            projects_dir,
            now=now,
        )
    except Exception as exc:
        local = ProviderState(
            key="claude",
            label="Claude",
            mode="local",
            plan_type="consumer",
            error="Claude local usage unavailable: %s" % exc,
        )
    if not enable_oauth:
        return local
    current = _utc_datetime(now)
    cache = oauth_cache or ClaudeOAuthCache()
    if cache.should_attempt(current):
        try:
            # Read only when a request is due, and immediately before it:
            # Claude Code rotates this token frequently.
            token = await asyncio.to_thread(_read_keychain_token)
            data = await _fetch_oauth_usage(token)
            # Validate before caching so a successful cache entry always
            # contains quota meters that can be served on later failures.
            _oauth_meters(data)
            cache.record_success(data, current)
        except Exception as exc:
            cache.record_failure(exc, current)

    cache_age = cache.age_seconds(current)
    backed_off = cache.is_backed_off(current)
    next_retry_at = (
        _iso_datetime(cache.next_retry_at)
        if cache.next_retry_at is not None
        else None
    )
    if cache.payload is not None and cache.fetched_at is not None:
        meters = _oauth_meters(cache.payload)
        if backed_off:
            for meter in meters:
                meter.stale = True
        error = None
        if backed_off:
            error = (
                "Claude OAuth unavailable; showing cached OAuth quota data "
                "(%ds old). %s Next attempt at %s."
                % (
                    cache_age or 0,
                    cache.backoff_reason,
                    next_retry_at,
                )
            )
        return ProviderState(
            key="claude",
            label="Claude",
            mode="oauth",
            meters=meters,
            credits=_oauth_credits(cache.payload),
            plan_type="consumer",
            error=error,
            last_updated=_iso_datetime(cache.fetched_at),
            oauth_backed_off=backed_off,
            oauth_backoff_reason=cache.backoff_reason if backed_off else None,
            oauth_cache_age_seconds=cache_age,
            oauth_next_retry_at=next_retry_at if backed_off else None,
            local_usage=local.local_usage,
            model_usage=local.model_usage,
        )

    reason = cache.backoff_reason or "No successful Claude OAuth response."
    local.error = (
        "Claude OAuth unavailable; showing local estimates: %s "
        "Next attempt at %s."
        % (reason, next_retry_at)
    )
    local.oauth_backed_off = backed_off
    local.oauth_backoff_reason = reason
    local.oauth_next_retry_at = next_retry_at
    return local
