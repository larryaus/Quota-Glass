import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.providers.chatgpt import parse_chatgpt
from app.providers.claude import (
    _oauth_credits,
    _oauth_meters,
    parse_claude,
    parse_claude_local,
)


def _token_record(timestamp, used_pct):
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {
                    "used_percent": used_pct,
                    "window_minutes": 300,
                    "resets_at": 1_800_000_000,
                }
            },
        },
    }


def _write_snapshot(path, timestamp, used_pct):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_token_record(timestamp, used_pct)) + "\n",
        encoding="utf-8",
    )


def test_chatgpt_rollout_parser_uses_latest_token_snapshot(fixture_dir):
    state = parse_chatgpt(
        fixture_dir / "chatgpt",
        stale_after_minutes=30,
        now=datetime(2026, 7, 25, 22, 20, tzinfo=timezone.utc),
    )
    assert state.error is None
    assert state.plan_type == "plus"
    assert state.credits.balance == "0"
    assert len(state.meters) == 1
    assert state.meters[0].key == "chatgpt.primary"
    assert state.meters[0].used_pct == 87.5
    assert state.meters[0].window_minutes == 10080
    assert state.meters[0].resets_at == 1785621948
    assert state.meters[0].stale is False


def test_chatgpt_marks_old_snapshot_stale(fixture_dir):
    state = parse_chatgpt(
        fixture_dir / "chatgpt",
        stale_after_minutes=30,
        now=datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc),
    )
    assert state.meters[0].stale is True


def test_chatgpt_selects_greatest_record_timestamp_across_recent_files(
    tmp_path,
):
    older_directory = (
        tmp_path / "2026" / "07" / "25" / "rollout-active.jsonl"
    )
    newer_directory = (
        tmp_path / "2026" / "07" / "26" / "rollout-stale.jsonl"
    )
    _write_snapshot(
        older_directory,
        "2026-07-26T12:59:00Z",
        91,
    )
    _write_snapshot(
        newer_directory,
        "2026-07-26T12:00:00Z",
        12,
    )
    os.utime(older_directory, (100, 100))
    os.utime(newer_directory, (200, 200))

    state = parse_chatgpt(
        tmp_path,
        now=datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc),
    )

    assert state.meters[0].used_pct == 91
    assert state.last_updated == "2026-07-26T12:59:00Z"


@pytest.mark.parametrize("timestamp", [None, 12345])
def test_chatgpt_non_string_timestamp_is_safe_and_stale(tmp_path, timestamp):
    path = tmp_path / "2026" / "07" / "26" / "rollout-bad-time.jsonl"
    _write_snapshot(path, timestamp, 50)

    state = parse_chatgpt(
        tmp_path,
        now=datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc),
    )

    assert state.meters[0].used_pct == 50
    assert state.meters[0].stale is True
    assert state.last_updated is None


def test_chatgpt_marks_slightly_future_snapshot_stale(tmp_path):
    path = tmp_path / "2026" / "07" / "26" / "rollout-future.jsonl"
    _write_snapshot(path, "2026-07-26T13:02:00Z", 50)

    state = parse_chatgpt(
        tmp_path,
        stale_after_minutes=30,
        now=datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc),
    )

    assert state.meters[0].stale is True


def test_chatgpt_aggregates_model_mix_from_turn_token_deltas(tmp_path):
    path = tmp_path / "2026" / "07" / "26" / "rollout-models.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-07-26T03:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex"},
        },
        {
            "timestamp": "2026-07-26T03:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 200,
                        "cached_input_tokens": 50,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 20,
                        "total_tokens": 300,
                    },
                    "total_token_usage": {
                        "input_tokens": 200,
                        "cached_input_tokens": 50,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 20,
                        "total_tokens": 300,
                    },
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 10,
                        "window_minutes": 10080,
                        "resets_at": 1_800_000_000,
                    }
                },
            },
        },
        {
            "timestamp": "2026-07-26T10:02:00Z",
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_settings": {"model": "gpt-5.3-codex-spark"},
            },
        },
        {
            "timestamp": "2026-07-26T10:03:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 70,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 10,
                        "total_tokens": 100,
                    },
                    "total_token_usage": {
                        "input_tokens": 270,
                        "cached_input_tokens": 70,
                        "output_tokens": 130,
                        "reasoning_output_tokens": 30,
                        "total_tokens": 400,
                    },
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 11,
                        "window_minutes": 10080,
                        "resets_at": 1_800_000_000,
                    }
                },
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    state = parse_chatgpt(
        tmp_path,
        now=datetime(2026, 7, 26, 10, 5, tzinfo=timezone.utc),
    )

    assert [usage.label for usage in state.model_usage] == [
        "Last 5 hours",
        "Last 7 days",
    ]
    five_hour_models, seven_day_models = state.model_usage
    assert five_hour_models.total_tokens == 100
    assert [
        (item.model, item.tokens, item.percentage)
        for item in five_hour_models.models
    ] == [
        ("gpt-5.3-codex-spark", 100, 100.0),
    ]
    assert seven_day_models.total_tokens == 400
    assert [
        (item.model, item.tokens, item.percentage)
        for item in seven_day_models.models
    ] == [
        ("gpt-5.3-codex", 300, 75.0),
        ("gpt-5.3-codex-spark", 100, 25.0),
    ]
    five_hour, seven_day = state.local_usage
    assert five_hour.total_tokens == 100
    assert five_hour.input_tokens == 70
    assert five_hour.cached_input_tokens == 20
    assert five_hour.output_tokens == 30
    assert five_hour.reasoning_output_tokens == 10
    assert seven_day.total_tokens == 400
    assert seven_day.input_tokens == 270
    assert seven_day.cached_input_tokens == 70
    assert seven_day.output_tokens == 130
    assert seven_day.reasoning_output_tokens == 30
    assert all(
        usage.estimated_cost_usd is None
        for usage in state.local_usage
    )


def test_chatgpt_cumulative_baseline_excludes_pre_window_usage(tmp_path):
    path = tmp_path / "2026" / "07" / "26" / "rollout-baseline.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-07-26T05:59:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol"},
        },
        {
            "timestamp": "2026-07-26T06:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {},
                    "total_token_usage": {
                        "input_tokens": 900,
                        "cached_input_tokens": 200,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 20,
                        "total_tokens": 1000,
                    },
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 10,
                        "window_minutes": 10080,
                        "resets_at": 1_800_000_000,
                    }
                },
            },
        },
        {
            "timestamp": "2026-07-26T11:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {},
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 250,
                        "output_tokens": 150,
                        "reasoning_output_tokens": 30,
                        "total_tokens": 1150,
                    },
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 12,
                        "window_minutes": 10080,
                        "resets_at": 1_800_000_100,
                    }
                },
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    state = parse_chatgpt(
        tmp_path,
        now=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
    )

    five_hour, seven_day = state.local_usage
    assert five_hour.total_tokens == 150
    assert five_hour.input_tokens == 100
    assert five_hour.cached_input_tokens == 50
    assert five_hour.output_tokens == 50
    assert five_hour.reasoning_output_tokens == 10
    assert seven_day.total_tokens == 1150
    assert state.model_usage[0].total_tokens == 150
    assert state.model_usage[1].total_tokens == 1150
    assert state.meters[0].used_pct == 12
    assert state.meters[0].window_minutes == 10080
    assert state.meters[0].resets_at == 1_800_000_100


def test_chatgpt_includes_empty_fixed_window(tmp_path):
    path = tmp_path / "2026" / "07" / "26" / "rollout-empty-window.jsonl"
    path.parent.mkdir(parents=True)
    record = {
        "timestamp": "2026-07-26T06:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 40,
                    "output_tokens": 20,
                    "total_tokens": 60,
                },
                "total_token_usage": {
                    "input_tokens": 40,
                    "output_tokens": 20,
                    "total_tokens": 60,
                },
            },
            "rate_limits": {
                "primary": {
                    "used_percent": 20,
                    "window_minutes": 10080,
                    "resets_at": 1_800_000_000,
                }
            },
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    state = parse_chatgpt(
        tmp_path,
        now=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
    )

    assert [
        (usage.label, usage.total_tokens)
        for usage in state.local_usage
    ] == [
        ("Last 5 hours", 0),
        ("Last 7 days", 60),
    ]
    assert [
        (usage.label, usage.window_minutes, usage.total_tokens)
        for usage in state.model_usage
    ] == [
        ("Last 5 hours", 300, 0),
        ("Last 7 days", 10080, 60),
    ]
    assert state.model_usage[0].models == []


def test_chatgpt_rollout_usage_files_are_opened_once(tmp_path, monkeypatch):
    path = tmp_path / "2026" / "07" / "26" / "rollout-single-pass.jsonl"
    _write_snapshot(path, "2026-07-26T11:59:00Z", 20)
    real_open = Path.open
    usage_opens = []

    def counting_open(target, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if target.name.startswith("rollout-") and mode == "r":
            usage_opens.append(target)
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    parse_chatgpt(
        tmp_path,
        now=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
    )

    assert usage_opens == [path]


def test_claude_local_parser_aggregates_windows_and_cost(fixture_dir):
    state = parse_claude_local(
        fixture_dir / "claude",
        now=datetime(2026, 7, 25, 22, 30, tzinfo=timezone.utc),
    )
    assert state.mode == "local"
    assert state.error is None
    assert all(not item.has_quota for item in state.meters)
    assert all(item.used_pct is None for item in state.meters)
    five_hour, seven_day = state.local_usage
    assert five_hour.total_tokens == 2260
    assert seven_day.total_tokens == 2860
    assert five_hour.estimated_cost_usd == pytest.approx(0.00495)
    assert seven_day.estimated_cost_usd > five_hour.estimated_cost_usd
    five_hour_models, seven_day_models = state.model_usage
    assert five_hour_models.total_tokens == 2260
    assert [(item.model, item.percentage) for item in five_hour_models.models] == [
        ("claude-sonnet-4-20250514", 100.0)
    ]
    assert seven_day_models.total_tokens == 2860
    assert [(item.model, item.percentage) for item in seven_day_models.models] == [
        ("claude-sonnet-4-20250514", 79.0),
        ("claude-haiku-3-5", 21.0),
    ]


def test_claude_local_parser_tolerates_truncated_utf8(tmp_path):
    path = tmp_path / "project" / "session.jsonl"
    path.parent.mkdir(parents=True)
    record = {
        "type": "assistant",
        "timestamp": "2026-07-25T22:00:00Z",
        "message": {
            "model": "claude-sonnet",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
            },
        },
    }
    path.write_bytes(
        (json.dumps(record) + "\n").encode("utf-8") + b'{"broken":"\xe2\x82'
    )

    state = parse_claude_local(
        tmp_path,
        now=datetime(2026, 7, 25, 22, 30, tzinfo=timezone.utc),
    )

    assert state.error is None
    assert state.local_usage[0].total_tokens == 15


def test_claude_oauth_detects_utilization_scale_once_per_payload():
    meters = _oauth_meters(
        {
            "five_hour": {
                "utilization": 1.0,
                "resets_at": 1_800_000_000,
            },
            "seven_day": {
                "utilization": 86,
                "resets_at": 1_800_000_000,
            },
        }
    )
    fractions = _oauth_meters(
        {
            "five_hour": {
                "utilization": 0.86,
                "resets_at": 1_800_000_000,
            },
            "seven_day": {
                "utilization": 1.0,
                "resets_at": 1_800_000_000,
            },
        }
    )

    assert [meter.used_pct for meter in meters] == [1.0, 86.0]
    assert [meter.used_pct for meter in fractions] == [86.0, 100.0]


def test_claude_oauth_normalizes_reset_timestamp_shapes():
    meters = _oauth_meters(
        {
            "five_hour": {
                "utilization": 0.1,
                "resets_at": 1_800_000_000,
            },
            "seven_day": {
                "utilization": 0.2,
                "resets_at": 1_800_000_000_000,
            },
            "seven_day_opus": {
                "utilization": 0.3,
                "resets_at": "2026-01-29T10:00:00Z",
            },
            "seven_day_sonnet": {
                "utilization": 0.4,
                "resets_at": "2026-01-29T11:00:00+01:00",
            },
            "overage": {
                "utilization": 0.5,
                "resets_at": "not-a-timestamp",
            },
        }
    )

    expected_iso = int(
        datetime(2026, 1, 29, 10, 0, tzinfo=timezone.utc).timestamp()
    )
    assert [meter.resets_at for meter in meters] == [
        1_800_000_000,
        1_800_000_000,
        expected_iso,
        expected_iso,
        None,
    ]


@pytest.mark.asyncio
async def test_claude_real_oauth_payload_is_successful(
    monkeypatch,
    fixture_dir,
    claude_oauth_payload,
):
    async def oauth_usage(token):
        return claude_oauth_payload

    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        lambda: "token",
    )
    monkeypatch.setattr("app.providers.claude._fetch_oauth_usage", oauth_usage)

    state = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=True,
        now=datetime(2026, 7, 26, 2, 0, tzinfo=timezone.utc),
    )

    assert state.mode == "oauth"
    assert state.error is None
    assert len(state.meters) == 2
    assert [meter.key for meter in state.meters] == [
        "claude.five_hour",
        "claude.seven_day",
    ]
    assert [meter.used_pct for meter in state.meters] == [100.0, 12.0]
    assert all(meter.has_quota for meter in state.meters)
    assert [meter.resets_at for meter in state.meters] == [
        int(
            datetime(
                2026,
                7,
                26,
                2,
                9,
                59,
                368138,
                tzinfo=timezone.utc,
            ).timestamp()
        ),
        int(
            datetime(
                2026,
                7,
                30,
                1,
                59,
                59,
                368154,
                tzinfo=timezone.utc,
            ).timestamp()
        ),
    ]


def test_claude_oauth_extra_usage_is_not_a_meter():
    meters = _oauth_meters(
        {
            "five_hour": {"utilization": 50, "resets_at": None},
            "extra_usage": {
                "utilization": None,
                "is_enabled": True,
            },
        }
    )

    assert [meter.key for meter in meters] == ["claude.five_hour"]


def test_claude_oauth_ignores_unknown_null_codenames():
    meters = _oauth_meters(
        {
            "five_hour": {"utilization": 50, "resets_at": None},
            "tangelo": None,
            "iguana_necktie": None,
            "brand_new_codename": None,
        }
    )

    assert [meter.key for meter in meters] == ["claude.five_hour"]


def test_claude_oauth_surfaces_unknown_valid_window():
    meters = _oauth_meters(
        {
            "brand_new_window": {
                "utilization": 0.42,
                "resets_at": "2026-07-30T01:59:59+00:00",
            }
        }
    )

    assert len(meters) == 1
    assert meters[0].key == "claude.brand_new_window"
    assert meters[0].label == "Brand new window"
    assert meters[0].used_pct == 42.0


def test_claude_oauth_credits_use_spend_minor_units(claude_oauth_payload):
    credits = _oauth_credits(claude_oauth_payload)

    assert credits.has_credits is True
    assert credits.balance == "9.73"
    assert credits.currency == "AUD"
    assert credits.is_enabled is True
    assert credits.spend_limit_reached is False


def test_claude_oauth_credits_fall_back_to_extra_usage():
    credits = _oauth_credits(
        {
            "extra_usage": {
                "is_enabled": False,
                "used_credits": 1234,
                "currency": "EUR",
                "decimal_places": 2,
                "spend_limit_reached": True,
            },
            "spend": {"used": None},
        }
    )

    assert credits.balance == "12.34"
    assert credits.currency == "EUR"
    assert credits.is_enabled is False
    assert credits.spend_limit_reached is True


@pytest.mark.asyncio
async def test_claude_default_does_not_touch_oauth(monkeypatch, fixture_dir):
    def forbidden():
        raise AssertionError("Keychain should not be read")

    async def forbidden_network(token):
        raise AssertionError("Network should not be used")

    monkeypatch.setattr("app.providers.claude._read_keychain_token", forbidden)
    monkeypatch.setattr(
        "app.providers.claude._fetch_oauth_usage",
        forbidden_network,
    )
    state = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=False,
        now=datetime(2026, 7, 25, 22, 30, tzinfo=timezone.utc),
    )
    assert state.mode == "local"


@pytest.mark.asyncio
async def test_claude_oauth_failure_degrades_to_local(monkeypatch, fixture_dir):
    def broken_keychain():
        raise RuntimeError("locked")

    monkeypatch.setattr("app.providers.claude._read_keychain_token", broken_keychain)
    state = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=True,
        now=datetime(2026, 7, 25, 22, 30, tzinfo=timezone.utc),
    )
    assert state.mode == "local"
    assert "OAuth unavailable" in state.error
    assert len(state.local_usage) == 2


@pytest.mark.asyncio
async def test_claude_unrecognized_oauth_payload_degrades_without_percentage(
    monkeypatch,
    fixture_dir,
):
    async def malformed_usage(token):
        return {"five_hour": {"utilization": "86", "resets_at": "bad"}}

    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        lambda: "token",
    )
    monkeypatch.setattr(
        "app.providers.claude._fetch_oauth_usage",
        malformed_usage,
    )

    state = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=True,
        now=datetime(2026, 7, 25, 22, 30, tzinfo=timezone.utc),
    )

    assert state.mode == "local"
    assert "no usable quota windows" in state.error
    assert all(meter.used_pct is None for meter in state.meters)


@pytest.mark.asyncio
async def test_claude_oauth_with_no_usable_windows_degrades_cleanly(
    monkeypatch,
    fixture_dir,
):
    async def no_windows(token):
        return {
            "seven_day_opus": None,
            "extra_usage": {"utilization": None},
            "limits": [],
            "member_dashboard_available": False,
        }

    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        lambda: "token",
    )
    monkeypatch.setattr("app.providers.claude._fetch_oauth_usage", no_windows)

    state = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=True,
        now=datetime(2026, 7, 25, 22, 30, tzinfo=timezone.utc),
    )

    assert state.mode == "local"
    assert state.error is not None
    assert "no usable quota windows" in state.error
    assert all(meter.used_pct is None for meter in state.meters)


@pytest.mark.asyncio
async def test_claude_oauth_still_polls_when_local_parser_fails(
    monkeypatch,
    fixture_dir,
):
    def broken_local(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xe2", 0, 1, "truncated")

    async def oauth_usage(token):
        return {
            "five_hour": {
                "utilization": 0.75,
                "resets_at": "2026-07-26T10:00:00Z",
            }
        }

    monkeypatch.setattr("app.providers.claude.parse_claude_local", broken_local)
    monkeypatch.setattr(
        "app.providers.claude._read_keychain_token",
        lambda: "token",
    )
    monkeypatch.setattr("app.providers.claude._fetch_oauth_usage", oauth_usage)

    state = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=True,
        now=datetime(2026, 7, 25, 22, 30, tzinfo=timezone.utc),
    )

    assert state.mode == "oauth"
    assert state.meters[0].used_pct == 75
