"""Burn rate and projected exhaustion, derived from stored samples.

The poller writes one `samples` row per meter per poll. This module turns that
series into the only two numbers that answer "will I run out before this window
resets?": a burn rate in percent per hour, and the timestamp the meter is on
track to reach 100%.

The arithmetic is deliberately plain. Within a single quota window `used_pct`
only ever rises, so the delta between the first and last observation is already
an unbiased estimate of the rate over that span -- a least-squares fit would
add machinery without adding accuracy. What actually matters for correctness is
which samples are allowed into the span, and that is what most of this module
is about.
"""

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.models import Meter, MeterProjection


DEFAULT_TRAILING_MINUTES = 60
DEFAULT_MIN_SAMPLES = 3
DEFAULT_MIN_SPAN_SECONDS = 600
FULL_PCT = 100.0


def window_start(meter: Meter) -> Optional[int]:
    """First second of the meter's current quota window, when it is knowable.

    A rollover resets `used_pct` to near zero, so a span that straddles one
    describes two different windows and yields a meaningless -- often negative
    -- rate. Clipping every sample to the current window is what keeps that
    from happening. `resets_at` wobbles by up to a second between reads (see
    `AlertEngine._window_changed`), which moves this boundary by the same
    second and never by enough to matter.
    """
    if meter.resets_at is None or meter.window_minutes is None:
        return None
    return int(meter.resets_at) - int(meter.window_minutes) * 60


def _usable_points(
    rows: Sequence[Dict[str, Any]],
    cutoff: Optional[int],
    now: int,
) -> List[Tuple[int, float]]:
    """`(sampled_at, used_pct)` pairs eligible to form a span, oldest first.

    Stale rows repeat a reading the provider never refreshed; counting them
    would stretch the span while holding the percentage flat and understate
    the rate. Rows without a percentage are the meters that have no quota.
    """
    points: List[Tuple[int, float]] = []
    for row in rows:
        used_pct = row["used_pct"]
        if used_pct is None or row["stale"]:
            continue
        sampled_at = int(row["sampled_at"])
        if cutoff is not None and sampled_at < cutoff:
            continue
        # The caller appends the live reading at `now`; dropping anything at or
        # after it keeps timestamps strictly increasing and avoids a duplicate
        # final point on the poll that just wrote its own sample.
        if sampled_at >= now:
            continue
        points.append((sampled_at, float(used_pct)))
    points.sort(key=lambda point: point[0])
    return points


def projection_for(
    rows: Sequence[Dict[str, Any]],
    meter: Meter,
    now: Optional[int] = None,
    trailing_minutes: int = DEFAULT_TRAILING_MINUTES,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_span_seconds: int = DEFAULT_MIN_SPAN_SECONDS,
) -> Optional[MeterProjection]:
    """Project when `meter` runs out, or None when there is too little data.

    `rows` are `samples` rows for this meter alone. The meter's live reading is
    appended as the final point, because the poller persists samples after
    alerting and the stored series therefore lags one poll behind.
    """
    if not meter.has_quota or meter.stale or meter.used_pct is None:
        return None

    current = int(time.time()) if now is None else now
    trailing_cutoff = current - max(1, trailing_minutes) * 60
    start = window_start(meter)
    # The window bound and the trailing bound are both upper limits on how far
    # back to look; the later of the two wins.
    cutoff = trailing_cutoff if start is None else max(start, trailing_cutoff)

    points = _usable_points(rows, cutoff, current)
    points.append((current, float(meter.used_pct)))
    if len(points) < max(2, min_samples):
        return None

    first_at, first_pct = points[0]
    last_at, last_pct = points[-1]
    span_seconds = last_at - first_at
    if span_seconds < max(1, min_span_seconds):
        return None

    # Usage only rises inside a window, so a negative delta means the series
    # crossed something this module cannot see. Report no burn rather than a
    # projection built on it.
    delta_pct = max(0.0, last_pct - first_pct)
    rate = delta_pct * 3600.0 / span_seconds

    projected_at = None
    if rate > 0 and last_pct < FULL_PCT:
        remaining_pct = FULL_PCT - last_pct
        projected_at = current + int(round(remaining_pct * 3600.0 / rate))

    exhausts_before_reset = (
        projected_at is not None
        and meter.resets_at is not None
        and projected_at < int(meter.resets_at)
    )
    return MeterProjection(
        burn_rate_pct_per_hour=round(rate, 2),
        projected_exhaustion_at=projected_at,
        exhausts_before_reset=exhausts_before_reset,
        sample_count=len(points),
        span_seconds=span_seconds,
    )
