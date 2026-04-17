"""Tests du pipeline de filtres : 1 test par chemin REJECT + bout-en-bout APPROVED."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.models import MyPosition
from polycopy.strategy.clob_read_client import ClobReadClient
from polycopy.strategy.dtos import MarketMetadata, PipelineContext
from polycopy.strategy.gamma_client import GammaApiClient
from polycopy.strategy.pipeline import (
    MarketFilter,
    PositionSizer,
    RiskManager,
    SlippageChecker,
    run_pipeline,
)


def _trade(price: float = 0.08, size: float = 100.0) -> DetectedTradeDTO:
    return DetectedTradeDTO(
        tx_hash="0xtx",
        target_wallet="0xw",
        condition_id="0xc",
        asset_id="123",
        side="BUY",
        size=size,
        usdc_size=size * price,
        price=price,
        timestamp=datetime.now(tz=UTC),
        raw_json={},
    )


def _settings(**overrides: Any) -> Settings:
    base = {
        "copy_ratio": 0.01,
        "max_position_usd": 100.0,
        "min_market_liquidity_usd": 5000.0,
        "min_hours_to_expiry": 24.0,
        "max_slippage_pct": 2.0,
        "kill_switch_drawdown_pct": 20.0,
        "risk_available_capital_usd_stub": 1000.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _market(
    *,
    active: bool = True,
    closed: bool = False,
    archived: bool = False,
    accepting_orders: bool = True,
    enable_order_book: bool = True,
    liquidity_clob: float = 50000.0,
    end_date: datetime | None = None,
) -> MarketMetadata:
    return MarketMetadata(
        id="1",
        conditionId="0xc",
        active=active,
        closed=closed,
        archived=archived,
        acceptingOrders=accepting_orders,
        enableOrderBook=enable_order_book,
        liquidityClob=liquidity_clob,
        endDate=end_date or (datetime.now(tz=UTC) + timedelta(days=30)),
        clobTokenIds='["123","456"]',
        outcomes='["Yes","No"]',
    )


# --- MarketFilter ------------------------------------------------------------


async def test_market_filter_market_not_found() -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = None
    f = MarketFilter(gamma, _settings())
    ctx = PipelineContext(trade=_trade())
    result = await f.check(ctx)
    assert result.passed is False
    assert result.reason == "market_not_found"


async def test_market_filter_inactive() -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = _market(active=False)
    f = MarketFilter(gamma, _settings())
    result = await f.check(PipelineContext(trade=_trade()))
    assert result.reason == "market_inactive"


async def test_market_filter_closed() -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = _market(closed=True)
    f = MarketFilter(gamma, _settings())
    result = await f.check(PipelineContext(trade=_trade()))
    assert result.reason == "market_closed"


async def test_market_filter_orderbook_disabled() -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = _market(enable_order_book=False)
    f = MarketFilter(gamma, _settings())
    result = await f.check(PipelineContext(trade=_trade()))
    assert result.reason == "orderbook_disabled"


async def test_market_filter_liquidity_too_low() -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = _market(liquidity_clob=1000.0)
    f = MarketFilter(gamma, _settings(min_market_liquidity_usd=5000.0))
    result = await f.check(PipelineContext(trade=_trade()))
    assert result.reason == "liquidity_too_low"


async def test_market_filter_expiry_too_close() -> None:
    soon = datetime.now(tz=UTC) + timedelta(hours=1)
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = _market(end_date=soon)
    f = MarketFilter(gamma, _settings(min_hours_to_expiry=24.0))
    result = await f.check(PipelineContext(trade=_trade()))
    assert result.reason == "expiry_too_close"


async def test_market_filter_pass() -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = _market()
    f = MarketFilter(gamma, _settings())
    ctx = PipelineContext(trade=_trade())
    result = await f.check(ctx)
    assert result.passed is True
    assert ctx.market is not None


# --- PositionSizer -----------------------------------------------------------


async def test_position_sizer_position_already_open(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            MyPosition(condition_id="0xc", asset_id="123", size=1.0, avg_price=0.5),
        )
        await session.commit()
    f = PositionSizer(session_factory, _settings())
    result = await f.check(PipelineContext(trade=_trade()))
    assert result.reason == "position_already_open"


async def test_position_sizer_pass_with_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    f = PositionSizer(session_factory, _settings(max_position_usd=10.0))
    ctx = PipelineContext(trade=_trade(price=0.5, size=10000.0))
    result = await f.check(ctx)
    assert result.passed is True
    # raw_size = 10000 * 0.01 = 100 ; cap = 10 / 0.5 = 20 ; min = 20.
    assert ctx.my_size == 20.0


async def test_position_sizer_pass_no_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    f = PositionSizer(session_factory, _settings(max_position_usd=10000.0))
    ctx = PipelineContext(trade=_trade(price=0.5, size=10.0))
    result = await f.check(ctx)
    assert result.passed is True
    # raw_size = 0.1 < cap (10000/0.5=20000)
    assert ctx.my_size == pytest.approx(0.1)


# --- SlippageChecker ---------------------------------------------------------


async def test_slippage_checker_no_orderbook() -> None:
    clob = AsyncMock(spec=ClobReadClient)
    clob.get_midpoint.return_value = None
    f = SlippageChecker(clob, _settings())
    result = await f.check(PipelineContext(trade=_trade()))
    assert result.reason == "no_orderbook"


async def test_slippage_checker_exceeded() -> None:
    clob = AsyncMock(spec=ClobReadClient)
    clob.get_midpoint.return_value = 0.20  # source=0.08 → 150% slippage
    f = SlippageChecker(clob, _settings(max_slippage_pct=2.0))
    ctx = PipelineContext(trade=_trade(price=0.08))
    result = await f.check(ctx)
    assert result.reason == "slippage_exceeded"
    assert ctx.slippage_pct is not None and ctx.slippage_pct > 2.0


async def test_slippage_checker_pass() -> None:
    clob = AsyncMock(spec=ClobReadClient)
    clob.get_midpoint.return_value = 0.0805  # ~0.6% off de 0.08
    f = SlippageChecker(clob, _settings(max_slippage_pct=2.0))
    ctx = PipelineContext(trade=_trade(price=0.08))
    result = await f.check(ctx)
    assert result.passed is True
    assert ctx.midpoint == 0.0805
    assert ctx.slippage_pct is not None and ctx.slippage_pct < 1.0


# --- RiskManager -------------------------------------------------------------


async def test_risk_manager_capital_exceeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    f = RiskManager(session_factory, _settings(risk_available_capital_usd_stub=1.0))
    ctx = PipelineContext(trade=_trade())
    ctx.my_size = 100.0
    ctx.midpoint = 0.5  # cost = 50 USD > 1 USD dispo
    result = await f.check(ctx)
    assert result.reason == "capital_exceeded"


async def test_risk_manager_pass(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    f = RiskManager(session_factory, _settings(risk_available_capital_usd_stub=1000.0))
    ctx = PipelineContext(trade=_trade())
    ctx.my_size = 1.0
    ctx.midpoint = 0.5
    result = await f.check(ctx)
    assert result.passed is True


# --- Pipeline bout-en-bout ---------------------------------------------------


async def test_full_pipeline_approved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = _market()
    clob = AsyncMock(spec=ClobReadClient)
    clob.get_midpoint.return_value = 0.0805

    decision, reason, ctx = await run_pipeline(
        _trade(),
        gamma_client=gamma,
        clob_client=clob,
        session_factory=session_factory,
        settings=_settings(),
    )
    assert decision == "APPROVED"
    assert reason is None
    assert ctx.market is not None
    assert ctx.my_size is not None and ctx.my_size > 0
    assert ctx.midpoint == 0.0805
    assert len(ctx.filter_trace) == 4
    assert all(step["passed"] for step in ctx.filter_trace)


async def test_full_pipeline_rejects_first_failing_filter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = None  # MarketFilter rejette en 1er
    clob = AsyncMock(spec=ClobReadClient)

    decision, reason, ctx = await run_pipeline(
        _trade(),
        gamma_client=gamma,
        clob_client=clob,
        session_factory=session_factory,
        settings=_settings(),
    )
    assert decision == "REJECTED"
    assert reason == "market_not_found"
    # Pipeline arrêté au 1er filtre — clob.get_midpoint jamais appelé.
    clob.get_midpoint.assert_not_called()
    assert len(ctx.filter_trace) == 1
