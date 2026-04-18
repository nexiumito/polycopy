"""Orchestrateur Executor : consomme `approved_orders_queue` et exécute.

M11 : wrap chaque appel ``execute_order`` avec un timer
``executor_submitted_ms`` (stage 6). L'instrumentation est conditionnée à
``settings.latency_instrumentation_enabled`` ET
``approved.trade_id is not None`` — no-op sur les chemins M2..M10 stricts.
"""

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.executor.clob_metadata_client import ClobMetadataClient
from polycopy.executor.clob_orderbook_reader import ClobOrderbookReader
from polycopy.executor.clob_write_client import ClobWriteClient
from polycopy.executor.dry_run_resolution_watcher import DryRunResolutionWatcher
from polycopy.executor.dtos import ExecutorAuthError
from polycopy.executor.pipeline import execute_order
from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.storage.repositories import (
    MyOrderRepository,
    MyPositionRepository,
    TradeLatencyRepository,
)
from polycopy.strategy.dtos import OrderApproved
from polycopy.strategy.gamma_client import GammaApiClient

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.monitoring.dtos import Alert

log = structlog.get_logger(__name__)

_QUEUE_GET_TIMEOUT_SECONDS: float = 1.0


class ExecutorOrchestrator:
    """Pull `OrderApproved` depuis la queue M2 et exécute via le pipeline.

    Garde-fou démarrage strict (M3/M10) : si ``execution_mode == "live"`` ET
    clés absentes, raise ``RuntimeError`` AVANT le TaskGroup (cf. spec §2.2).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: "Settings",
        approved_orders_queue: asyncio.Queue[OrderApproved],
        alerts_queue: "asyncio.Queue[Alert] | None" = None,
    ) -> None:
        if settings.execution_mode == "live" and (
            settings.polymarket_private_key is None or settings.polymarket_funder is None
        ):
            raise RuntimeError(
                "Executor cannot start without Polymarket credentials when "
                "EXECUTION_MODE=live. Set POLYMARKET_PRIVATE_KEY and "
                "POLYMARKET_FUNDER in .env, or use EXECUTION_MODE=dry_run.",
            )
        self._session_factory = session_factory
        self._settings = settings
        self._queue = approved_orders_queue
        self._alerts = alerts_queue
        self._latency_repo: TradeLatencyRepository | None = (
            TradeLatencyRepository(session_factory)
            if settings.latency_instrumentation_enabled
            else None
        )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale jusqu'à `stop_event.set()`.

        M8 : si ``dry_run`` ET ``dry_run_realistic_fill``, instancie un
        ``ClobOrderbookReader`` partagé + lance ``DryRunResolutionWatcher``
        dans un TaskGroup parallèle à la boucle de consommation. Aucune
        creds touchée — le path live (``ClobWriteClient``) reste lazy comme
        en M3.
        """
        order_repo = MyOrderRepository(self._session_factory)
        position_repo = MyPositionRepository(self._session_factory)
        async with httpx.AsyncClient() as http_client:
            metadata_client = ClobMetadataClient(http_client)
            gamma_client = GammaApiClient(http_client)
            wallet_state_reader = WalletStateReader(http_client, self._settings)
            write_client: ClobWriteClient | None = None
            if self._settings.execution_mode == "live":
                write_client = ClobWriteClient(self._settings)
            orderbook_reader: ClobOrderbookReader | None = None
            resolution_watcher: DryRunResolutionWatcher | None = None
            m8_enabled = (
                self._settings.execution_mode == "dry_run" and self._settings.dry_run_realistic_fill
            )
            if m8_enabled:
                orderbook_reader = ClobOrderbookReader(
                    http_client,
                    ttl_seconds=self._settings.dry_run_book_cache_ttl_seconds,
                )
                resolution_watcher = DryRunResolutionWatcher(
                    self._session_factory,
                    gamma_client,
                    self._settings,
                )
                log.warning(
                    "dry_run_realistic_fill_enabled",
                    virtual_capital=self._settings.dry_run_virtual_capital_usd,
                    cache_ttl_s=self._settings.dry_run_book_cache_ttl_seconds,
                    poll_minutes=self._settings.dry_run_resolution_poll_minutes,
                    allow_partial=self._settings.dry_run_allow_partial_book,
                )
            log.info(
                "executor_started",
                mode=self._settings.execution_mode,
                m8=m8_enabled,
            )
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(
                        self._consume_loop(
                            stop_event,
                            metadata_client=metadata_client,
                            gamma_client=gamma_client,
                            wallet_state_reader=wallet_state_reader,
                            write_client=write_client,
                            order_repo=order_repo,
                            position_repo=position_repo,
                            orderbook_reader=orderbook_reader,
                        ),
                    )
                    if resolution_watcher is not None:
                        tg.create_task(resolution_watcher.run_forever(stop_event))
            except* asyncio.CancelledError:
                pass
            except* ExecutorAuthError as eg:
                # Preserve M3 contract : raise bare ExecutorAuthError (cf. test
                # `test_orchestrator_pushes_auth_alert_on_fatal`).
                raise next(iter(eg.exceptions)) from None
        log.info("executor_stopped")

    async def _consume_loop(
        self,
        stop_event: asyncio.Event,
        *,
        metadata_client: ClobMetadataClient,
        gamma_client: GammaApiClient,
        wallet_state_reader: WalletStateReader,
        write_client: ClobWriteClient | None,
        order_repo: MyOrderRepository,
        position_repo: MyPositionRepository,
        orderbook_reader: ClobOrderbookReader | None,
    ) -> None:
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
            # M11 : (re)bind trade_id contextvar côté executor + stage 6 timer.
            token = None
            if approved.trade_id is not None:
                token = structlog.contextvars.bind_contextvars(trade_id=approved.trade_id)
            instrumented = (
                self._settings.latency_instrumentation_enabled and approved.trade_id is not None
            )
            t0 = time.perf_counter_ns() if instrumented else 0
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
                    orderbook_reader=orderbook_reader,
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
            finally:
                if instrumented and approved.trade_id is not None:
                    duration_ms = (time.perf_counter_ns() - t0) / 1e6
                    log.info(
                        "stage_complete",
                        stage_name="executor_submitted_ms",
                        stage_duration_ms=round(duration_ms, 3),
                    )
                    if self._latency_repo is not None:
                        with contextlib.suppress(Exception):
                            await self._latency_repo.insert(
                                approved.trade_id,
                                "executor_submitted_ms",
                                duration_ms,
                            )
                if token is not None:
                    with contextlib.suppress(Exception):
                        structlog.contextvars.unbind_contextvars("trade_id")

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
