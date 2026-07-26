import sqlite3
from pathlib import Path

import pytest

from app.alerting import AlertEngine
from app.database import Database
from app.models import Meter


def meter(
    pct=10.0,
    resets_at=1_800_000_000,
    stale=False,
    has_quota=True,
) -> Meter:
    return Meter(
        key="chatgpt.primary",
        provider="chatgpt",
        label="Primary limit",
        used_pct=pct,
        window_minutes=300,
        resets_at=resets_at,
        has_quota=has_quota,
        source="rollout",
        stale=stale,
    )


def engine(tmp_path: Path, notifier):
    database = Database(tmp_path / "usage.db")
    return database, AlertEngine(database, notifier, 100.0, 5.0)


def test_first_observation_seeds_without_firing(tmp_path, recording_notifier):
    database, alerts = engine(tmp_path, recording_notifier)
    assert alerts.process(meter(100), now=100) == []
    assert database.get_events() == []
    assert recording_notifier.calls == []


def test_crossing_to_100_fires_once_and_staying_full_does_not(
    tmp_path, recording_notifier
):
    database, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(99), now=100)
    assert alerts.process(meter(100), now=101) == ["EXHAUSTED"]
    assert alerts.process(meter(100), now=102) == []
    assert [row["event_type"] for row in database.get_events()] == ["EXHAUSTED"]
    assert len(recording_notifier.calls) == 1


def test_window_rollover_fires_refreshed_exactly_once(tmp_path, recording_notifier):
    database, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(99, 2000), now=100)
    alerts.process(meter(100, 2000), now=101)
    assert alerts.process(meter(100, 3000), now=102) == [
        "REFRESHED",
        "EXHAUSTED",
    ]
    assert alerts.process(meter(100, 3000), now=103) == []
    assert [row["event_type"] for row in database.get_events()] == [
        "EXHAUSTED",
        "REFRESHED",
        "EXHAUSTED",
    ]
    assert [call[0] for call in recording_notifier.calls] == [
        "Quota exhausted",
        "Quota refreshed",
        "Quota exhausted",
    ]


def test_low_water_refreshes_even_without_window_change(tmp_path, recording_notifier):
    _, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(90), now=100)
    alerts.process(meter(100), now=101)
    assert alerts.process(meter(4), now=102) == ["REFRESHED"]
    assert alerts.process(meter(4), now=103) == []


def test_restart_rehydrates_and_does_not_double_fire(tmp_path, recording_notifier):
    database, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(90), now=100)
    alerts.process(meter(100), now=101)
    database.close()

    restarted_db = Database(tmp_path / "usage.db")
    restarted = AlertEngine(restarted_db, recording_notifier, 100.0, 5.0)
    assert restarted.process(meter(100), now=102) == []
    assert len(restarted_db.get_events()) == 1
    assert len(recording_notifier.calls) == 1


def test_stale_and_none_never_fire_or_replace_last_known_state(
    tmp_path, recording_notifier
):
    database, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(90), now=100)
    assert alerts.process(meter(100, stale=True), now=101) == []
    assert alerts.process(meter(None), now=102) == []
    assert alerts.process(meter(100), now=103) == ["EXHAUSTED"]
    assert len(database.get_events()) == 1


def test_has_quota_false_never_alerts(tmp_path, recording_notifier):
    database, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(90, has_quota=False), now=100)
    assert alerts.process(meter(100, has_quota=False), now=101) == []
    assert database.get_meter_state("chatgpt.primary") is None
    assert recording_notifier.calls == []


def test_event_and_latch_update_roll_back_together(tmp_path, recording_notifier):
    database, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(90), now=100)
    database._connection.execute(
        """
        CREATE TRIGGER reject_alert_event
        BEFORE INSERT ON events
        BEGIN
            SELECT RAISE(ABORT, 'simulated event write failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        alerts.process(meter(100), now=101)

    state = database.get_meter_state("chatgpt.primary")
    assert state["last_pct"] == 90
    assert state["fired_full_for_window"] is None
    assert database.get_events() == []


def test_backward_window_identity_change_is_a_reset(tmp_path, recording_notifier):
    _, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(90, 3000), now=100)
    alerts.process(meter(100, 3000), now=101)

    assert alerts.process(meter(40, 2000), now=102) == ["REFRESHED"]


@pytest.mark.parametrize("resets_at", [None, 3000])
def test_usage_drop_reseeds_when_reset_identity_is_missing_or_unchanged(
    tmp_path,
    recording_notifier,
    resets_at,
):
    _, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(90, resets_at), now=100)
    alerts.process(meter(100, resets_at), now=101)

    assert alerts.process(meter(6, resets_at), now=102) == ["REFRESHED"]
    assert alerts.process(meter(100, resets_at), now=103) == ["EXHAUSTED"]


def test_meter_reappearing_after_two_missed_polls_is_reseeded(
    tmp_path,
    recording_notifier,
):
    database, alerts = engine(tmp_path, recording_notifier)
    alerts.process(meter(90), now=100)
    alerts.process(meter(100), now=101)

    alerts.record_poll_presence([], ["chatgpt"])
    alerts.record_poll_presence([], ["chatgpt"])
    missing_state = database.get_meter_state("chatgpt.primary")
    assert missing_state["fired_full_for_window"] is None
    assert missing_state["reseed_required"] == 1

    assert alerts.process(meter(100), now=104) == []
    reseeded = database.get_meter_state("chatgpt.primary")
    assert reseeded["last_pct"] == 100
    assert reseeded["reseed_required"] == 0


class FailingNotifier:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def notify(self, title, subtitle, message):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("notification center unavailable")


def test_failed_notification_retries_without_duplicate_event(tmp_path):
    notifier = FailingNotifier(failures=2)
    database = Database(tmp_path / "usage.db")
    alerts = AlertEngine(
        database,
        notifier,
        100.0,
        5.0,
        max_notification_attempts=3,
    )
    alerts.process(meter(90), now=100)

    assert alerts.process(meter(100), now=101) == ["EXHAUSTED"]
    assert alerts.process(meter(100), now=102) == []
    assert alerts.process(meter(100), now=103) == []

    events = database.get_events()
    assert len(events) == 1
    assert events[0]["notification_status"] == "delivered"
    assert events[0]["notification_attempts"] == 3
    assert notifier.calls == 3


def test_notification_retry_cap_stops_further_attempts(tmp_path):
    notifier = FailingNotifier(failures=10)
    database = Database(tmp_path / "usage.db")
    alerts = AlertEngine(
        database,
        notifier,
        100.0,
        5.0,
        max_notification_attempts=2,
    )
    alerts.process(meter(90), now=100)
    alerts.process(meter(100), now=101)
    alerts.process(meter(100), now=102)
    alerts.process(meter(100), now=103)

    event = database.get_events()[0]
    assert event["notification_status"] == "failed"
    assert event["notification_attempts"] == 2
    assert "notification center unavailable" in event["notification_error"]
    assert notifier.calls == 2
