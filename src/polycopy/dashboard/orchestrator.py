"""Orchestrateur Dashboard (M4.5) — entrée dans le ``TaskGroup`` ``__main__``."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.dashboard.routes import build_app
from polycopy.dashboard.server import serve_dashboard

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


class DashboardOrchestrator:
    """Sert le dashboard FastAPI tant que ``stop_event`` n'est pas déclenché."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : démarre uvicorn et attend ``stop_event.set()``."""
        log.info(
            "dashboard_starting",
            host=self._settings.dashboard_host,
            port=self._settings.dashboard_port,
        )
        app = build_app(self._session_factory, self._settings)
        log.info(
            "dashboard_started",
            host=self._settings.dashboard_host,
            port=self._settings.dashboard_port,
        )
        try:
            await serve_dashboard(
                app,
                host=self._settings.dashboard_host,
                port=self._settings.dashboard_port,
                stop_event=stop_event,
            )
        finally:
            log.info("dashboard_stopped")
