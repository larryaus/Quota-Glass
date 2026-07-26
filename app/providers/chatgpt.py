import json
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from datetime import timedelta
from typing import Any, DefaultDict, Dict, Iterator, List, Optional, Tuple

from app.models import (
    Credits,
    JsonDict,
    LocalUsageWindow,
    Meter,
    ModelUsage,
    ModelUsageWindow,
    ProviderState,
)


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


def parse_chatgpt(
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
    labels = {
        "primary": "Primary limit",
        "secondary": "Secondary limit",
        "individual_limit": "Individual limit",
    }
    for window_key in ("primary", "secondary", "individual_limit"):
        window = rate_limits.get(window_key)
        if not isinstance(window, dict):
            continue
        raw_pct = window.get("used_percent")
        try:
            used_pct = None if raw_pct is None else max(0.0, min(100.0, float(raw_pct)))
        except (TypeError, ValueError):
            used_pct = None
        raw_minutes = window.get("window_minutes")
        raw_reset = window.get("resets_at")
        try:
            window_minutes = None if raw_minutes is None else int(raw_minutes)
        except (TypeError, ValueError):
            window_minutes = None
        try:
            resets_at = None if raw_reset is None else int(raw_reset)
        except (TypeError, ValueError):
            resets_at = None
        meters.append(
            Meter(
                key="chatgpt.%s" % window_key,
                provider="chatgpt",
                label=labels[window_key],
                used_pct=used_pct,
                window_minutes=window_minutes,
                resets_at=resets_at,
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
