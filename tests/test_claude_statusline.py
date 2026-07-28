import json
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.providers.claude import parse_claude
from app.providers.claude_statusline import (
    ClaudeStatuslineSnapshotError,
    capture_statusline_payload,
    read_statusline_snapshot,
)


START = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)


def statusline_payload(five_hour=23.5, seven_day=41.2):
    return {
        "session_id": "must-not-be-persisted",
        "transcript_path": "/private/session.jsonl",
        "rate_limits": {
            "five_hour": {
                "used_percentage": five_hour,
                "resets_at": 1_785_300_000,
            },
            "seven_day": {
                "used_percentage": seven_day,
                "resets_at": 1_785_600_000,
            },
        },
    }


def test_capture_persists_only_private_normalized_quota_fields(tmp_path):
    output = tmp_path / "cache" / "claude.json"

    assert capture_statusline_payload(
        statusline_payload(),
        output,
        captured_at=START,
    )

    saved = json.loads(output.read_text())
    assert saved == {
        "captured_at": int(START.timestamp()),
        "rate_limits": statusline_payload()["rate_limits"],
    }
    assert "session_id" not in saved
    assert "transcript_path" not in saved
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_capture_without_limits_preserves_last_good_snapshot(tmp_path):
    output = tmp_path / "claude.json"
    capture_statusline_payload(statusline_payload(), output, START)
    previous = output.read_bytes()

    assert capture_statusline_payload({"session_id": "early"}, output) is False
    assert output.read_bytes() == previous


def test_capture_rejects_out_of_range_percentage(tmp_path):
    output = tmp_path / "claude.json"
    payload = statusline_payload(five_hour=101)

    assert capture_statusline_payload(payload, output, START) is False
    assert output.exists() is False


def test_script_entrypoint_works_outside_repository(tmp_path):
    output = tmp_path / "claude.json"
    script = (
        Path(__file__).parents[1]
        / "scripts"
        / "claude_quota_statusline.py"
    )

    result = subprocess.run(
        [sys.executable, str(script), "--output", str(output)],
        cwd=tmp_path,
        input=json.dumps(statusline_payload()),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert json.loads(output.read_text())["rate_limits"]["five_hour"][
        "used_percentage"
    ] == 23.5


def test_reader_marks_old_snapshot_stale(tmp_path):
    output = tmp_path / "claude.json"
    capture_statusline_payload(statusline_payload(), output, START)

    fresh = read_statusline_snapshot(
        output,
        now=START + timedelta(minutes=30),
        stale_after_minutes=30,
    )
    stale = read_statusline_snapshot(
        output,
        now=START + timedelta(minutes=30, seconds=1),
        stale_after_minutes=30,
    )

    assert fresh.stale is False
    assert stale.stale is True
    assert stale.age_seconds == 1801


def test_reader_rejects_corrupt_and_future_snapshots(tmp_path):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{")
    with pytest.raises(ClaudeStatuslineSnapshotError, match="could not read"):
        read_statusline_snapshot(corrupt, now=START)

    future = tmp_path / "future.json"
    capture_statusline_payload(
        statusline_payload(),
        future,
        START + timedelta(minutes=6),
    )
    with pytest.raises(
        ClaudeStatuslineSnapshotError,
        match="more than five minutes in the future",
    ):
        read_statusline_snapshot(future, now=START)


@pytest.mark.asyncio
async def test_provider_prefers_statusline_without_touching_oauth(
    monkeypatch,
    fixture_dir,
    tmp_path,
):
    snapshot = tmp_path / "claude.json"
    capture_statusline_payload(
        statusline_payload(five_hour=1.0, seven_day=0.5),
        snapshot,
        START,
    )

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
        enable_oauth=True,
        now=START,
        enable_statusline=True,
        statusline_snapshot_path=snapshot,
    )

    assert state.mode == "statusline"
    assert state.error is None
    assert [meter.source for meter in state.meters] == [
        "statusline",
        "statusline",
    ]
    assert [meter.used_pct for meter in state.meters] == [1.0, 0.5]
    assert all(meter.stale is False for meter in state.meters)
    assert state.local_usage
    assert state.model_usage


@pytest.mark.asyncio
async def test_provider_surfaces_stale_statusline_without_alerting(
    fixture_dir,
    tmp_path,
):
    snapshot = tmp_path / "claude.json"
    capture_statusline_payload(statusline_payload(), snapshot, START)

    state = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=False,
        now=START + timedelta(minutes=31),
        enable_statusline=True,
        statusline_snapshot_path=snapshot,
        statusline_stale_after_minutes=30,
    )

    assert state.mode == "statusline"
    assert all(meter.stale is True for meter in state.meters)
    assert "Stale readings never alert" in state.error


@pytest.mark.asyncio
async def test_provider_marks_a_past_reset_window_stale(
    fixture_dir,
    tmp_path,
):
    snapshot = tmp_path / "claude.json"
    payload = statusline_payload()
    payload["rate_limits"]["five_hour"]["resets_at"] = int(
        (START + timedelta(minutes=5)).timestamp()
    )
    payload["rate_limits"]["seven_day"]["resets_at"] = int(
        (START + timedelta(days=1)).timestamp()
    )
    capture_statusline_payload(payload, snapshot, START)

    state = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=False,
        now=START + timedelta(minutes=6),
        enable_statusline=True,
        statusline_snapshot_path=snapshot,
    )

    assert [meter.stale for meter in state.meters] == [True, False]
    assert "window has reset" in state.error
    assert "Stale readings never alert" in state.error


@pytest.mark.asyncio
async def test_missing_statusline_snapshot_falls_back_to_local(
    fixture_dir,
    tmp_path,
):
    state = await parse_claude(
        fixture_dir / "claude",
        enable_oauth=False,
        now=START,
        enable_statusline=True,
        statusline_snapshot_path=tmp_path / "missing.json",
    )

    assert state.mode == "local"
    assert "status-line quota unavailable" in state.error
    assert "no snapshot exists" in state.error
    assert all(meter.has_quota is False for meter in state.meters)
