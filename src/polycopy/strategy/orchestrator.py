"""Orchestrateur du Strategy Engine : consomme la queue watcher, run pipeline, persiste.

M11 : instancie (conditionnellement) le ``ClobMarketWSClient`` et le passe à
``run_pipeline`` ; wrap ``run_pipeline`` avec un timer
``strategy_filtered_ms`` (stage 3, cumulatif MarketFilter + PositionSizer +
SlippageChecker + RiskManager). Rebind le ``trade_id`` contextvar à
l'entrée pour permettre aux logs downstream de porter l'id.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.executor.fee_rate_client import FeeRateClient
from polycopy.storage.dtos import DetectedTradeDTO, StrategyDecisionDTO
from polycopy.storage.repositories import (
    StrategyDecisionRepository,
    TradeLatencyRepository,
)
from polycopy.strategy.clob_read_client import ClobReadClient
from polycopy.strategy.clob_ws_client import ClobMarketWSClient
from polycopy.strategy.dtos import OrderApproved, PipelineContext
from polycopy.strategy.gamma_client import GammaApiClient
from polycopy.strategy.pipeline import run_pipeline

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.monitoring.dtos import Alert

log = structlog.get_logger(__name__)

_QUEUE_GET_TIMEOUT_SECONDS: float = 1.0


class StrategyOrchestrator:
    """Pull `DetectedTradeDTO` depuis la queue watcher, run pipeline, push `OrderApproved`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        detected_trades_queue: asyncio.Queue[DetectedTradeDTO],
        approved_orders_queue: asyncio.Queue[OrderApproved],
        alerts_queue: asyncio.Queue[Alert] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._in_queue = detected_trades_queue
        self._out_queue = approved_orders_queue
        # Strategy n'émet pas d'alertes à M4 mais accepte la queue par cohérence.
        self._alerts = alerts_queue
        self._decision_repo = StrategyDecisionRepository(session_factory)
        self._latency_repo: TradeLatencyRepository | None = (
            TradeLatencyRepository(session_factory)
            if settings.latency_instrumentation_enabled
            else None
        )
        self._ws_client: ClobMarketWSClient | None = None

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale jusqu'à ce que `stop_event` soit set."""
        log.info(
            "strategy_started",
            pipeline_steps=["MarketFilter", "PositionSizer", "SlippageChecker", "RiskManager"],
            available_capital_stub=self._settings.risk_available_capital_usd_stub,
            ws_enabled=self._settings.strategy_clob_ws_enabled,
            latency_instrumentation=self._settings.latency_instrumentation_enabled,
            fees_aware=self._settings.strategy_fees_aware_enabled,
        )
        async with httpx.AsyncClient() as http_client:
            gamma_client = GammaApiClient(http_client, settings=self._settings)
            clob_client = ClobReadClient(http_client, settings=self._settings)
            # M16 : FeeRateClient opt-in via STRATEGY_FEES_AWARE_ENABLED.
            # Partage le httpx.AsyncClient (read-only public no-auth, pas de
            # cred touché). Co-lancement Strategy (pas Executor) car le fee
            # check vit pre-POST dans PositionSizer (décision D2).
            fee_rate_client: FeeRateClient | None = None
            if self._settings.strategy_fees_aware_enabled:
                fee_rate_client = FeeRateClient(
                    http_client,
                    cache_max=self._settings.strategy_fee_rate_cache_max,
                    settings=self._settings,
                )
                log.info(
                    "fee_rate_client_instantiated",
                    cache_max=self._settings.strategy_fee_rate_cache_max,
                )
            if self._settings.strategy_clob_ws_enabled:
                self._ws_client = ClobMarketWSClient(self._settings)

            async with asyncio.TaskGroup() as tg:
                if self._ws_client is not None:
                    tg.create_task(
                        self._ws_client.run(stop_event),
                        name="clob_ws_client",
                    )
                tg.create_task(
                    self._consume_loop(stop_event, gamma_client, clob_client, fee_rate_client),
                    name="strategy_consumer",
                )
        log.info("strategy_stopped")

    async def _consume_loop(
        self,
        stop_event: asyncio.Event,
        gamma_client: GammaApiClient,
        clob_client: ClobReadClient,
        fee_rate_client: FeeRateClient | None = None,
    ) -> None:
        while not stop_event.is_set():
            try:
                trade = await asyncio.wait_for(
                    self._in_queue.get(),
                    timeout=_QUEUE_GET_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self._handle_trade(trade, gamma_client, clob_client, fee_rate_client)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pipeline_error", tx_hash=trade.tx_hash)
                await self._persist_pipeline_error(trade)

    async def _handle_trade(
        self,
        trade: DetectedTradeDTO,
        gamma_client: GammaApiClient,
        clob_client: ClobReadClient,
        fee_rate_client: FeeRateClient | None = None,
    ) -> None:
        # M11 : (re)bind trade_id contextvar côté strategy pour propager dans
        # tous les logs de cette task asyncio.
        token = None
        if trade.trade_id is not None:
            token = structlog.contextvars.bind_contextvars(trade_id=trade.trade_id)
        instrumented = self._settings.latency_instrumentation_enabled and trade.trade_id is not None
        t0 = time.perf_counter_ns() if instrumented else 0
        try:
            decision, reason, ctx = await run_pipeline(
                trade,
                gamma_client=gamma_client,
                clob_client=clob_client,
                session_factory=self._session_factory,
                settings=self._settings,
                ws_client=self._ws_client,
                latency_repo=self._latency_repo,
                fee_rate_client=fee_rate_client,
            )
        finally:
            if instrumented and trade.trade_id is not None:
                duration_ms = (time.perf_counter_ns() - t0) / 1e6
                log.info(
                    "stage_complete",
                    stage_name="strategy_filtered_ms",
                    stage_duration_ms=round(duration_ms, 3),
                )
                if self._latency_repo is not None:
                    await self._latency_repo.insert(
                        trade.trade_id,
                        "strategy_filtered_ms",
                        duration_ms,
                    )
        await self._persist_decision(trade, decision, reason, ctx)
        bound = log.bind(
            tx_hash=trade.tx_hash,
            condition_id=trade.condition_id,
            reason=reason,
            my_size=ctx.my_size,
            slippage_pct=ctx.slippage_pct,
        )
        if decision == "APPROVED":
            assert ctx.my_size is not None and ctx.midpoint is not None
            event = OrderApproved(
                detected_trade_id=self._infer_trade_id(trade),
                tx_hash=trade.tx_hash,
                condition_id=trade.condition_id,
                asset_id=trade.asset_id,
                side=trade.side,
                my_size=ctx.my_size,
                my_price=ctx.midpoint,
                trade_id=trade.trade_id,
                # M15 MB.1 : propage le wallet source pour persistance MyPosition
                # (alimente le collecteur internal_pnl côté discovery).
                source_wallet_address=trade.target_wallet.lower(),
            )
            try:
                self._out_queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("executor_queue_full", tx_hash=trade.tx_hash)
            bound.info("order_approved")
        else:
            bound.info("order_rejected")
        if token is not None:
            with contextlib.suppress(Exception):
                structlog.contextvars.unbind_contextvars("trade_id")

    async def _persist_decision(
        self,
        trade: DetectedTradeDTO,
        decision: str,
        reason: str | None,
        ctx: PipelineContext,
    ) -> None:
        dto = StrategyDecisionDTO(
            detected_trade_id=self._infer_trade_id(trade),
            tx_hash=trade.tx_hash,
            decision=decision,
            reason=reason,
            my_size=ctx.my_size,
            my_price=ctx.midpoint,
            slippage_pct=ctx.slippage_pct,
            pipeline_state=ctx.to_audit_dict(),
        )
        await self._decision_repo.insert(dto)

    async def _persist_pipeline_error(self, trade: DetectedTradeDTO) -> None:
        dto = StrategyDecisionDTO(
            detected_trade_id=self._infer_trade_id(trade),
            tx_hash=trade.tx_hash,
            decision="REJECTED",
            reason="pipeline_error",
            pipeline_state={"tx_hash": trade.tx_hash, "error": "see logs"},
        )
        await self._decision_repo.insert(dto)

    @staticmethod
    def _infer_trade_id(trade: DetectedTradeDTO) -> int:
        """`DetectedTradeDTO` ne porte pas l'`id` SQL ; à M2 on stocke 0 (lookup
        possible plus tard via `tx_hash` si M3/M4 en a besoin)."""
        return 0
