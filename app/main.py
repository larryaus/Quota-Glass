import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from app.alerting import AlertEngine
from app.database import Database
from app.notifier import MacOSNotifier, Notifier, NullNotifier
from app.poller import UsagePoller
from app.settings import Settings


def create_app(
    settings: Optional[Settings] = None,
    notifier: Optional[Notifier] = None,
) -> FastAPI:
    configured = settings or Settings()
    database = Database(configured.database_path)
    selected_notifier = notifier
    if selected_notifier is None:
        selected_notifier = (
            MacOSNotifier() if configured.notifications_enabled else NullNotifier()
        )
    alert_engine = AlertEngine(
        database,
        selected_notifier,
        configured.alert_threshold_pct,
        configured.alert_reset_pct,
        max_notification_attempts=configured.max_notification_attempts,
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
        current = request.app.state.poller.state().poller
        return {
            "status": current.status,
            "running": current.running,
            "background_task_alive": current.background_task_alive,
            "poll_count": current.poll_count,
            "last_poll_completed": current.last_poll_completed,
            "last_poll_completed_age_seconds": (
                current.last_poll_completed_age_seconds
            ),
            "last_error": current.last_error,
        }

    @api.get("/api/state")
    async def state(request: Request) -> dict:
        return request.app.state.poller.state().model_dump()

    @api.get("/api/history")
    async def history(
        request: Request,
        hours: int = Query(default=24, ge=1, le=720),
    ) -> dict:
        return {
            "hours": hours,
            "samples": request.app.state.database.get_history(hours),
        }

    @api.get("/api/events")
    async def events(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict:
        return {
            "events": request.app.state.database.get_events(limit),
        }

    @api.post("/api/refresh")
    async def refresh(request: Request) -> dict:
        return (await request.app.state.poller.refresh()).model_dump()

    return api


app = create_app()
