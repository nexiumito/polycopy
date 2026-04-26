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
    EntryPriceFilter,
    MarketFilter,
    PositionSizer,
    RiskManager,
    SlippageChecker,
    run_pipeline,
)


def _trade(
    price: float = 0.08,
    size: float = 100.0,
    *,
    side: str = "BUY",
    is_source_probation: bool = False,
) -> DetectedTradeDTO:
    return DetectedTradeDTO(
        tx_hash="0xtx",
        target_wallet="0xw",
        condition_id="0xc",
        asset_id="123",
        side=side,  # type: ignore[arg-type]
        size=size,
        usdc_size=size * price,
        price=price,
        timestamp=datetime.now(tz=UTC),
        raw_json={},
        is_source_probation=is_source_probation,
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


# --- EntryPriceFilter --------------------------------------------------------


async def test_entry_price_filter_rejects_buy_above_threshold() -> None:
    """BUY @ 0.99 avec max=0.97 → rejeté avec reason='entry_price_too_high'."""
    f = EntryPriceFilter(_settings(strategy_max_entry_price=0.97))
    result = await f.check(PipelineContext(trade=_trade(price=0.99)))
    assert result.passed is False
    assert result.reason == "entry_price_too_high"


async def test_entry_price_filter_accepts_buy_at_exact_threshold() -> None:
    """Comparaison stricte ``>`` : price==max doit passer (on rejette au-dessus)."""
    f = EntryPriceFilter(_settings(strategy_max_entry_price=0.97))
    result = await f.check(PipelineContext(trade=_trade(price=0.97)))
    assert result.passed is True


async def test_entry_price_filter_accepts_buy_below_threshold() -> None:
    """BUY @ 0.50 avec max=0.97 → accepté (zone normale)."""
    f = EntryPriceFilter(_settings(strategy_max_entry_price=0.97))
    result = await f.check(PipelineContext(trade=_trade(price=0.50)))
    assert result.passed is True


async def test_entry_price_filter_sell_passthrough_even_above_threshold() -> None:
    """SELL @ 0.99 doit passer — on doit pouvoir copier un SELL pour fermer."""
    f = EntryPriceFilter(_settings(strategy_max_entry_price=0.97))
    trade = _trade(price=0.99)
    trade = DetectedTradeDTO(**{**trade.model_dump(), "side": "SELL"})
    result = await f.check(PipelineContext(trade=trade))
    assert result.passed is True


async def test_entry_price_filter_disabled_at_100pct() -> None:
    """max=1.0 désactive le filtre (aucun prix > 1.0 possible sur Polymarket)."""
    f = EntryPriceFilter(_settings(strategy_max_entry_price=1.0))
    result = await f.check(PipelineContext(trade=_trade(price=0.999)))
    assert result.passed is True


# --- PositionSizer -----------------------------------------------------------


async def test_position_sizer_position_already_open(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Default settings = execution_mode="dry_run" → simulated=True attendu.
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xc",
                asset_id="123",
                size=1.0,
                avg_price=0.5,
                simulated=True,
            ),
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


# --- PositionSizer side-aware (M13 Bug 5) -----------------------------------


async def test_position_sizer_sell_matches_open_position(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SELL sur (cond, asset) matching → accepté, size cappée à existing.size."""
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xc",
                asset_id="123",
                size=10.0,
                avg_price=0.4,
                simulated=True,
            ),
        )
        await session.commit()
    f = PositionSizer(session_factory, _settings(copy_ratio=0.01))
    trade = _trade(price=0.6, size=1000.0)
    trade = DetectedTradeDTO(**{**trade.model_dump(), "side": "SELL"})
    ctx = PipelineContext(trade=trade)
    result = await f.check(ctx)
    assert result.passed is True
    # raw=1000*0.01=10 ; existing.size=10 ; min=10.0.
    assert ctx.my_size == pytest.approx(10.0)


async def test_position_sizer_sell_proportional_when_source_smaller(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SELL : raw_size < existing.size → proportional strict, pas capé."""
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xc",
                asset_id="123",
                size=10.0,
                avg_price=0.4,
                simulated=True,
            ),
        )
        await session.commit()
    f = PositionSizer(session_factory, _settings(copy_ratio=0.01))
    trade = _trade(price=0.6, size=500.0)
    trade = DetectedTradeDTO(**{**trade.model_dump(), "side": "SELL"})
    ctx = PipelineContext(trade=trade)
    result = await f.check(ctx)
    assert result.passed is True
    # raw=500*0.01=5 < existing.size=10 → prend raw proportional.
    assert ctx.my_size == pytest.approx(5.0)


async def test_position_sizer_sell_orphan_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SELL sans position matching → rejeté avec ``sell_without_position``."""
    f = PositionSizer(session_factory, _settings())
    trade = _trade(price=0.6, size=1000.0)
    trade = DetectedTradeDTO(**{**trade.model_dump(), "side": "SELL"})
    result = await f.check(PipelineContext(trade=trade))
    assert result.passed is False
    assert result.reason == "sell_without_position"


async def test_position_sizer_sell_wrong_asset_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SELL sur cond=X/asset_B alors qu'on a cond=X/asset_A ouvert → rejeté.

    Un SELL YES ne ferme pas une position NO (asset_id différent).
    Conservateur et sûr.
    """
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xc",
                asset_id="123",  # asset A
                size=10.0,
                avg_price=0.4,
                simulated=True,
            ),
        )
        await session.commit()
    f = PositionSizer(session_factory, _settings())
    trade = _trade(price=0.6, size=1000.0)
    trade = DetectedTradeDTO(**{**trade.model_dump(), "side": "SELL", "asset_id": "456"})
    result = await f.check(PipelineContext(trade=trade))
    assert result.passed is False
    assert result.reason == "sell_without_position"


# --- M16 MC.2 PositionSizer EV-aware post-fees -----------------------------
# Cf. spec docs/specs/M16-dynamic-fees-ev.md §9.2.


def _market_with_fee(fee_type: str = "crypto_fees_v2") -> MarketMetadata:
    """Helper : market avec feeType pour les tests M16."""
    return MarketMetadata(
        id="1",
        conditionId="0xc",
        active=True,
        closed=False,
        archived=False,
        acceptingOrders=True,
        enableOrderBook=True,
        liquidityClob=50000.0,
        endDate=datetime.now(tz=UTC) + timedelta(days=30),
        clobTokenIds='["123","456"]',
        outcomes='["Yes","No"]',
        feeType=fee_type,
        feesEnabled=True,
    )


def _make_fee_client(rate: str) -> Any:
    """Mock async FeeRateClient dont get_fee_rate retourne Decimal(rate)."""
    from decimal import Decimal

    client = AsyncMock()
    client.get_fee_rate = AsyncMock(return_value=Decimal(rate))
    return client


async def test_position_sizer_subtracts_fee_from_ev_happy_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """BUY YES @ 0.30 sur Crypto, EV-after-fee largement > seuil → PASS + ctx enrichi."""
    fee_client = _make_fee_client("0.10")
    f = PositionSizer(
        session_factory,
        _settings(max_position_usd=200.0, copy_ratio=0.01),
        fee_rate_client=fee_client,
    )
    ctx = PipelineContext(trade=_trade(price=0.30, size=100.0), market=_market_with_fee())
    result = await f.check(ctx)
    # raw_size = 100 * 0.01 = 1 ; cap = 200 / 0.30 = 666 ; min = 1.
    assert result.passed is True
    assert ctx.my_size == pytest.approx(1.0)
    # effective_rate = 0.25 × (0.30 × 0.70)^2 = 0.25 × 0.0441 = 0.011025 = 1.1025%
    assert ctx.fee_rate is not None
    assert ctx.fee_rate == pytest.approx(0.011025, abs=1e-6)
    # fee_cost = notional × rate = (1 × 0.30) × 0.011025 = 0.003308
    assert ctx.fee_cost_usd == pytest.approx(0.003308, abs=1e-5)
    # ev_after_fee = max_gain - fee = 1 × 0.70 - 0.003308 = 0.6967
    assert ctx.ev_after_fee_usd == pytest.approx(0.6967, abs=1e-3)
    # Vérifie qu'on a bien appelé le fee client
    fee_client.get_fee_rate.assert_awaited_once_with("123")


async def test_position_sizer_rejects_negative_ev_after_fee(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """BUY YES @ 0.97 sur Crypto : upside 3¢/share trop faible vs seuil 0.05$ → REJECT."""
    fee_client = _make_fee_client("0.10")
    f = PositionSizer(
        session_factory,
        _settings(max_position_usd=200.0, copy_ratio=0.01),
        fee_rate_client=fee_client,
    )
    ctx = PipelineContext(trade=_trade(price=0.97, size=1.0), market=_market_with_fee())
    result = await f.check(ctx)
    # raw_size = 0.01 ; expected_max_gain = 0.01 × 0.03 = 0.0003 < 0.05.
    assert result.passed is False
    assert result.reason == "ev_negative_after_fees"
    # ctx enrichi pour audit
    assert ctx.fee_rate is not None
    assert ctx.fee_cost_usd is not None
    assert ctx.ev_after_fee_usd is not None
    assert ctx.ev_after_fee_usd < 0.05


async def test_position_sizer_no_fee_client_preserves_behavior(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fee_rate_client=None → pas de fee math, ctx fee_* restent None."""
    f = PositionSizer(session_factory, _settings(max_position_usd=200.0, copy_ratio=0.01))
    ctx = PipelineContext(trade=_trade(price=0.97, size=1.0), market=_market_with_fee())
    result = await f.check(ctx)
    # Sans fee math, ce trade qui aurait été rejeté en M16 passe ici.
    assert result.passed is True
    assert ctx.fee_rate is None
    assert ctx.fee_cost_usd is None
    assert ctx.ev_after_fee_usd is None


async def test_position_sizer_flag_off_preserves_behavior(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fee_rate_client injecté MAIS flag off → fee_client jamais appelé."""
    fee_client = _make_fee_client("0.10")
    f = PositionSizer(
        session_factory,
        _settings(
            max_position_usd=200.0,
            copy_ratio=0.01,
            strategy_fees_aware_enabled=False,
        ),
        fee_rate_client=fee_client,
    )
    ctx = PipelineContext(trade=_trade(price=0.97, size=1.0), market=_market_with_fee())
    result = await f.check(ctx)
    assert result.passed is True
    assert ctx.fee_rate is None
    fee_client.get_fee_rate.assert_not_awaited()


async def test_position_sizer_buy_yes_vs_buy_no_ev_calculation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """BUY YES @ 0.40 et BUY NO @ 0.60 → effective_fee_rate identique (formule symétrique).

    `(p × (1-p))^exp` est invariant sous p → 1-p, donc la fee est la même
    pour 2 BUYs miroirs sur la même condition.
    """
    fee_client = _make_fee_client("0.10")
    f = PositionSizer(
        session_factory,
        _settings(max_position_usd=200.0, copy_ratio=0.01),
        fee_rate_client=fee_client,
    )
    # BUY YES @ 0.40 (asset 123)
    ctx_yes = PipelineContext(trade=_trade(price=0.40, size=10.0), market=_market_with_fee())
    await f.check(ctx_yes)

    # BUY NO @ 0.60 (asset 456 — sister token, miroir prob).
    trade_no = _trade(price=0.60, size=10.0)
    trade_no = DetectedTradeDTO(**{**trade_no.model_dump(), "asset_id": "456"})
    ctx_no = PipelineContext(trade=trade_no, market=_market_with_fee())
    await f.check(ctx_no)

    # Effective fee rate identique : 0.25 × (0.4 × 0.6)^2 = 0.25 × (0.6 × 0.4)^2
    assert ctx_yes.fee_rate == pytest.approx(ctx_no.fee_rate or 0, abs=1e-9)


async def test_position_sizer_fee_skipped_when_base_fee_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`base_fee=0` du endpoint = marché fee-free → court-circuit propre.

    On ne calcule pas la formule (qui appliquerait Crypto fallback même si
    le market n'a pas de fee enabled). Comportement strict M2..M15 préservé.
    """
    fee_client = _make_fee_client("0")  # marché fee-free
    f = PositionSizer(
        session_factory,
        _settings(max_position_usd=200.0, copy_ratio=0.01),
        fee_rate_client=fee_client,
    )
    # Trade qui aurait été REJECTED si fee crypto appliquée par erreur.
    ctx = PipelineContext(trade=_trade(price=0.97, size=1.0), market=_market_with_fee())
    result = await f.check(ctx)
    assert result.passed is True
    assert ctx.fee_rate == 0.0
    assert ctx.fee_cost_usd == 0.0
    fee_client.get_fee_rate.assert_awaited_once()


async def test_position_sizer_compute_effective_fee_rate_crypto() -> None:
    """Formule Crypto v2 : feeRate=0.25, exp=2 → max effective 1.5625% à p=0.5."""
    from decimal import Decimal

    rate = PositionSizer._compute_effective_fee_rate(
        price=Decimal("0.5"),
        market=_market_with_fee("crypto_fees_v2"),
    )
    assert rate == Decimal("0.015625")  # 0.25 × 0.0625 = 0.015625


async def test_position_sizer_compute_effective_fee_rate_sports_v2() -> None:
    """Formule Sports v2 (post-March 30 2026) : feeRate=0.03, exp=1 → 0.75% à p=0.5."""
    from decimal import Decimal

    rate = PositionSizer._compute_effective_fee_rate(
        price=Decimal("0.5"),
        market=_market_with_fee("sports_fees_v2"),
    )
    assert rate == Decimal("0.0075")  # 0.03 × 0.25 = 0.0075


async def test_position_sizer_compute_effective_fee_rate_unknown_uses_crypto_fallback() -> None:
    """fee_type inconnu → fallback Crypto (conservateur)."""
    from decimal import Decimal

    rate = PositionSizer._compute_effective_fee_rate(
        price=Decimal("0.5"),
        market=_market_with_fee("politics_fees_v_future"),
    )
    assert rate == Decimal("0.015625")  # même que Crypto


async def test_position_sizer_compute_effective_fee_rate_no_market_uses_crypto_fallback() -> None:
    """market=None → fallback Crypto."""
    from decimal import Decimal

    rate = PositionSizer._compute_effective_fee_rate(
        price=Decimal("0.5"),
        market=None,
    )
    assert rate == Decimal("0.015625")


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


# --- M17 MD.1 : ségrégation simulated ↔ live (audit C-001) ------------------


async def test_position_sizer_check_buy_filters_simulated_in_live_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Live mode ignore les positions virtuelles M13 traînantes."""
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xc",
                asset_id="123",
                size=1.0,
                avg_price=0.5,
                simulated=True,  # position virtuelle M13 traînante
            ),
        )
        await session.commit()
    f = PositionSizer(session_factory, _settings(execution_mode="live"))
    ctx = PipelineContext(trade=_trade())
    result = await f.check(ctx)
    # En live, la position virtuelle est invisible → check passe.
    assert result.passed is True


async def test_position_sizer_check_sell_filters_simulated_in_live_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Live mode ne voit pas une position virtuelle quand un SELL live arrive."""
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xc",
                asset_id="123",
                size=10.0,
                avg_price=0.4,
                simulated=True,
            ),
        )
        await session.commit()
    f = PositionSizer(session_factory, _settings(execution_mode="live"))
    trade = _trade(price=0.6, size=1000.0)
    trade = DetectedTradeDTO(**{**trade.model_dump(), "side": "SELL"})
    result = await f.check(PipelineContext(trade=trade))
    # En live, la position virtuelle n'existe pas pour le _check_sell.
    assert result.passed is False
    assert result.reason == "sell_without_position"


async def test_risk_manager_check_filters_simulated_in_live_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """L'exposition live n'inclut pas les positions virtuelles."""
    async with session_factory() as session:
        # 5 positions virtuelles à $100 chacune = $500 d'exposition virtuelle.
        for i in range(5):
            session.add(
                MyPosition(
                    condition_id=f"0xv{i}",
                    asset_id=str(i),
                    size=200.0,
                    avg_price=0.5,
                    simulated=True,
                ),
            )
        await session.commit()
    f = RiskManager(
        session_factory,
        _settings(execution_mode="live", risk_available_capital_usd_stub=200.0),
    )
    ctx = PipelineContext(trade=_trade())
    ctx.my_size = 100.0
    ctx.midpoint = 0.5  # cost = 50 USD
    result = await f.check(ctx)
    # En live l'exposition = 0, capital_dispo = 200, cost = 50 → pass.
    assert result.passed is True


async def test_dry_run_still_sees_only_virtual_positions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """En dry_run, les positions live ne polluent pas les checks pipeline."""
    async with session_factory() as session:
        # Position live (cas hypothétique : flip live → dry_run après cleanup
        # incomplet). MD.1 garantit que dry_run ne la voit pas.
        session.add(
            MyPosition(
                condition_id="0xc",
                asset_id="123",
                size=1.0,
                avg_price=0.5,
                simulated=False,  # position live
            ),
        )
        await session.commit()
    f = PositionSizer(session_factory, _settings(execution_mode="dry_run"))
    result = await f.check(PipelineContext(trade=_trade()))
    # En dry_run, la position live est invisible → check passe.
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
    # 6 filtres : TraderLifecycle, Market, EntryPrice (bug 4), PositionSizer,
    # SlippageChecker, RiskManager.
    assert len(ctx.filter_trace) == 6
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
    # Pipeline arrêté au MarketFilter — clob.get_midpoint jamais appelé.
    clob.get_midpoint.assert_not_called()
    # M5_bis Phase C.4 : TraderLifecycleFilter passe (EVICTION_ENABLED=false
    # fast path), MarketFilter rejette → 2 traces.
    assert len(ctx.filter_trace) == 2
    assert ctx.filter_trace[0]["filter"] == "TraderLifecycleFilter"
    assert ctx.filter_trace[0]["passed"] is True
    assert ctx.filter_trace[1]["filter"] == "MarketFilter"
    assert ctx.filter_trace[1]["passed"] is False


# --- M15 MB.6 : probation 0.25× sizing (4 tests §9.6) ----------------------


async def test_probation_wallet_sized_quarter_kelly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MB.6 §9.6 #17 — wallet probation → my_size *= 0.25 (default).

    Setup : `is_source_probation=True`, BUY size=100 @ 0.50, copy_ratio=0.01,
    max_position_usd=200 (cap_size = 200/0.50 = 400, raw_size = 100*0.01 =
    1.0 ≤ cap → raw_my_size = 1.0). M16 fees off.

    Expected : `ctx.my_size = 1.0 × 0.25 = 0.25`.
    """
    f = PositionSizer(
        session_factory,
        _settings(
            copy_ratio=0.01,
            max_position_usd=200.0,
            probation_size_multiplier="0.25",
            strategy_fees_aware_enabled=False,
        ),
    )
    ctx = PipelineContext(
        trade=_trade(price=0.50, size=100.0, is_source_probation=True),
    )
    result = await f.check(ctx)
    assert result.passed is True
    assert ctx.my_size == pytest.approx(0.25, abs=1e-6)


async def test_non_probation_wallet_sized_normally(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MB.6 — wallet non-probation → multiplier=1.0 (no-op).

    Régression check : un trade `is_source_probation=False` (default)
    ne doit PAS subir la division par 4.
    """
    f = PositionSizer(
        session_factory,
        _settings(
            copy_ratio=0.01,
            max_position_usd=200.0,
            probation_size_multiplier="0.25",
            strategy_fees_aware_enabled=False,
        ),
    )
    ctx = PipelineContext(
        trade=_trade(price=0.50, size=100.0, is_source_probation=False),
    )
    result = await f.check(ctx)
    assert result.passed is True
    assert ctx.my_size == pytest.approx(1.0, abs=1e-6)


@pytest.fixture
def alerts_queue():  # type: ignore[no-untyped-def]
    """Queue Alert minimaliste pour les tests MB.6."""
    import asyncio as _asyncio

    from polycopy.monitoring.dtos import Alert as _Alert

    return _asyncio.Queue[_Alert](maxsize=10)


async def test_probation_release_when_full_gate_satisfied(
    target_trader_repo,  # type: ignore[no-untyped-def]
    alerts_queue,  # type: ignore[no-untyped-def]
) -> None:
    """MB.6 §9.6 #19 — wallet probation passé full gate → flag flip à False.

    Setup : ACTIVE wallet ``is_probation=True``. On invoke
    ``decision_engine.decide(...)`` avec ``trade_count_90d=51`` et
    ``days_active=32`` (≥ probation_full_*).
    """
    from polycopy.config import Settings
    from polycopy.discovery.decision_engine import DecisionEngine
    from polycopy.discovery.dtos import ScoringResult, TraderMetrics

    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None,
        target_wallets="0xdummy",
        scoring_promotion_threshold=0.65,
        max_active_traders=10,
        probation_full_trades=50,
        probation_full_days=30,
    )
    await target_trader_repo.insert_shadow("0xprob", is_probation=True)
    await target_trader_repo.transition_status("0xprob", new_status="active")
    await target_trader_repo.update_score("0xprob", score=0.70, scoring_version="v2.1.1")

    engine = DecisionEngine(target_trader_repo, cfg, alerts_queue)
    scoring = ScoringResult(
        wallet_address="0xprob",
        score=0.70,
        scoring_version="v2.1.1",
        low_confidence=False,
        metrics=TraderMetrics(wallet_address="0xprob", fetched_at=datetime.now(tz=UTC)),
        cycle_at=datetime.now(tz=UTC),
    )
    cur = await target_trader_repo.get("0xprob")
    await engine.decide(
        scoring,
        cur,
        active_count=1,
        trade_count_90d=55,
        days_active=35,
    )
    after = await target_trader_repo.get("0xprob")
    assert after is not None
    assert after.is_probation is False  # Released by _maybe_release_probation


def test_walletpoller_resolver_propagates_probation_flag() -> None:
    """MB.6 §9.6 #20 — WalletPoller resolver propagate is_probation au DTO.

    Pure unit test : on builde un WalletPoller avec un resolver qui
    retourne True pour un wallet donné, et on vérifie que `_to_dto`
    set `is_source_probation=True` dans le DTO produit.
    """
    from unittest.mock import AsyncMock, MagicMock

    from polycopy.watcher.dtos import TradeActivity
    from polycopy.watcher.wallet_poller import WalletPoller

    # Resolver fixed → True pour le wallet test.
    def resolver(_wallet: str) -> bool:
        return True

    poller = WalletPoller(
        wallet_address="0xprob",
        client=MagicMock(),
        repo=AsyncMock(),
        interval_seconds=1,
        probation_resolver=resolver,
    )
    activity = TradeActivity(
        proxyWallet="0xprob",
        transactionHash="0xtx",
        timestamp=int(datetime.now(tz=UTC).timestamp()),
        conditionId="0xc",
        asset="0xa",
        side="BUY",
        size=1.0,
        price=0.5,
        usdcSize=0.5,
        type="TRADE",
        outcomeIndex=0,
        title=None,
        outcome=None,
        slug=None,
    )
    dto = poller._to_dto(activity, trade_id=None)
    assert dto.is_source_probation is True

    # Resolver False → flag False.
    poller_neg = WalletPoller(
        wallet_address="0xother",
        client=MagicMock(),
        repo=AsyncMock(),
        interval_seconds=1,
        probation_resolver=lambda _w: False,
    )
    dto_neg = poller_neg._to_dto(activity, trade_id=None)
    assert dto_neg.is_source_probation is False

    # Resolver absent (default None) → flag False (rétrocompat).
    poller_no_resolver = WalletPoller(
        wallet_address="0xany",
        client=MagicMock(),
        repo=AsyncMock(),
        interval_seconds=1,
    )
    dto_default = poller_no_resolver._to_dto(activity, trade_id=None)
    assert dto_default.is_source_probation is False


async def test_probation_candidate_inserted_with_flag(
    target_trader_repo,  # type: ignore[no-untyped-def]
    alerts_queue,  # type: ignore[no-untyped-def]
) -> None:
    """MB.6 §9.6 #20 — wallet absent + 10≤trades<50 + days≥7 → insert
    shadow avec ``is_probation=True``.
    """
    from polycopy.config import Settings
    from polycopy.discovery.decision_engine import DecisionEngine
    from polycopy.discovery.dtos import ScoringResult, TraderMetrics

    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None,
        target_wallets="0xdummy",
        scoring_promotion_threshold=0.50,
        max_active_traders=10,
        probation_min_trades=10,
        probation_full_trades=50,
        probation_min_days=7,
        probation_full_days=30,
    )
    engine = DecisionEngine(target_trader_repo, cfg, alerts_queue)
    scoring = ScoringResult(
        wallet_address="0xprob",
        score=0.65,
        scoring_version="v2.1.1",
        low_confidence=False,
        metrics=TraderMetrics(wallet_address="0xprob", fetched_at=datetime.now(tz=UTC)),
        cycle_at=datetime.now(tz=UTC),
    )
    decision = await engine.decide(
        scoring,
        None,  # absent
        active_count=0,
        trade_count_90d=25,  # in probation window [10, 50)
        days_active=10,  # ≥ probation_min_days=7
    )
    assert decision.decision == "discovered_shadow"
    assert decision.event_metadata.get("is_probation") is True
    inserted = await target_trader_repo.get("0xprob")
    assert inserted is not None
    assert inserted.is_probation is True
