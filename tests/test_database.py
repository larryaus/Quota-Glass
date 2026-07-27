import sqlite3
import time

from app.database import Database
from app.models import Meter


def meter(pct, key="chatgpt.primary"):
    return Meter(
        key=key,
        provider=key.split(".", 1)[0],
        label="Primary limit",
        used_pct=pct,
        window_minutes=300,
        resets_at=1_800_000_000,
        has_quota=True,
        source="rollout",
        stale=False,
    )


def test_retention_prunes_old_samples_and_events(tmp_path):
    database = Database(tmp_path / "usage.db")
    now = 2_000_000_000
    old = now - 31 * 24 * 3600
    recent = now - 29 * 24 * 3600
    database.add_samples([meter(10)], sampled_at=old)
    database.add_samples([meter(20)], sampled_at=recent)
    database.record_events_and_state(
        ["EXHAUSTED"],
        meter(100),
        meter(100).resets_at,
        old,
        meter(100).resets_at,
        old,
    )
    database.record_events_and_state(
        ["REFRESHED"],
        meter(0),
        meter(0).resets_at,
        recent,
        None,
        recent,
    )

    assert database.prune_history(30, now=now) == (1, 1)
    sample_count = database._connection.execute(
        "SELECT COUNT(*) FROM samples"
    ).fetchone()[0]
    event_count = database._connection.execute(
        "SELECT COUNT(*) FROM events"
    ).fetchone()[0]
    assert sample_count == 1
    assert event_count == 1


def test_projection_latch_round_trips(tmp_path):
    database = Database(tmp_path / "usage.db")
    database.put_meter_state(
        "chatgpt.primary",
        42.0,
        1_800_000_000,
        None,
        None,
        fired_projection_for_window=1_800_000_000,
    )

    state = database.get_meter_state("chatgpt.primary")
    assert state["fired_projection_for_window"] == 1_800_000_000
    assert state["fired_full_for_window"] is None


def test_reseed_clears_the_projection_latch(tmp_path):
    """A meter that vanished must not carry a stale projection latch back.

    Without this the latch would suppress the next genuine warning for a window
    the engine has no baseline for.
    """
    database = Database(tmp_path / "usage.db")
    database.put_meter_state(
        "chatgpt.primary",
        42.0,
        1_800_000_000,
        1_800_000_000,
        None,
        fired_projection_for_window=1_800_000_000,
    )

    # One missed poll is tolerated; the second forces a reseed.
    database.mark_meter_presence([], ["chatgpt"])
    assert (
        database.get_meter_state("chatgpt.primary")[
            "fired_projection_for_window"
        ]
        == 1_800_000_000
    )

    database.mark_meter_presence([], ["chatgpt"])
    state = database.get_meter_state("chatgpt.primary")
    assert state["reseed_required"] == 1
    assert state["fired_projection_for_window"] is None
    assert state["fired_full_for_window"] is None


def test_get_recent_samples_filters_by_meter_and_time(tmp_path):
    database = Database(tmp_path / "usage.db")
    database.add_samples([meter(10), meter(70, "claude.primary")], sampled_at=100)
    database.add_samples([meter(20), meter(80, "claude.primary")], sampled_at=200)
    database.add_samples([meter(30)], sampled_at=300)

    rows = database.get_recent_samples("chatgpt.primary", 200)
    assert [(row["sampled_at"], row["used_pct"]) for row in rows] == [
        (200, 20.0),
        (300, 30.0),
    ]


def test_get_history_filters_by_meter_key(tmp_path):
    database = Database(tmp_path / "usage.db")
    now = int(time.time())
    database.add_samples(
        [meter(10), meter(70, "claude.primary")],
        sampled_at=now - 60,
    )

    rows = database.get_history(1, meter_key="claude.primary")
    assert [row["meter_key"] for row in rows] == ["claude.primary"]


def test_get_history_buckets_collapse_rows_and_keep_the_peak(tmp_path):
    """Bucketing must take the max, not the average.

    Usage only rises within a window, so the peak inside a bucket is the real
    reading; averaging would drag the line below the series it summarises.
    """
    database = Database(tmp_path / "usage.db")
    # Anchor to a bucket boundary so the grouping does not depend on the
    # wall-clock second the test happens to run at.
    base = ((int(time.time()) - 3000) // 300) * 300
    for offset, pct in ((0, 10.0), (60, 14.0), (120, 18.0), (300, 25.0)):
        database.add_samples([meter(pct)], sampled_at=base + offset)

    rows = database.get_history(1, bucket_seconds=300)
    assert [(row["sampled_at"], row["used_pct"]) for row in rows] == [
        (base, 18.0),
        (base + 300, 25.0),
    ]


def test_schema_migrates_a_database_without_the_new_columns(tmp_path):
    """A database written by an earlier build must open and keep its rows."""
    path = tmp_path / "usage.db"
    legacy = sqlite3.connect(str(path))
    legacy.executescript(
        """
        CREATE TABLE meter_state (
            key TEXT PRIMARY KEY,
            last_pct REAL,
            window_id INTEGER,
            fired_full_for_window INTEGER,
            last_event_at INTEGER
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            meter_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            label TEXT NOT NULL,
            used_pct REAL,
            window_id INTEGER,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sampled_at INTEGER NOT NULL,
            meter_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            used_pct REAL,
            stale INTEGER NOT NULL
        );
        INSERT INTO meter_state VALUES ('chatgpt.primary', 55.0, 1800000000, 1800000000, 10);
        INSERT INTO events
            (event_type, meter_key, provider, label, used_pct, window_id, created_at)
        VALUES ('EXHAUSTED', 'chatgpt.primary', 'chatgpt', 'Primary limit', 100.0, 1800000000, 10);
        """
    )
    legacy.commit()
    legacy.close()

    database = Database(path)

    state = database.get_meter_state("chatgpt.primary")
    assert state["last_pct"] == 55.0
    assert state["fired_full_for_window"] == 1_800_000_000
    assert state["fired_projection_for_window"] is None
    assert state["reseed_required"] == 0

    events = database.get_events()
    assert len(events) == 1
    assert events[0]["burn_rate_pct_per_hour"] is None
    assert events[0]["notification_status"] == "delivered"


def test_notification_health_is_clean_when_everything_delivered(tmp_path):
    database = Database(tmp_path / "usage.db")
    database.record_events_and_state(
        ["EXHAUSTED"], meter(100), 1_800_000_000, 10, 1_800_000_000, 10
    )
    for event in database.get_pending_notifications("chatgpt.primary", 3):
        database.mark_notification_delivered(int(event["id"]))

    health = database.get_notification_health()
    assert health["pending"] == 0
    assert health["failed"] == 0
    assert health["abandoned"] == 0
    assert health["last_error"] is None


def test_notification_health_counts_failures(tmp_path):
    database = Database(tmp_path / "usage.db")
    database.record_events_and_state(
        ["EXHAUSTED"], meter(100), 1_800_000_000, 10, 1_800_000_000, 10
    )
    event_id = int(database.get_pending_notifications("chatgpt.primary", 2)[0]["id"])
    database.mark_notification_failed(event_id, "smtp: connection refused", 2)
    database.mark_notification_failed(event_id, "smtp: connection refused", 2)

    health = database.get_notification_health()
    assert health["failed"] == 1
    assert health["pending"] == 0
    assert "connection refused" in health["last_error"]
    assert health["last_failure_meter"] == "chatgpt.primary"
    assert health["last_failure_at"] == 10


def test_abandoned_events_are_counted_but_are_not_an_error(tmp_path):
    """A vanished meter is a provider condition, not a delivery failure.

    Treating it as an error would leave the dashboard degraded for the whole
    retention window every time a meter legitimately goes away.
    """
    database = Database(tmp_path / "usage.db")
    database.record_events_and_state(
        ["EXHAUSTED"], meter(100), 1_800_000_000, 10, 1_800_000_000, 10
    )
    database.mark_meter_presence([], ["chatgpt"])
    database.mark_meter_presence([], ["chatgpt"])

    health = database.get_notification_health()
    assert health["abandoned"] == 1
    assert health["last_error"] is None
    assert health["last_failure_meter"] is None
