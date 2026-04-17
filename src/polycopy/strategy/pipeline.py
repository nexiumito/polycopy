"""Pipeline de filtres du Strategy Engine.

Ordre fixe : MarketFilter → PositionSizer → SlippageChecker → RiskManager.
Premier rejet = arrêt du pipeline. Tous OK = `OrderApproved`.

Pas d'abstraction `AbstractFilter` à M2 — 4 classes concrètes (rule of three
pas encore atteinte, cf. `CLAUDE.md`).
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.models import MyPosition
from polycopy.strategy.clob_read_client import ClobReadClient
from polycopy.strategy.dtos import FilterResult, PipelineContext
from polycopy.strategy.gamma_client import GammaApiClient

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


class MarketFilter:
    """Vérifie via Gamma que le marché est tradable et a une expiration assez lointaine."""

    def __init__(self, gamma_client: GammaApiClient, settings: "Settings") -> None:
        self._gamma = gamma_client
        self._settings = settings

    async def check(self, ctx: PipelineContext) -> FilterResult:
        market = await self._gamma.get_market(ctx.trade.condition_id)
        if market is None:
            return FilterResult(passed=False, reason="market_not_found")
        ctx.market = market
        if market.active is False:
            return FilterResult(passed=False, reason="market_inactive")
        if market.closed or market.archived:
            return FilterResult(passed=False, reason="market_closed")
        if market.accepting_orders is False or market.enable_order_book is False:
            return FilterResult(passed=False, reason="orderbook_disabled")
        if (market.liquidity_clob or 0.0) < self._settings.min_market_liquidity_usd:
            return FilterResult(passed=False, reason="liquidity_too_low")
        end = self._resolve_end_datetime(market.end_date, market.end_date_iso)
        if end is not None:
            hours = (end - datetime.now(tz=UTC)).total_seconds() / 3600.0
            if hours < self._settings.min_hours_to_expiry:
                return FilterResult(passed=False, reason="expiry_too_close")
        return FilterResult(passed=True)

    @staticmethod
    def _resolve_end_datetime(
        end_date: datetime | None,
        end_date_iso: str | None,
    ) -> datetime | None:
        if end_date is not None:
            return end_date if end_date.tzinfo else end_date.replace(tzinfo=UTC)
        if end_date_iso is None:
            return None
        # "YYYY-MM-DD" → fin de journée UTC ; sinon parse ISO direct.
        try:
            if len(end_date_iso) == 10:  # YYYY-MM-DD
                return datetime.fromisoformat(end_date_iso + "T23:59:59+00:00")
            parsed = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None


class PositionSizer:
    """Calcule `my_size` selon `COPY_RATIO` plafonné à `MAX_POSITION_USD`.

    Rejette si une position est déjà ouverte sur le `condition_id` (table
    `my_positions` — vide à M2, peuplée à M3).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: "Settings",
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def check(self, ctx: PipelineContext) -> FilterResult:
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.condition_id == ctx.trade.condition_id,
                MyPosition.closed_at.is_(None),
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return FilterResult(passed=False, reason="position_already_open")
        raw_size = ctx.trade.size * self._settings.copy_ratio
        cap_size = self._settings.max_position_usd / ctx.trade.price if ctx.trade.price > 0 else 0.0
        ctx.my_size = min(raw_size, cap_size)
        if ctx.my_size <= 0:
            return FilterResult(passed=False, reason="size_zero")
        return FilterResult(passed=True)


class SlippageChecker:
    """Compare le mid CLOB courant au prix source ; rejette si > `MAX_SLIPPAGE_PCT`."""

    def __init__(self, clob_client: ClobReadClient, settings: "Settings") -> None:
        self._clob = clob_client
        self._settings = settings

    async def check(self, ctx: PipelineContext) -> FilterResult:
        mid = await self._clob.get_midpoint(ctx.trade.asset_id)
        if mid is None:
            return FilterResult(passed=False, reason="no_orderbook")
        ctx.midpoint = mid
        if ctx.trade.price <= 0:
            return FilterResult(passed=False, reason="invalid_source_price")
        slippage = abs(mid - ctx.trade.price) / ctx.trade.price
        ctx.slippage_pct = slippage * 100.0
        if ctx.slippage_pct > self._settings.max_slippage_pct:
            return FilterResult(passed=False, reason="slippage_exceeded")
        return FilterResult(passed=True)


class RiskManager:
    """Vérifie capital disponible, exposition totale et drawdown."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: "Settings",
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def check(self, ctx: PipelineContext) -> FilterResult:
        if ctx.my_size is None or ctx.midpoint is None:
            return FilterResult(passed=False, reason="risk_inputs_missing")
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(MyPosition.closed_at.is_(None))
            open_positions = list((await session.execute(stmt)).scalars().all())
        current_exposure = sum((p.size or 0.0) * (p.avg_price or 0.0) for p in open_positions)
        prospective_cost = ctx.my_size * ctx.midpoint
        if current_exposure + prospective_cost > self._settings.risk_available_capital_usd_stub:
            return FilterResult(passed=False, reason="capital_exceeded")
        # Drawdown : `pnl_snapshots` est vide à M2 → drawdown = 0%.
        return FilterResult(passed=True)


async def run_pipeline(
    trade: DetectedTradeDTO,
    *,
    gamma_client: GammaApiClient,
    clob_client: ClobReadClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: "Settings",
) -> tuple[Literal["APPROVED", "REJECTED"], str | None, PipelineContext]:
    """Exécute les 4 filtres en séquence. Premier rejet = arrêt."""
    ctx = PipelineContext(trade=trade)
    filters = (
        ("MarketFilter", MarketFilter(gamma_client, settings)),
        ("PositionSizer", PositionSizer(session_factory, settings)),
        ("SlippageChecker", SlippageChecker(clob_client, settings)),
        ("RiskManager", RiskManager(session_factory, settings)),
    )
    for name, f in filters:
        result = await f.check(ctx)
        ctx.record_filter(name, result)
        if not result.passed:
            return "REJECTED", result.reason, ctx
    return "APPROVED", None, ctx
