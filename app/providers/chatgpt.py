import json
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from datetime import timedelta
from typing import Any, Callable, DefaultDict, Dict, Iterator, List, Optional, Tuple

from app.models import (
    Credits,
    JsonDict,
    LocalUsageWindow,
    Meter,
    ModelUsage,
    ModelUsageWindow,
    ProviderState,
)
from app.providers.codex_cli import (
    CodexCliError,
    CodexCliProtocolError,
    read_account_rate_limits,
)
from app.providers.live_cache import LiveSourceCache


DEFAULT_CANDIDATE_FILE_COUNT = 5
MAX_SCAN_MULTIPLIER = 10
FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 60
USAGE_WINDOWS: Tuple[Tuple[str, int], ...] = (
    ("Last 5 hours", 300),
    ("Last 7 days", 10080),
)
USAGE_COMPONENTS: Tuple[str, ...] = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
WINDOW_LABELS = {
    "primary": "Primary limit",
    "secondary": "Secondary limit",
    "individual_limit": "Individual limit",
}
LIVE_WINDOW_KEYS: Tuple[Tuple[str, str], ...] = (
    ("primary", "primary"),
    ("secondary", "secondary"),
    ("individualLimit", "individual_limit"),
)


def _clamp_percent(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _reverse_lines(path: Path, chunk_size: int = 64 * 1024) -> Iterator[str]:
    """Yield a JSONL file from its newest line without loading it all in memory."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        remainder = b""
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size) + remainder
            lines = block.split(b"\n")
            remainder = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line.decode("utf-8", errors="replace")
        if remainder:
            yield remainder.decode("utf-8", errors="replace")


def _file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _latest_usable_snapshot(path: Path) -> Optional[JsonDict]:
    try:
        for raw_line in _reverse_lines(path):
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if (
                record.get("type") == "event_msg"
                and isinstance(payload, dict)
                and payload.get("type") == "token_count"
                and isinstance(payload.get("rate_limits"), dict)
            ):
                return record
    except OSError:
        return None
    return None


def _latest_snapshot(
    root: Path,
    candidate_file_count: int = DEFAULT_CANDIDATE_FILE_COUNT,
) -> Optional[JsonDict]:
    if not root.exists():
        return None
    wanted = max(1, candidate_file_count)
    paths = sorted(
        root.glob("*/*/*/rollout-*.jsonl"),
        key=_file_mtime,
        reverse=True,
    )[: wanted * MAX_SCAN_MULTIPLIER]
    candidates: List[Tuple[JsonDict, Optional[datetime]]] = []
    for path in paths:
        record = _latest_usable_snapshot(path)
        if record is None:
            continue
        candidates.append((record, _parse_timestamp(record.get("timestamp"))))
        if len(candidates) >= wanted:
            break
    timestamped = [candidate for candidate in candidates if candidate[1] is not None]
    if timestamped:
        return max(
            timestamped,
            key=lambda candidate: candidate[1],
        )[0]
    return candidates[0][0] if candidates else None


def _usage_tokens(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    raw_total = value.get("total_tokens")
    try:
        if raw_total is not None:
            return max(0, int(raw_total))
    except (TypeError, ValueError):
        return 0
    total = 0
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        try:
            total += max(0, int(value.get(key, 0) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _usage_breakdown(value: Any) -> Dict[str, int]:
    breakdown = {
        component: 0
        for component in USAGE_COMPONENTS
    }
    if isinstance(value, dict):
        for component in USAGE_COMPONENTS:
            try:
                breakdown[component] = max(0, int(value.get(component, 0) or 0))
            except (TypeError, ValueError):
                continue
    breakdown["total_tokens"] = _usage_tokens(value)
    return breakdown


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


def _chatgpt_usage(
    root: Path,
    current: datetime,
) -> Tuple[List[LocalUsageWindow], List[ModelUsageWindow]]:
    cutoffs = [
        current - timedelta(minutes=window_minutes)
        for _, window_minutes in USAGE_WINDOWS
    ]
    model_counts: List[DefaultDict[str, int]] = [
        defaultdict(int)
        for _ in USAGE_WINDOWS
    ]
    local_counts: List[DefaultDict[str, int]] = [
        defaultdict(int)
        for _ in USAGE_WINDOWS
    ]
    if root.exists():
        for path in root.glob("*/*/*/rollout-*.jsonl"):
            active_model = "unknown"
            previous_total = 0
            previous_usage = _usage_breakdown(None)
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        payload = record.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        if record.get("type") == "turn_context":
                            model = payload.get("model")
                            if isinstance(model, str) and model:
                                active_model = model
                            continue
                        if (
                            record.get("type") == "event_msg"
                            and payload.get("type") == "thread_settings_applied"
                        ):
                            settings = payload.get("thread_settings")
                            if isinstance(settings, dict):
                                model = settings.get("model")
                                if isinstance(model, str) and model:
                                    active_model = model
                            continue
                        if (
                            record.get("type") != "event_msg"
                            or payload.get("type") != "token_count"
                        ):
                            continue
                        info = payload.get("info")
                        if not isinstance(info, dict):
                            continue
                        timestamp = _parse_timestamp(record.get("timestamp"))
                        last_token_usage = _usage_breakdown(
                            info.get("last_token_usage")
                        )
                        cumulative_usage = _usage_breakdown(
                            info.get("total_token_usage")
                        )
                        last_tokens = last_token_usage["total_tokens"]
                        cumulative = cumulative_usage["total_tokens"]
                        if last_tokens <= 0 and cumulative >= previous_total:
                            last_tokens = cumulative - previous_total
                            last_token_usage = {
                                component: max(
                                    0,
                                    cumulative_usage[component]
                                    - previous_usage[component],
                                )
                                for component in USAGE_COMPONENTS
                            }
                            last_token_usage["total_tokens"] = last_tokens
                        previous_total = max(previous_total, cumulative)
                        for component in USAGE_COMPONENTS:
                            previous_usage[component] = max(
                                previous_usage[component],
                                cumulative_usage[component],
                            )
                        previous_usage["total_tokens"] = previous_total
                        if timestamp is None or last_tokens <= 0:
                            continue
                        for index, cutoff in enumerate(cutoffs):
                            if cutoff <= timestamp <= current:
                                model_counts[index][active_model] += last_tokens
                                for component in USAGE_COMPONENTS:
                                    local_counts[index][component] += (
                                        last_token_usage[component]
                                    )
                                local_counts[index]["total_tokens"] += last_tokens
            except OSError:
                continue
    local_usage = [
        LocalUsageWindow(
            label=label,
            input_tokens=local_counts[index]["input_tokens"],
            cached_input_tokens=local_counts[index]["cached_input_tokens"],
            output_tokens=local_counts[index]["output_tokens"],
            reasoning_output_tokens=local_counts[index][
                "reasoning_output_tokens"
            ],
            total_tokens=local_counts[index]["total_tokens"],
            estimated_cost_usd=None,
        )
        for index, (label, _) in enumerate(USAGE_WINDOWS)
    ]
    model_usage = [
        _model_usage_window(
            label,
            window_minutes,
            model_counts[index],
        )
        for index, (label, window_minutes) in enumerate(USAGE_WINDOWS)
    ]
    return local_usage, model_usage


def _parse_chatgpt_rollout(
    sessions_dir: Path,
    stale_after_minutes: int = 30,
    now: Optional[datetime] = None,
    candidate_file_count: int = DEFAULT_CANDIDATE_FILE_COUNT,
) -> ProviderState:
    snapshot = _latest_snapshot(Path(sessions_dir), candidate_file_count)
    if snapshot is None:
        return ProviderState(
            key="chatgpt",
            label="ChatGPT",
            mode="local",
            error="No Codex session usage snapshots found.",
        )

    payload = snapshot["payload"]
    rate_limits = payload["rate_limits"]
    timestamp_text = snapshot.get("timestamp")
    timestamp = _parse_timestamp(timestamp_text)
    current = now or datetime.now(timezone.utc)
    stale = True
    if timestamp is not None:
        skew_seconds = (current - timestamp).total_seconds()
        stale = (
            abs(skew_seconds) > stale_after_minutes * 60
            or skew_seconds < -FUTURE_TIMESTAMP_TOLERANCE_SECONDS
        )

    meters: List[Meter] = []
    for window_key in ("primary", "secondary", "individual_limit"):
        window = rate_limits.get(window_key)
        if not isinstance(window, dict):
            continue
        meters.append(
            Meter(
                key="chatgpt.%s" % window_key,
                provider="chatgpt",
                label=WINDOW_LABELS[window_key],
                used_pct=_clamp_percent(window.get("used_percent")),
                window_minutes=_optional_int(window.get("window_minutes")),
                resets_at=_optional_int(window.get("resets_at")),
                has_quota=True,
                source="rollout",
                stale=stale,
            )
        )

    raw_credits = rate_limits.get("credits")
    if not isinstance(raw_credits, dict):
        raw_credits = {}
    credits = Credits(
        has_credits=bool(raw_credits.get("has_credits", False)),
        unlimited=bool(raw_credits.get("unlimited", False)),
        balance=str(raw_credits.get("balance", "0")),
    )
    error = None
    if not meters:
        error = "The latest Codex snapshot did not contain any quota windows."
    local_usage, model_usage = _chatgpt_usage(Path(sessions_dir), current)
    return ProviderState(
        key="chatgpt",
        label="ChatGPT",
        mode="local",
        meters=meters,
        credits=credits,
        plan_type=rate_limits.get("plan_type"),
        error=error,
        last_updated=timestamp_text if isinstance(timestamp_text, str) else None,
        local_usage=local_usage,
        model_usage=model_usage,
    )


def _live_meters(rate_limits: JsonDict) -> List[Meter]:
    meters: List[Meter] = []
    for source_key, meter_suffix in LIVE_WINDOW_KEYS:
        window = rate_limits.get(source_key)
        if not isinstance(window, dict):
            continue
        meters.append(
            Meter(
                key="chatgpt.%s" % meter_suffix,
                provider="chatgpt",
                label=WINDOW_LABELS[meter_suffix],
                used_pct=_clamp_percent(window.get("usedPercent")),
                window_minutes=_optional_int(window.get("windowDurationMins")),
                resets_at=_optional_int(window.get("resetsAt")),
                has_quota=True,
                source="app-server",
                stale=False,
            )
        )
    return meters


def _live_reading(payload: Any) -> Tuple[JsonDict, List[Meter]]:
    """Return the rate limits and meters carried by a live payload.

    Raises CodexCliProtocolError when the payload cannot produce a single
    quota window, so a renamed or restructured response is treated as a live
    failure instead of as a successful reading with an empty card.
    """
    if not isinstance(payload, dict):
        raise CodexCliProtocolError(
            "codex app-server returned a non-object rate limit payload"
        )
    rate_limits = payload.get("rateLimits")
    if not isinstance(rate_limits, dict):
        raise CodexCliProtocolError(
            "codex app-server response had no rateLimits object"
        )
    meters = _live_meters(rate_limits)
    if not meters:
        raise CodexCliProtocolError(
            "codex app-server response contained no recognizable quota windows"
        )
    return rate_limits, meters


def _live_credits(rate_limits: JsonDict) -> Credits:
    raw = rate_limits.get("credits")
    if not isinstance(raw, dict):
        raw = {}
    return Credits(
        has_credits=bool(raw.get("hasCredits", False)),
        unlimited=bool(raw.get("unlimited", False)),
        balance=str(raw.get("balance", "0")),
        spend_limit_reached=bool(rate_limits.get("spendControlReached", False)),
    )


def parse_chatgpt(
    sessions_dir: Path,
    stale_after_minutes: int = 30,
    now: Optional[datetime] = None,
    candidate_file_count: int = DEFAULT_CANDIDATE_FILE_COUNT,
    enable_live: bool = False,
    live_cache: Optional[LiveSourceCache] = None,
    cli_path: str = "codex",
    cli_timeout_seconds: int = 20,
    live_reader: Optional[Callable[[str, int], Dict[str, Any]]] = None,
) -> ProviderState:
    if not enable_live or live_cache is None:
        return _parse_chatgpt_rollout(
            sessions_dir,
            stale_after_minutes,
            now,
            candidate_file_count,
        )

    reader = live_reader or read_account_rate_limits
    current = now or datetime.now(timezone.utc)
    if live_cache.should_attempt(current):
        try:
            payload = reader(cli_path, cli_timeout_seconds)
            # Validate before caching so a successful cache entry always
            # contains quota meters that can be served on later failures.
            _live_reading(payload)
            live_cache.record_success(payload, current)
        except CodexCliError as exc:
            live_cache.record_failure(exc, current)
        except Exception as exc:  # noqa: BLE001 - never let the poller die
            live_cache.record_failure(exc, current)

    reading: Optional[Tuple[JsonDict, List[Meter]]] = None
    if live_cache.payload is not None and live_cache.fetched_at is not None:
        try:
            reading = _live_reading(live_cache.payload)
        except CodexCliError:
            # Defensive: a cache entry is validated before it is stored, so
            # this only fires if a payload was injected past that guard.
            reading = None
    if reading is None:
        # Never succeeded: a rollout reading beats no reading at all.
        return _parse_chatgpt_rollout(
            sessions_dir,
            stale_after_minutes,
            now,
            candidate_file_count,
        )
    rate_limits, meters = reading

    local_usage, model_usage = _chatgpt_usage(Path(sessions_dir), current)
    backed_off = live_cache.is_backed_off(current)
    return ProviderState(
        key="chatgpt",
        label="ChatGPT",
        mode="oauth",
        meters=meters,
        credits=_live_credits(rate_limits),
        plan_type=rate_limits.get("planType"),
        error=None,
        last_updated=_iso_datetime(live_cache.fetched_at)
        if live_cache.fetched_at is not None
        else None,
        oauth_backed_off=backed_off,
        oauth_backoff_reason=live_cache.backoff_reason if backed_off else None,
        oauth_cache_age_seconds=live_cache.age_seconds(current),
        oauth_next_retry_at=_iso_datetime(live_cache.next_retry_at)
        if backed_off and live_cache.next_retry_at is not None
        else None,
        local_usage=local_usage,
        model_usage=model_usage,
    )
