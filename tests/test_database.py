from app.database import Database
from app.models import Meter


def meter(pct):
    return Meter(
        key="chatgpt.primary",
        provider="chatgpt",
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
