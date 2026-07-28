"""Capture and read Claude Code's documented status-line quota payload.

Claude Code passes subscription rate limits to configured status-line commands
on stdin. This bridge persists only those quota fields, never the surrounding
session metadata, transcript path, or credentials.
"""

import argparse
import json
import math
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


WINDOW_KEYS = ("five_hour", "seven_day")
DEFAULT_SNAPSHOT_PATH = (
    "~/Library/Caches/QuotaGlass/claude-rate-limits.json"
)


class ClaudeStatuslineSnapshotError(RuntimeError):
    pass


@dataclass
class ClaudeStatuslineSnapshot:
    rate_limits: Dict[str, Dict[str, Any]]
    captured_at: datetime
    age_seconds: int
    stale: bool


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClaudeStatuslineSnapshotError(
            "Claude status-line field %s was not numeric" % field
        )
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ClaudeStatuslineSnapshotError(
            "Claude status-line field %s was not finite" % field
        )
    return numeric


def _normalize_window(key: str, value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ClaudeStatuslineSnapshotError(
            "Claude status-line field rate_limits.%s was not an object" % key
        )
    used = _number(
        value.get("used_percentage"),
        "rate_limits.%s.used_percentage" % key,
    )
    if used < 0 or used > 100:
        raise ClaudeStatuslineSnapshotError(
            "Claude status-line field rate_limits.%s.used_percentage "
            "was outside 0-100" % key
        )

    resets_at = value.get("resets_at")
    if resets_at is not None:
        resets_at = int(
            _number(
                resets_at,
                "rate_limits.%s.resets_at" % key,
            )
        )
    return {
        "used_percentage": used,
        "resets_at": resets_at,
    }


def _rate_limits(payload: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ClaudeStatuslineSnapshotError(
            "Claude status-line input was not an object"
        )
    raw_limits = payload.get("rate_limits")
    if not isinstance(raw_limits, dict):
        raise ClaudeStatuslineSnapshotError(
            "Claude status-line input contained no rate_limits object"
        )

    limits: Dict[str, Dict[str, Any]] = {}
    for key in WINDOW_KEYS:
        if raw_limits.get(key) is None:
            continue
        limits[key] = _normalize_window(key, raw_limits[key])
    if not limits:
        raise ClaudeStatuslineSnapshotError(
            "Claude status-line input contained no usable quota windows"
        )
    return limits


def _utc_datetime(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def capture_statusline_payload(
    payload: Any,
    output_path: Path,
    captured_at: Optional[datetime] = None,
) -> bool:
    """Persist a sanitized snapshot; absent early-session limits are ignored."""

    try:
        limits = _rate_limits(payload)
    except ClaudeStatuslineSnapshotError:
        return False

    current = _utc_datetime(captured_at)
    snapshot = {
        "captured_at": int(current.timestamp()),
        "rate_limits": limits,
    }
    destination = Path(output_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: Optional[Path] = None
    descriptor: Optional[int] = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".%s." % destination.name,
            dir=str(destination.parent),
        )
        temporary_path = Path(raw_path)
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(snapshot, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(destination))
        temporary_path = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return True


def read_statusline_snapshot(
    path: Path,
    now: Optional[datetime] = None,
    stale_after_minutes: int = 30,
) -> ClaudeStatuslineSnapshot:
    source = Path(path).expanduser()
    try:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise ClaudeStatuslineSnapshotError(
            "no snapshot exists at %s" % source
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaudeStatuslineSnapshotError(
            "could not read %s: %s" % (source, exc)
        )

    if not isinstance(payload, dict):
        raise ClaudeStatuslineSnapshotError(
            "snapshot at %s was not an object" % source
        )
    captured_seconds = _number(payload.get("captured_at"), "captured_at")
    try:
        captured_at = datetime.fromtimestamp(
            captured_seconds,
            tz=timezone.utc,
        )
    except (OverflowError, OSError, ValueError):
        raise ClaudeStatuslineSnapshotError(
            "Claude status-line field captured_at was invalid"
        )

    current = _utc_datetime(now)
    raw_age = (current - captured_at).total_seconds()
    if raw_age < -300:
        raise ClaudeStatuslineSnapshotError(
            "snapshot timestamp is more than five minutes in the future"
        )
    age_seconds = max(0, int(raw_age))
    limits = _rate_limits(payload)
    return ClaudeStatuslineSnapshot(
        rate_limits=limits,
        captured_at=captured_at,
        age_seconds=age_seconds,
        stale=age_seconds > max(1, stale_after_minutes) * 60,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture Claude Code rate limits for Quota Glass.",
    )
    parser.add_argument(
        "--output",
        default=os.getenv(
            "CLAUDE_STATUS_SNAPSHOT_PATH",
            DEFAULT_SNAPSHOT_PATH,
        ),
    )
    arguments = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        capture_statusline_payload(payload, Path(arguments.output))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            "Quota Glass could not capture Claude status-line data: %s" % exc,
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
