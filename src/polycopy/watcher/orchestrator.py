"""Orchestrateur async : pilote les pollers sur tous les wallets actifs.

Le `stop_event` et les signal handlers vivent dans `__main__` depuis M2 (partagés
avec `StrategyOrchestrator`).

M5_ter : boucle de reload périodique (TTL `WATCHER_RELOAD_INTERVAL_SECONDS`).
À chaque cycle, re-fetch `list_wallets_to_poll()` et diff set-based contre les
pollers en cours — `tg.create_task` pour les nouveaux, `task.cancel()` pour les
retirés. Réactif aux mutations M5 (promote/demote) et M5_bis (eviction cascade,
sell_only wind-down, blacklist reconcile) sans restart.
"""

from __future__ import annotations

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
    """Démarre 1 `WalletPoller` par wallet à poller. Push sur `detected_trades_queue` si fournie.

    M5_ter : la liste des pollers est reloadée à chaque
    ``WATCHER_RELOAD_INTERVAL_SECONDS`` — les mutations DB (M5/M5_bis)
    sont propagées en quasi-temps-réel sans restart.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        detected_trades_queue: asyncio.Queue[DetectedTradeDTO] | None = None,
        alerts_queue: asyncio.Queue[Alert] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._out_queue = detected_trades_queue
        # Watcher n'émet pas d'alertes à M4 mais accepte la queue par cohérence.
        self._alerts = alerts_queue

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale jusqu'à ce que `stop_event` soit set.

        M5_ter : le TaskGroup reste vivant sur toute la durée ; les pollers
        sont ajoutés/retirés dynamiquement via ``tg.create_task()`` et
        ``task.cancel()``. Le premier cycle démarre les pollers seedés en
        DB ; un ``list_wallets_to_poll()`` vide au boot n'est plus un
        early-return — l'orchestrator attend les promotions futures M5.
        """
        target_repo = TargetTraderRepository(self._session_factory)
        trade_repo = DetectedTradeRepository(self._session_factory)
        latency_repo: TradeLatencyRepository | None = (
            TradeLatencyRepository(self._session_factory)
            if self._settings.latency_instrumentation_enabled
            else None
        )
        interval = self._settings.watcher_reload_interval_seconds
        pollers_by_wallet: dict[str, asyncio.Task[None]] = {}
        # M15 MB.6 : map partagée wallet→is_probation, rafraîchie à chaque
        # cycle reload. WalletPoller lookup ce dict via une closure (resolver
        # sync, 0 query par trade détecté).
        probation_map: dict[str, bool] = {}
        log.info(
            "watcher_started",
            poll_interval=self._settings.poll_interval_seconds,
            reload_interval=interval,
        )
        async with httpx.AsyncClient() as http_client:
            api_client = DataApiClient(http_client)
            try:
                async with asyncio.TaskGroup() as tg:
                    await self._reload_loop(
                        tg=tg,
                        target_repo=target_repo,
                        trade_repo=trade_repo,
                        api_client=api_client,
                        latency_repo=latency_repo,
                        stop_event=stop_event,
                        pollers_by_wallet=pollers_by_wallet,
                        interval=interval,
                        probation_map=probation_map,
                    )
            except* asyncio.CancelledError:
                pass
        log.info("watcher_stopped", final_pollers=len(pollers_by_wallet))

    async def _reload_loop(
        self,
        *,
        tg: asyncio.TaskGroup,
        target_repo: TargetTraderRepository,
        trade_repo: DetectedTradeRepository,
        api_client: DataApiClient,
        latency_repo: TradeLatencyRepository | None,
        stop_event: asyncio.Event,
        pollers_by_wallet: dict[str, asyncio.Task[None]],
        interval: int,
        probation_map: dict[str, bool],
    ) -> None:
        # M15 MB.6 : closure resolver — lookup sync dans `probation_map`
        # qui est rafraîchi à chaque cycle reload. Default False si wallet
        # absent (wallet vient d'être ajouté entre 2 reloads, mais le poller
        # va se relancer au prochain reload avec la bonne valeur).
        def probation_resolver(wallet_address: str) -> bool:
            return probation_map.get(wallet_address.lower(), False)

        while not stop_event.is_set():
            try:
                desired_traders = await target_repo.list_wallets_to_poll(
                    blacklist=self._settings.blacklisted_wallets,
                )
            except Exception:
                log.warning("watcher_reload_failed", exc_info=True)
                if await self._sleep_or_stop(stop_event, interval):
                    return
                continue

            desired = {t.wallet_address.lower() for t in desired_traders}
            current = set(pollers_by_wallet.keys())
            to_add = desired - current
            to_remove = current - desired

            # M15 MB.6 : refresh la map probation depuis le snapshot courant.
            probation_map.clear()
            probation_map.update(
                {t.wallet_address.lower(): bool(t.is_probation) for t in desired_traders},
            )

            if to_remove:
                await _cancel_pollers(pollers_by_wallet, to_remove)

            for wallet in sorted(to_add):
                if stop_event.is_set():
                    break
                poller = WalletPoller(
                    wallet_address=wallet,
                    client=api_client,
                    repo=trade_repo,
                    interval_seconds=self._settings.poll_interval_seconds,
                    out_queue=self._out_queue,
                    latency_repo=latency_repo,
                    instrumentation_enabled=self._settings.latency_instrumentation_enabled,
                    # M15 MB.6 : closure resolver injectée — chaque trade
                    # détecté lookup le flag à publish-time.
                    probation_resolver=probation_resolver,
                )
                pollers_by_wallet[wallet] = tg.create_task(
                    poller.run(stop_event),
                    name=f"wallet_poller:{wallet}",
                )

            if to_add or to_remove:
                log.info(
                    "watcher_reload_cycle",
                    added=len(to_add),
                    removed=len(to_remove),
                    total=len(pollers_by_wallet),
                    added_wallets=sorted(to_add),
                    removed_wallets=sorted(to_remove),
                )
            else:
                log.debug(
                    "watcher_reload_cycle_noop",
                    total=len(pollers_by_wallet),
                )

            if await self._sleep_or_stop(stop_event, interval):
                return

    @staticmethod
    async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
        """Attend ``seconds`` ou le set du ``stop_event``. Retourne True si stop."""
        if seconds <= 0:
            await asyncio.sleep(0)
            return stop_event.is_set()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True


async def _cancel_pollers(
    pollers_by_wallet: dict[str, asyncio.Task[None]],
    wallets_to_remove: set[str],
) -> None:
    """Cancel + await les pollers retirés. ``return_exceptions=True`` absorbe
    les ``CancelledError`` individuelles et les erreurs terminales."""
    tasks_to_cancel: list[asyncio.Task[None]] = []
    for wallet in wallets_to_remove:
        task = pollers_by_wallet.pop(wallet, None)
        if task is not None and not task.done():
            task.cancel()
            tasks_to_cancel.append(task)
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
