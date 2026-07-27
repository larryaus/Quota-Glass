import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from app.alerting import AlertEngine
from app.database import Database
from app.models import EventsResponse, HistoryResponse
from app.notifier import (
    CompositeNotifier,
    MacOSNotifier,
    Notifier,
    NullNotifier,
    SmtpEmailNotifier,
)
from app.poller import UsagePoller
from app.settings import Settings


def configured_notifier(settings: Settings) -> Notifier:
    notifiers: List[Notifier] = []
    if settings.notifications_enabled:
        notifiers.append(MacOSNotifier())
    if settings.email_notifications_enabled:
        notifiers.append(
            SmtpEmailNotifier(
                host=settings.smtp_host,
                port=settings.smtp_port,
                sender=settings.email_from,
                recipients=settings.email_to,
                username=settings.smtp_username,
                password=settings.smtp_password,
                security=settings.smtp_security,
                timeout_seconds=settings.smtp_timeout_seconds,
            )
        )
    if not notifiers:
        return NullNotifier()
    if len(notifiers) == 1:
        return notifiers[0]
    return CompositeNotifier(notifiers)


def create_app(
    settings: Optional[Settings] = None,
    notifier: Optional[Notifier] = None,
) -> FastAPI:
    configured = settings or Settings()
    database = Database(configured.database_path)
    selected_notifier = notifier
    if selected_notifier is None:
        selected_notifier = configured_notifier(configured)
    alert_engine = AlertEngine(
        database,
        selected_notifier,
        configured.alert_threshold_pct,
        configured.alert_reset_pct,
        max_notification_attempts=configured.max_notification_attempts,
        projection_alert_enabled=(
            configured.enable_burn_rate and configured.projection_alert_enabled
        ),
        projection_alert_margin_seconds=(
            configured.projection_alert_margin_seconds
        ),
    )
    poller = UsagePoller(configured, database, alert_engine)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(poller.run())
        application.state.poller_task = task
        try:
            yield
        finally:
            poller.stop()
            await task
            database.close()

    api = FastAPI(
        title="Quota Glass",
        version="1.0.0",
        lifespan=lifespan,
    )
    api.state.settings = configured
    api.state.database = database
    api.state.poller = poller
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @api.get("/api/health")
    async def health(request: Request) -> dict:
        state = request.app.state.poller.state()
        current = state.poller
        return {
            "status": current.status,
            "running": current.running,
            "background_task_alive": current.background_task_alive,
            "poll_count": current.poll_count,
            "last_poll_started": current.last_poll_started,
            "last_poll_completed": current.last_poll_completed,
            "last_poll_completed_age_seconds": (
                current.last_poll_completed_age_seconds
            ),
            "last_poll_duration_ms": current.last_poll_duration_ms,
            "last_error": current.last_error,
            "notifications": state.notifications.model_dump(),
        }

    @api.get("/api/state")
    async def state(request: Request) -> dict:
        return request.app.state.poller.state().model_dump()

    @api.get("/api/history", response_model=HistoryResponse)
    async def history(
        request: Request,
        hours: int = Query(default=24, ge=1, le=720),
        meter_key: Optional[str] = Query(default=None),
        bucket_seconds: int = Query(default=0, ge=0, le=86400),
    ) -> HistoryResponse:
        return HistoryResponse(
            hours=hours,
            bucket_seconds=bucket_seconds,
            samples=request.app.state.database.get_history(
                hours,
                meter_key=meter_key,
                bucket_seconds=bucket_seconds or None,
            ),
        )

    @api.get("/api/events", response_model=EventsResponse)
    async def events(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> EventsResponse:
        return EventsResponse(
            events=request.app.state.database.get_events(limit),
        )

    @api.post("/api/refresh")
    async def refresh(request: Request) -> dict:
        return (await request.app.state.poller.refresh()).model_dump()

    return api


app = create_app()
