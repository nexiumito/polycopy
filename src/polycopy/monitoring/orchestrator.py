"""Orchestrateur Monitoring (M4) : ``PnlSnapshotWriter`` + ``AlertDispatcher``."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.monitoring.alert_dispatcher import AlertDispatcher
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.pnl_writer import PnlSnapshotWriter
from polycopy.monitoring.telegram_client import TelegramClient

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


class MonitoringOrchestrator:
    """Lance en parallèle le writer de snapshots et le dispatcher d'alertes."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert],
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._alerts = alerts_queue

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : deux sous-tâches jusqu'à ``stop_event.set()``."""
        async with httpx.AsyncClient() as http_client:
            wallet_reader = WalletStateReader(http_client, self._settings)
            telegram = TelegramClient(http_client, self._settings)
            if telegram.enabled:
                log.info("telegram_enabled")
            else:
                log.warning("telegram_disabled")
            log.info(
                "monitoring_started",
                telegram_enabled=telegram.enabled,
                pnl_interval=self._settings.pnl_snapshot_interval_seconds,
            )
            writer = PnlSnapshotWriter(
                self._session_factory,
                self._settings,
                wallet_reader,
                self._alerts,
            )
            dispatcher = AlertDispatcher(self._alerts, telegram, self._settings)
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(writer.run(stop_event))
                    tg.create_task(dispatcher.run(stop_event))
            except* asyncio.CancelledError:
                pass
        log.info("monitoring_stopped")
