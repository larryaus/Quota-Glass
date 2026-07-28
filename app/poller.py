import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from app.alerting import AlertEngine
from app.database import Database
from app.history import projection_for, window_start
from app.models import (
    DashboardState,
    Meter,
    NotificationHealth,
    PollerHealth,
    ProviderState,
)
from app.providers.chatgpt import parse_chatgpt
from app.providers.claude import ClaudeOAuthCache, parse_claude
from app.providers.live_cache import LiveSourceCache
from app.settings import Settings


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class UsagePoller:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        alert_engine: AlertEngine,
    ) -> None:
        self.settings = settings
        self.database = database
        self.alert_engine = alert_engine
        self.providers: List[ProviderState] = []
        self.health = PollerHealth()
        self.notifications = NotificationHealth()
        # These signals may be touched by an ASGI lifespan task and an
        # immediate manual-refresh request. Threading primitives avoid binding
        # the poller to whichever event loop happens to touch them first on
        # Python 3.9.
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._last_poll_completed_monotonic: Optional[float] = None
        self._last_prune_monotonic: Optional[float] = None
        self._claude_oauth_cache = ClaudeOAuthCache(
            settings.oauth_min_interval_seconds,
            settings.oauth_max_backoff_seconds,
        )
        self._chatgpt_live_cache = LiveSourceCache(
            "Codex CLI",
            settings.chatgpt_live_min_interval_seconds,
            settings.oauth_max_backoff_seconds,
        )

    async def refresh(self) -> DashboardState:
        # Never queue for the lock. Waiting would park a thread-pool worker,
        # and the holder needs workers of its own (parse_chatgpt, alerting,
        # sample persistence); enough waiters and it can never finish, never
        # release, and the poller never recovers. A caller that arrives
        # mid-poll gets the current state and the in-flight result lands
        # moments later; the background loop retries on its next interval.
        if not self._refresh_lock.acquire(blocking=False):
            return self.state()
        try:
            started = time.monotonic()
            self.health.running = True
            self.health.status = "polling"
            self.health.last_poll_started = _iso_now()
            self.health.last_error = None
            errors: List[str] = []
            polled_providers: List[str] = []
            try:
                try:
                    chatgpt = await asyncio.to_thread(
                        parse_chatgpt,
                        self.settings.codex_sessions_dir,
                        self.settings.stale_after_minutes,
                        candidate_file_count=self.settings.chatgpt_candidate_files,
                        enable_live=self.settings.enable_chatgpt_live,
                        live_cache=self._chatgpt_live_cache,
                        cli_path=self.settings.codex_cli_path,
                        cli_timeout_seconds=self.settings.codex_cli_timeout_seconds,
                    )
                    polled_providers.append("chatgpt")
                except Exception as exc:
                    error = "ChatGPT polling failed: %s" % exc
                    errors.append(error)
                    chatgpt = ProviderState(
                        key="chatgpt",
                        label="ChatGPT",
                        mode="local",
                        error=error,
                    )
                try:
                    claude = await parse_claude(
                        self.settings.claude_projects_dir,
                        self.settings.enable_claude_oauth,
                        oauth_cache=self._claude_oauth_cache,
                        enable_statusline=(
                            self.settings.enable_claude_statusline
                        ),
                        statusline_snapshot_path=(
                            self.settings.claude_status_snapshot_path
                        ),
                        statusline_stale_after_minutes=(
                            self.settings.claude_status_stale_after_minutes
                        ),
                    )
                    polled_providers.append("claude")
                except Exception as exc:
                    error = "Claude polling failed: %s" % exc
                    errors.append(error)
                    claude = ProviderState(
                        key="claude",
                        label="Claude",
                        mode="local",
                        error=error,
                    )
                self.providers = [chatgpt, claude]
                meters: List[Meter] = [
                    meter for provider in self.providers for meter in provider.meters
                ]
                if self.settings.enable_burn_rate:
                    try:
                        await asyncio.to_thread(self._attach_projections, meters)
                    except Exception as exc:
                        errors.append("Burn rate calculation failed: %s" % exc)
                for meter in meters:
                    try:
                        await asyncio.to_thread(
                            self.alert_engine.process,
                            meter,
                        )
                        errors.extend(self.alert_engine.last_errors)
                    except Exception as exc:
                        errors.append(
                            "Alert processing failed for %s: %s"
                            % (meter.key, exc)
                        )
                seen_keys = [
                    meter.key
                    for meter in meters
                    if meter.has_quota and meter.used_pct is not None
                ]
                try:
                    await asyncio.to_thread(
                        self.alert_engine.record_poll_presence,
                        seen_keys,
                        polled_providers,
                    )
                except Exception as exc:
                    errors.append("Meter presence update failed: %s" % exc)
                try:
                    await asyncio.to_thread(self.database.add_samples, meters)
                except Exception as exc:
                    errors.append("Sample persistence failed: %s" % exc)

                prune_due = (
                    self._last_prune_monotonic is None
                    or time.monotonic() - self._last_prune_monotonic >= 3600
                )
                if prune_due:
                    try:
                        await asyncio.to_thread(
                            self.database.prune_history,
                            self.settings.history_retention_days,
                        )
                        self._last_prune_monotonic = time.monotonic()
                    except Exception as exc:
                        errors.append("History retention failed: %s" % exc)

                # Collected here, once per poll, so `state()` stays a pure
                # in-memory read -- it is called on every API request and by
                # the non-blocking refresh path.
                try:
                    self.notifications = NotificationHealth(
                        **await asyncio.to_thread(
                            self.database.get_notification_health
                        )
                    )
                except Exception as exc:
                    errors.append("Notification health lookup failed: %s" % exc)
                if self.notifications.last_error is not None:
                    errors.append(self.notifications.last_error)
                self.health.status = "degraded" if errors else "healthy"
                self.health.last_error = "; ".join(errors) if errors else None
            except Exception as exc:
                self.health.status = "degraded"
                self.health.last_error = str(exc)
            finally:
                self.health.running = False
                self.health.poll_count += 1
                self.health.last_poll_completed = _iso_now()
                self.health.last_poll_completed_age_seconds = 0
                self._last_poll_completed_monotonic = time.monotonic()
                self.health.last_poll_duration_ms = int(
                    (time.monotonic() - started) * 1000
                )
            return self.state()
        finally:
            self._refresh_lock.release()

    def _attach_projections(self, meters: List[Meter]) -> None:
        """Fill in `meter.projection` from each meter's stored samples.

        Runs before alerting, because the alert engine reads the projection.
        Samples are persisted later in the same poll, so the stored series ends
        one poll behind; `projection_for` appends the live reading to close
        that gap.
        """
        now = int(time.time())
        since = now - self.settings.burn_rate_window_minutes * 60
        for meter in meters:
            if not meter.has_quota or meter.stale or meter.used_pct is None:
                continue
            start = window_start(meter)
            rows = self.database.get_recent_samples(
                meter.key,
                since if start is None else max(start, since),
            )
            meter.projection = projection_for(
                rows,
                meter,
                now,
                trailing_minutes=self.settings.burn_rate_window_minutes,
                min_samples=self.settings.burn_rate_min_samples,
                min_span_seconds=self.settings.burn_rate_min_span_seconds,
            )

    def state(self) -> DashboardState:
        if self._last_poll_completed_monotonic is not None:
            self.health.last_poll_completed_age_seconds = max(
                0,
                int(
                    time.monotonic()
                    - self._last_poll_completed_monotonic
                ),
            )
        return DashboardState(
            providers=self.providers,
            poller=self.health,
            notifications=self.notifications,
            generated_at=_iso_now(),
        )

    async def run(self) -> None:
        self.health.background_task_alive = True
        try:
            await self.refresh()
            while not self._stop_event.is_set():
                stopped = await asyncio.to_thread(
                    self._stop_event.wait,
                    self.settings.poll_interval_seconds,
                )
                if not stopped:
                    await self.refresh()
        finally:
            self.health.background_task_alive = False

    def stop(self) -> None:
        self._stop_event.set()
