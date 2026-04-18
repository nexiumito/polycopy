"""Orchestrateur async : pilote les pollers sur tous les wallets actifs.

Le `stop_event` et les signal handlers vivent dans `__main__` depuis M2 (partagés
avec `StrategyOrchestrator`).
"""

import asyncio
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    TargetTraderRepository,
    TradeLatencyRepository,
)
from polycopy.watcher.data_api_client import DataApiClient
from polycopy.watcher.wallet_poller import WalletPoller

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.monitoring.dtos import Alert

log = structlog.get_logger(__name__)


class WatcherOrchestrator:
    """Démarre 1 `WalletPoller` par wallet actif. Push sur `detected_trades_queue` si fournie."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: "Settings",
        detected_trades_queue: asyncio.Queue[DetectedTradeDTO] | None = None,
        alerts_queue: "asyncio.Queue[Alert] | None" = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._out_queue = detected_trades_queue
        # Watcher n'émet pas d'alertes à M4 mais accepte la queue par cohérence.
        self._alerts = alerts_queue

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale jusqu'à ce que `stop_event` soit set."""
        target_repo = TargetTraderRepository(self._session_factory)
        trade_repo = DetectedTradeRepository(self._session_factory)
        traders = await target_repo.list_active()
        if not traders:
            log.warning("watcher_no_active_targets")
            return
        wallets = [t.wallet_address for t in traders]
        log.info(
            "watcher_started",
            wallets=wallets,
            interval=self._settings.poll_interval_seconds,
        )
        latency_repo: TradeLatencyRepository | None = (
            TradeLatencyRepository(self._session_factory)
            if self._settings.latency_instrumentation_enabled
            else None
        )
        async with httpx.AsyncClient() as http_client:
            api_client = DataApiClient(http_client)
            pollers = [
                WalletPoller(
                    wallet_address=wallet,
                    client=api_client,
                    repo=trade_repo,
                    interval_seconds=self._settings.poll_interval_seconds,
                    out_queue=self._out_queue,
                    latency_repo=latency_repo,
                    instrumentation_enabled=self._settings.latency_instrumentation_enabled,
                )
                for wallet in wallets
            ]
            try:
                async with asyncio.TaskGroup() as tg:
                    for poller in pollers:
                        tg.create_task(poller.run(stop_event))
            except* asyncio.CancelledError:
                pass
        log.info("watcher_stopped")
