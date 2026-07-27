import time
from typing import Any, Dict, List, Optional

from app.database import Database
from app.models import Meter
from app.notifier import Notifier


RESET_DROP_EPSILON_PCT = 10.0
DEFAULT_MAX_NOTIFICATION_ATTEMPTS = 3
# How far ahead of the reset a projection must land before it is worth an
# alert. Without a margin the alert flaps: a projection that hovers either side
# of the reset would fire, latch, clear on the next rollover, and fire again.
DEFAULT_PROJECTION_ALERT_MARGIN_SECONDS = 900
# Reset timestamps are not stable to the second. Claude reports resets_at with
# microsecond precision and it is truncated to a whole second, so two reads of
# one window can straddle a second boundary and differ by 1. A genuine rollover
# moves the reset by a whole window -- five hours at the shortest -- so a
# difference this small is jitter, never a new window.
WINDOW_JITTER_TOLERANCE_SECONDS = 120


class AlertEngine:
    def __init__(
        self,
        database: Database,
        notifier: Notifier,
        threshold_pct: float = 100.0,
        reset_pct: float = 5.0,
        reset_drop_epsilon_pct: float = RESET_DROP_EPSILON_PCT,
        max_notification_attempts: int = DEFAULT_MAX_NOTIFICATION_ATTEMPTS,
        window_jitter_tolerance_seconds: int = WINDOW_JITTER_TOLERANCE_SECONDS,
        projection_alert_enabled: bool = True,
        projection_alert_margin_seconds: int = (
            DEFAULT_PROJECTION_ALERT_MARGIN_SECONDS
        ),
    ) -> None:
        self.database = database
        self.notifier = notifier
        self.threshold_pct = threshold_pct
        self.reset_pct = reset_pct
        self.reset_drop_epsilon_pct = reset_drop_epsilon_pct
        self.max_notification_attempts = max(1, max_notification_attempts)
        self.window_jitter_tolerance_seconds = max(
            0,
            window_jitter_tolerance_seconds,
        )
        self.projection_alert_enabled = projection_alert_enabled
        self.projection_alert_margin_seconds = max(
            0,
            projection_alert_margin_seconds,
        )
        self.last_errors: List[str] = []

    def _window_changed(
        self,
        previous: Optional[int],
        current: Optional[int],
    ) -> bool:
        """True only when the reset moved further than jitter can explain."""
        if previous is None or current is None:
            return (previous is None) != (current is None)
        return (
            abs(int(previous) - int(current))
            > self.window_jitter_tolerance_seconds
        )

    def _projection_alert_due(self, meter: Meter) -> bool:
        """True when this meter is on track to run out before it resets."""
        if not self.projection_alert_enabled:
            return False
        projection = meter.projection
        if projection is None or not projection.exhausts_before_reset:
            return False
        # A meter already at the threshold belongs to EXHAUSTED. Warning that
        # it is about to run out would be both late and duplicative.
        if meter.used_pct is None or meter.used_pct >= self.threshold_pct:
            return False
        projected_at = projection.projected_exhaustion_at
        if projected_at is None or meter.resets_at is None:
            return False
        return (
            int(meter.resets_at) - int(projected_at)
            > self.projection_alert_margin_seconds
        )

    def process(self, meter: Meter, now: Optional[int] = None) -> List[str]:
        self.last_errors = []
        if not meter.has_quota or meter.stale or meter.used_pct is None:
            self._deliver_pending(meter.key)
            return []

        event_time = int(time.time()) if now is None else now
        current_window = meter.resets_at
        # SQLite NULL means "not latched", so 0 is the stable identity for a
        # quota meter that does not publish a reset timestamp.
        latch_window = current_window if current_window is not None else 0
        state = self.database.get_meter_state(meter.key)
        if state is None or bool(state["reseed_required"]):
            self.database.put_meter_state(
                meter.key, meter.used_pct, current_window, None, None
            )
            self._deliver_pending(meter.key)
            return []

        previous_pct = state["last_pct"]
        fired_window = state["fired_full_for_window"]
        fired_projection = state.get("fired_projection_for_window")
        last_event_at = state["last_event_at"]
        events: List[str] = []

        window_changed = self._window_changed(state["window_id"], current_window)
        usage_drop = (
            previous_pct is not None
            and float(previous_pct) - meter.used_pct
            > self.reset_drop_epsilon_pct
        )
        low_water = (
            fired_window is not None and meter.used_pct < self.reset_pct
        )
        rolled_over = window_changed or usage_drop or low_water
        baseline = previous_pct
        if rolled_over:
            if fired_window is not None:
                events.append("REFRESHED")
            fired_window = None
            # Both latches belong to the window that just ended.
            fired_projection = None
            # A reset starts a new series at zero even if the first observation
            # arrives after the new window has already filled.
            baseline = 0.0

        crossed = (
            baseline is not None
            and float(baseline) < self.threshold_pct
            and meter.used_pct >= self.threshold_pct
            and fired_window != latch_window
        )
        if crossed:
            events.append("EXHAUSTED")
            fired_window = latch_window

        if self._projection_alert_due(meter) and fired_projection != latch_window:
            events.append("PROJECTED_EXHAUSTION")
            fired_projection = latch_window

        if events:
            last_event_at = event_time

        self.database.record_events_and_state(
            events,
            meter,
            current_window,
            event_time,
            fired_window,
            last_event_at,
            fired_projection,
        )
        # Pending rows and the detection latch are durable before any external
        # notification attempt. A crash here retries the same row after restart.
        self._deliver_pending(meter.key)
        return events

    def record_poll_presence(
        self,
        seen_keys: List[str],
        polled_providers: List[str],
    ) -> None:
        self.database.mark_meter_presence(seen_keys, polled_providers)

    def _deliver_pending(self, meter_key: str) -> None:
        pending = self.database.get_pending_notifications(
            meter_key,
            self.max_notification_attempts,
        )
        for event in pending:
            try:
                self._notify_event(event)
            except Exception as exc:
                error = "Notification delivery failed for %s: %s" % (
                    event["meter_key"],
                    exc,
                )
                self.database.mark_notification_failed(
                    int(event["id"]),
                    error,
                    self.max_notification_attempts,
                )
                self.last_errors.append(error)
            else:
                self.database.mark_notification_delivered(int(event["id"]))

    def _duration_label(self, seconds: int) -> str:
        """`2h 10m` / `45m`, for how much of a window a projection eats into."""
        minutes = max(0, int(seconds)) // 60
        hours, minutes = divmod(minutes, 60)
        if hours and minutes:
            return "%dh %dm" % (hours, minutes)
        if hours:
            return "%dh" % hours
        return "%dm" % minutes

    def _projection_message(self, event: Dict[str, Any], provider: str) -> str:
        rate = event.get("burn_rate_pct_per_hour")
        projected_at = event.get("projected_exhaustion_at")
        window_id = event.get("window_id")
        used_pct = event["used_pct"]
        detail = "%s is at %.0f%%" % (provider, float(used_pct))
        if rate is not None:
            detail += " and climbing %.1f%%/hr" % float(rate)
        if projected_at is not None and window_id:
            early = int(window_id) - int(projected_at)
            return "%s -- on track to run out %s before this window resets." % (
                detail,
                self._duration_label(early),
            )
        return "%s -- on track to run out before this window resets." % detail

    def _notify_event(self, event: Dict[str, Any]) -> None:
        event_type = str(event["event_type"])
        provider_key = str(event["provider"])
        provider = {
            "chatgpt": "ChatGPT",
            "claude": "Claude",
        }.get(provider_key.lower(), provider_key.title())
        label = str(event["label"])
        if event_type == "PROJECTED_EXHAUSTION":
            self.notifier.notify(
                "Quota running out",
                label,
                self._projection_message(event, provider),
            )
            return
        if event_type == "REFRESHED":
            used_pct = event["used_pct"]
            message = "%s usage is available again." % provider
            if used_pct is not None and float(used_pct) == 0.0:
                message = (
                    "%s usage reset to 0%% and is available again."
                    % provider
                )
            self.notifier.notify(
                "Quota refreshed",
                label,
                message,
            )
            return
        used_pct = event["used_pct"]
        self.notifier.notify(
            "Quota exhausted",
            label,
            "%s reached %.0f%% usage." % (provider, float(used_pct)),
        )
