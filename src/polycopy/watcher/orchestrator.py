"""Orchestrateur async : pilote les pollers sur tous les wallets actifs."""

import asyncio
import contextlib
import signal
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.repositories import (
    DetectedTradeRepository,
    TargetTraderRepository,
)
from polycopy.watcher.data_api_client import DataApiClient
from polycopy.watcher.wallet_poller import WalletPoller

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


class WatcherOrchestrator:
    """Démarre 1 `WalletPoller` par wallet actif et orchestre la sortie propre."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: "Settings",
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._stop_event = asyncio.Event()

    async def run_forever(self) -> None:
        """Boucle principale jusqu'à SIGINT/SIGTERM ou `request_stop()`."""
        self._install_signal_handlers()
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
        async with httpx.AsyncClient() as http_client:
            api_client = DataApiClient(http_client)
            pollers = [
                WalletPoller(
                    wallet_address=wallet,
                    client=api_client,
                    repo=trade_repo,
                    interval_seconds=self._settings.poll_interval_seconds,
                )
                for wallet in wallets
            ]
            try:
                async with asyncio.TaskGroup() as tg:
                    for poller in pollers:
                        tg.create_task(poller.run(self._stop_event))
            except* asyncio.CancelledError:
                pass
        log.info("watcher_stopped")

    def request_stop(self) -> None:
        """Demande l'arrêt propre des pollers."""
        if not self._stop_event.is_set():
            log.info("watcher_stop_requested")
            self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        # Windows ProactorEventLoop ne supporte pas add_signal_handler ;
        # on retombe sur KeyboardInterrupt côté __main__.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_stop)
