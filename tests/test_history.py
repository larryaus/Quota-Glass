from app.history import projection_for, window_start
from app.models import Meter


RESETS_AT = 1_800_000_000
WINDOW_MINUTES = 300
# The window this meter is in runs from here to RESETS_AT.
WINDOW_START = RESETS_AT - WINDOW_MINUTES * 60


def meter(pct=50.0, resets_at=RESETS_AT, stale=False, has_quota=True) -> Meter:
    return Meter(
        key="chatgpt.primary",
        provider="chatgpt",
        label="Primary limit",
        used_pct=pct,
        window_minutes=WINDOW_MINUTES,
        resets_at=resets_at,
        has_quota=has_quota,
        source="rollout",
        stale=stale,
    )


def sample(sampled_at, used_pct, stale=0):
    return {
        "sampled_at": sampled_at,
        "meter_key": "chatgpt.primary",
        "provider": "chatgpt",
        "used_pct": used_pct,
        "stale": stale,
    }


def rising(now, span_seconds, start_pct, end_pct, count=4):
    """`count` evenly spaced samples climbing linearly, ending just before now."""
    rows = []
    for index in range(count):
        offset = span_seconds - (span_seconds * index // (count - 1))
        fraction = index / float(count - 1)
        rows.append(
            sample(now - offset, start_pct + (end_pct - start_pct) * fraction)
        )
    return rows


def test_window_start_is_the_reset_minus_one_window():
    assert window_start(meter()) == WINDOW_START


def test_window_start_is_unknown_without_a_reset():
    assert window_start(meter(resets_at=None)) is None


def test_constant_burn_yields_the_expected_rate():
    now = WINDOW_START + 3600
    # 10% over the last 30 minutes is 20%/hr.
    rows = rising(now, 1800, 20.0, 30.0)
    projection = projection_for(rows, meter(30.0), now=now)

    assert projection is not None
    assert projection.burn_rate_pct_per_hour == 20.0
    assert projection.span_seconds == 1800
    # 70% left at 20%/hr is 3.5 hours.
    assert projection.projected_exhaustion_at == now + 12600


def test_flat_usage_reports_no_burn_and_no_projection():
    now = WINDOW_START + 3600
    rows = rising(now, 1800, 40.0, 40.0)
    projection = projection_for(rows, meter(40.0), now=now)

    assert projection is not None
    assert projection.burn_rate_pct_per_hour == 0.0
    assert projection.projected_exhaustion_at is None
    assert projection.exhausts_before_reset is False


def test_stale_samples_are_excluded_from_the_span():
    """A stale row repeats a reading the provider never refreshed.

    Counting it would stretch the span while holding the percentage flat, and
    understate the rate the user is actually burning at.
    """
    now = WINDOW_START + 3600
    rows = [
        sample(now - 3000, 20.0, stale=1),
        sample(now - 2400, 20.0, stale=1),
    ] + rising(now, 1800, 20.0, 30.0)
    projection = projection_for(rows, meter(30.0), now=now)

    assert projection is not None
    assert projection.span_seconds == 1800
    assert projection.burn_rate_pct_per_hour == 20.0


def test_samples_without_a_percentage_are_excluded():
    now = WINDOW_START + 3600
    rows = [sample(now - 3000, None)] + rising(now, 1800, 20.0, 30.0)
    projection = projection_for(rows, meter(30.0), now=now)

    assert projection is not None
    assert projection.span_seconds == 1800


def test_samples_from_before_the_window_are_excluded():
    """The rollover case: a span may never straddle a reset.

    The previous window ended at 95%; the current one is at 10%. Including the
    old samples would produce a negative delta and a nonsense projection.
    """
    now = WINDOW_START + 1800
    rows = [
        sample(WINDOW_START - 7200, 80.0),
        sample(WINDOW_START - 3600, 95.0),
    ] + rising(now, 1200, 4.0, 10.0)
    projection = projection_for(rows, meter(10.0), now=now)

    assert projection is not None
    assert projection.span_seconds == 1200
    assert projection.burn_rate_pct_per_hour == 18.0


def test_too_few_samples_yields_no_projection():
    now = WINDOW_START + 3600
    rows = [sample(now - 1800, 20.0)]
    assert projection_for(rows, meter(30.0), now=now, min_samples=3) is None


def test_span_shorter_than_the_minimum_yields_no_projection():
    now = WINDOW_START + 3600
    rows = rising(now, 300, 20.0, 30.0)
    assert (
        projection_for(rows, meter(30.0), now=now, min_span_seconds=600)
        is None
    )


def test_projection_landing_after_the_reset_is_not_flagged():
    now = WINDOW_START + 3600
    # 2%/hr from 30% needs 35 hours; the window resets in four.
    rows = rising(now, 3600, 28.0, 30.0)
    projection = projection_for(rows, meter(30.0), now=now)

    assert projection is not None
    assert projection.projected_exhaustion_at > RESETS_AT
    assert projection.exhausts_before_reset is False


def test_projection_landing_before_the_reset_is_flagged():
    now = WINDOW_START + 3600
    # 20% over half an hour is 40%/hr, so the remaining 70% is gone in 1h 45m.
    # The window does not reset for another four hours.
    rows = rising(now, 1800, 10.0, 30.0)
    projection = projection_for(rows, meter(30.0), now=now)

    assert projection is not None
    assert projection.burn_rate_pct_per_hour == 40.0
    assert projection.projected_exhaustion_at == now + 6300
    assert projection.projected_exhaustion_at < RESETS_AT
    assert projection.exhausts_before_reset is True


def test_trailing_window_bounds_the_span():
    """Only recent samples count, so a burst hours ago stops projecting."""
    now = WINDOW_START + 10800
    rows = [
        sample(now - 9000, 5.0),
        sample(now - 8400, 60.0),
    ] + rising(now, 1800, 60.0, 61.0)
    projection = projection_for(rows, meter(61.0), now=now, trailing_minutes=60)

    assert projection is not None
    assert projection.span_seconds == 1800
    assert projection.burn_rate_pct_per_hour == 2.0


def test_stale_or_quotaless_meters_get_no_projection():
    now = WINDOW_START + 3600
    rows = rising(now, 1800, 20.0, 30.0)

    assert projection_for(rows, meter(30.0, stale=True), now=now) is None
    assert projection_for(rows, meter(30.0, has_quota=False), now=now) is None
    assert projection_for(rows, meter(None), now=now) is None


def test_full_meter_projects_no_further_exhaustion():
    now = WINDOW_START + 3600
    rows = rising(now, 1800, 90.0, 100.0)
    projection = projection_for(rows, meter(100.0), now=now)

    assert projection is not None
    assert projection.burn_rate_pct_per_hour == 20.0
    assert projection.projected_exhaustion_at is None
    assert projection.exhausts_before_reset is False
