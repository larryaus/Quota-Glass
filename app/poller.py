import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from app.alerting import AlertEngine
from app.database import Database
from app.models import DashboardState, Meter, PollerHealth, ProviderState
from app.providers.chatgpt import parse_chatgpt
from app.providers.claude import ClaudeOAuthCache, parse_claude
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

    async def refresh(self) -> DashboardState:
        await asyncio.to_thread(self._refresh_lock.acquire)
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

                notification_error = await asyncio.to_thread(
                    self.database.get_latest_notification_error
                )
                if notification_error is not None:
                    errors.append(notification_error)
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
