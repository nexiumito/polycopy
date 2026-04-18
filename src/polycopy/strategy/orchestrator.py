"""Orchestrateur du Strategy Engine : consomme la queue watcher, run pipeline, persiste."""

import asyncio
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.dtos import DetectedTradeDTO, StrategyDecisionDTO
from polycopy.storage.repositories import StrategyDecisionRepository
from polycopy.strategy.clob_read_client import ClobReadClient
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
        settings: "Settings",
        detected_trades_queue: asyncio.Queue[DetectedTradeDTO],
        approved_orders_queue: asyncio.Queue[OrderApproved],
        alerts_queue: "asyncio.Queue[Alert] | None" = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._in_queue = detected_trades_queue
        self._out_queue = approved_orders_queue
        # Strategy n'émet pas d'alertes à M4 mais accepte la queue par cohérence.
        self._alerts = alerts_queue
        self._decision_repo = StrategyDecisionRepository(session_factory)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale jusqu'à ce que `stop_event` soit set."""
        log.info(
            "strategy_started",
            pipeline_steps=["MarketFilter", "PositionSizer", "SlippageChecker", "RiskManager"],
            available_capital_stub=self._settings.risk_available_capital_usd_stub,
        )
        async with httpx.AsyncClient() as http_client:
            gamma_client = GammaApiClient(http_client)
            clob_client = ClobReadClient(http_client)
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
                    await self._handle_trade(trade, gamma_client, clob_client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("pipeline_error", tx_hash=trade.tx_hash)
                    await self._persist_pipeline_error(trade)
        log.info("strategy_stopped")

    async def _handle_trade(
        self,
        trade: DetectedTradeDTO,
        gamma_client: GammaApiClient,
        clob_client: ClobReadClient,
    ) -> None:
        decision, reason, ctx = await run_pipeline(
            trade,
            gamma_client=gamma_client,
            clob_client=clob_client,
            session_factory=self._session_factory,
            settings=self._settings,
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
            )
            try:
                self._out_queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("executor_queue_full", tx_hash=trade.tx_hash)
            bound.info("order_approved")
        else:
            bound.info("order_rejected")

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
