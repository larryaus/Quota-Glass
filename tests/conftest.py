import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, str]] = []

    def notify(self, title: str, subtitle: str, message: str) -> None:
        self.calls.append((title, subtitle, message))


@pytest.fixture
def recording_notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def claude_oauth_payload(fixture_dir: Path) -> Dict[str, Any]:
    with (fixture_dir / "claude" / "oauth-usage.json").open(
        "r",
        encoding="utf-8",
    ) as handle:
        return json.load(handle)
