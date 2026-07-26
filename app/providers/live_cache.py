from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple


class LiveSourceCache:
    """Rate-limits calls to a live usage source and retains the last success.

    A previously successful payload stays available while the source is backed
    off, so a transient failure degrades to a stale reading rather than to no
    reading at all.
    """

    def __init__(
        self,
        label: str,
        min_interval_seconds: int = 300,
        max_backoff_seconds: int = 3600,
    ) -> None:
        self.label = label
        self.min_interval_seconds = max(1, min_interval_seconds)
        self.max_backoff_seconds = max(
            self.min_interval_seconds,
            max_backoff_seconds,
        )
        self.payload: Optional[Dict[str, Any]] = None
        self.fetched_at: Optional[datetime] = None
        self.next_retry_at: Optional[datetime] = None
        self.backoff_reason: Optional[str] = None
        self._consecutive_failures = 0

    def age_seconds(self, current: datetime) -> Optional[int]:
        if self.fetched_at is None:
            return None
        return max(0, int((current - self.fetched_at).total_seconds()))

    def should_attempt(self, current: datetime) -> bool:
        if self.next_retry_at is not None and current < self.next_retry_at:
            return False
        age = self.age_seconds(current)
        return age is None or age >= self.min_interval_seconds

    def is_backed_off(self, current: datetime) -> bool:
        return (
            self.next_retry_at is not None
            and current < self.next_retry_at
            and self.backoff_reason is not None
        )

    def record_success(
        self,
        payload: Dict[str, Any],
        current: datetime,
    ) -> None:
        self.payload = payload
        self.fetched_at = current
        self.next_retry_at = None
        self.backoff_reason = None
        self._consecutive_failures = 0

    def _exponential_backoff_seconds(self) -> int:
        delay = self.min_interval_seconds
        for _ in range(max(0, self._consecutive_failures - 1)):
            if delay >= self.max_backoff_seconds:
                return self.max_backoff_seconds
            delay = min(self.max_backoff_seconds, delay * 2)
        return delay

    def _classify_failure(
        self,
        exc: Exception,
        current: datetime,
    ) -> Tuple[int, str]:
        """Return (delay_seconds, reason).

        Owns its own failure counting: a subclass may decide some failures
        should not escalate the backoff at all.
        """
        self._consecutive_failures += 1
        delay = self._exponential_backoff_seconds()
        return delay, "%s request failed: %s" % (self.label, exc)

    def record_failure(
        self,
        exc: Exception,
        current: datetime,
    ) -> None:
        delay, reason = self._classify_failure(exc, current)
        self.backoff_reason = reason
        self.next_retry_at = current + timedelta(seconds=delay)
