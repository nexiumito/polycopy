"""Orchestrateur Executor : consomme `approved_orders_queue` et exécute."""

import asyncio
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.executor.clob_metadata_client import ClobMetadataClient
from polycopy.executor.clob_write_client import ClobWriteClient
from polycopy.executor.dtos import ExecutorAuthError
from polycopy.executor.pipeline import execute_order
from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.storage.repositories import MyOrderRepository, MyPositionRepository
from polycopy.strategy.dtos import OrderApproved
from polycopy.strategy.gamma_client import GammaApiClient

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.monitoring.dtos import Alert

log = structlog.get_logger(__name__)

_QUEUE_GET_TIMEOUT_SECONDS: float = 1.0


class ExecutorOrchestrator:
    """Pull `OrderApproved` depuis la queue M2 et exécute via le pipeline.

    Garde-fou démarrage strict : si `dry_run=False` ET clés absentes,
    raise `RuntimeError` AVANT le TaskGroup (cf. spec §2.2).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: "Settings",
        approved_orders_queue: asyncio.Queue[OrderApproved],
        alerts_queue: "asyncio.Queue[Alert] | None" = None,
    ) -> None:
        if settings.dry_run is False and (
            settings.polymarket_private_key is None or settings.polymarket_funder is None
        ):
            raise RuntimeError(
                "Executor cannot start without Polymarket credentials when DRY_RUN=false. "
                "Set POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in .env, or use --dry-run.",
            )
        self._session_factory = session_factory
        self._settings = settings
        self._queue = approved_orders_queue
        self._alerts = alerts_queue

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale jusqu'à `stop_event.set()`."""
        order_repo = MyOrderRepository(self._session_factory)
        position_repo = MyPositionRepository(self._session_factory)
        async with httpx.AsyncClient() as http_client:
            metadata_client = ClobMetadataClient(http_client)
            gamma_client = GammaApiClient(http_client)
            wallet_state_reader = WalletStateReader(http_client, self._settings)
            write_client: ClobWriteClient | None = None
            if self._settings.dry_run is False:
                write_client = ClobWriteClient(self._settings)
            mode = "real" if self._settings.dry_run is False else "dry_run"
            log.info("executor_started", mode=mode)
            while not stop_event.is_set():
                try:
                    approved = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=_QUEUE_GET_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
                try:
                    await execute_order(
                        approved,
                        settings=self._settings,
                        metadata_client=metadata_client,
                        gamma_client=gamma_client,
                        write_client=write_client,
                        wallet_state_reader=wallet_state_reader,
                        order_repo=order_repo,
                        position_repo=position_repo,
                        alerts_queue=self._alerts,
                    )
                except ExecutorAuthError:
                    log.error("executor_auth_fatal")
                    self._push_auth_alert()
                    stop_event.set()
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("executor_loop_error", tx_hash=approved.tx_hash)
        log.info("executor_stopped")

    def _push_auth_alert(self) -> None:
        """Push un ``Alert`` CRITICAL avant ``stop_event.set()``. No-op sans queue."""
        if self._alerts is None:
            return
        # Import local pour éviter import circulaire monitoring↔executor au load.
        from polycopy.monitoring.dtos import Alert

        alert = Alert(
            level="CRITICAL",
            event="executor_auth_fatal",
            body=(
                "Executor auth fatal — CLOB API key rejetée. "
                "Vérifier POLYMARKET_PRIVATE_KEY / signature_type / funder. "
                "Bot arrêté."
            ),
            cooldown_key="auth",
        )
        try:
            self._alerts.put_nowait(alert)
        except asyncio.QueueFull:
            log.warning("alerts_queue_full", event=alert.event)
