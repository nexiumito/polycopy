"""Tests des queries agrégées du daily summary (M7 §9.8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.monitoring.daily_summary_queries import (
    _orders_stats_since,
    collect_daily_summary_context,
)
from polycopy.storage.dtos import (
    DetectedTradeDTO,
    MyOrderDTO,
    StrategyDecisionDTO,
    TraderEventDTO,
)
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    MyOrderRepository,
    MyPositionRepository,
    PnlSnapshotRepository,
    StrategyDecisionRepository,
    TargetTraderRepository,
    TraderEventRepository,
)


def _settings() -> Settings:
    return Settings(_env_file=None, discovery_enabled=True)  # type: ignore[call-arg]


def _trade(
    wallet: str,
    timestamp: datetime,
    size: float = 10.0,
    price: float = 0.5,
) -> DetectedTradeDTO:
    return DetectedTradeDTO(
        tx_hash="0x" + uuid4().hex,
        target_wallet=wallet,
        condition_id="0x" + "a" * 64,
        asset_id="asset",
        side="BUY",
        size=size,
        usdc_size=size * price,
        price=price,
        timestamp=timestamp,
        outcome="Yes",
        slug="some-market",
        raw_json={"k": "v"},
    )


@pytest.mark.asyncio
async def test_empty_db_returns_zeros(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    since = datetime.now(tz=UTC) - timedelta(hours=24)
    ctx = await collect_daily_summary_context(session_factory, _settings(), since)
    assert ctx.trades_24h == 0
    assert ctx.decisions_approved == 0
    assert ctx.decisions_rejected == 0
    assert ctx.orders_sent == 0
    assert ctx.top_wallets == []
    assert ctx.total_usdc is None
    assert ctx.alerts_total_24h == 0
    assert ctx.positions_open == 0


@pytest.mark.asyncio
async def test_trades_and_top_wallets(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    target_trader_repo: TargetTraderRepository,
) -> None:
    wallet_a = "0x" + "a" * 40
    wallet_b = "0x" + "b" * 40
    await target_trader_repo.upsert(wallet_a, label="Alpha")
    await target_trader_repo.upsert(wallet_b, label="Beta")
    now = datetime.now(tz=UTC)
    since = now - timedelta(hours=24)

    for _ in range(3):
        await detected_trade_repo.insert_if_new(_trade(wallet_a, now - timedelta(hours=1)))
    for _ in range(2):
        await detected_trade_repo.insert_if_new(_trade(wallet_b, now - timedelta(hours=2)))
    # Trade hors fenêtre
    await detected_trade_repo.insert_if_new(_trade(wallet_a, now - timedelta(hours=30)))

    ctx = await collect_daily_summary_context(session_factory, _settings(), since)
    assert ctx.trades_24h == 5
    assert len(ctx.top_wallets) == 2
    assert ctx.top_wallets[0].trade_count == 3
    assert ctx.top_wallets[0].label == "Alpha"


@pytest.mark.asyncio
async def test_decisions_and_top_reason(
    session_factory: async_sessionmaker[AsyncSession],
    strategy_decision_repo: StrategyDecisionRepository,
) -> None:
    # 2 approved + 3 rejected (dont 2 pour "slippage")
    await strategy_decision_repo.insert(
        StrategyDecisionDTO(
            detected_trade_id=1,
            tx_hash="0x1",
            decision="APPROVED",
            reason=None,
            my_size=1.0,
            my_price=0.5,
            slippage_pct=0.0,
            pipeline_state={},
        ),
    )
    await strategy_decision_repo.insert(
        StrategyDecisionDTO(
            detected_trade_id=2,
            tx_hash="0x2",
            decision="APPROVED",
            reason=None,
            my_size=1.0,
            my_price=0.5,
            slippage_pct=0.0,
            pipeline_state={},
        ),
    )
    for i in range(2):
        await strategy_decision_repo.insert(
            StrategyDecisionDTO(
                detected_trade_id=100 + i,
                tx_hash=f"0x10{i}",
                decision="REJECTED",
                reason="slippage",
                my_size=None,
                my_price=None,
                slippage_pct=3.0,
                pipeline_state={},
            ),
        )
    await strategy_decision_repo.insert(
        StrategyDecisionDTO(
            detected_trade_id=200,
            tx_hash="0x200",
            decision="REJECTED",
            reason="liquidity",
            my_size=None,
            my_price=None,
            slippage_pct=None,
            pipeline_state={},
        ),
    )
    since = datetime.now(tz=UTC) - timedelta(hours=24)
    ctx = await collect_daily_summary_context(session_factory, _settings(), since)
    assert ctx.decisions_approved == 2
    assert ctx.decisions_rejected == 3
    assert ctx.top_reject_reason == "slippage"


@pytest.mark.asyncio
async def test_orders_stats_counts_and_volume(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
) -> None:
    base: dict[str, Any] = dict(
        source_tx_hash="0x" + "1" * 64,
        clob_order_id="0x" + "2" * 64,
        condition_id="0x" + "3" * 64,
        asset_id="asset",
        side="BUY",
        size=10.0,
        price=0.5,
        tick_size=0.01,
        neg_risk=False,
        order_type="FOK",
        status="SENT",
        simulated=False,
    )
    # 1 SIMULATED, 1 FILLED (via update_status), 1 REJECTED
    simulated = await my_order_repo.insert(MyOrderDTO(**{**base, "status": "SIMULATED"}))
    filled = await my_order_repo.insert(
        MyOrderDTO(**{**base, "source_tx_hash": "0x1" + "a" * 63}),
    )
    rejected = await my_order_repo.insert(
        MyOrderDTO(**{**base, "source_tx_hash": "0x1" + "b" * 63}),
    )
    await my_order_repo.update_status(filled.id, "FILLED")
    await my_order_repo.update_status(rejected.id, "REJECTED")
    _ = simulated  # just to reference and avoid unused

    since = datetime.now(tz=UTC) - timedelta(hours=24)
    ctx = await collect_daily_summary_context(session_factory, _settings(), since)
    assert ctx.orders_sent == 2  # SIMULATED + FILLED
    assert ctx.orders_filled == 1
    assert ctx.orders_rejected == 1
    assert ctx.volume_executed_usd == pytest.approx(5.0)  # 10*0.5 pour FILLED


@pytest.mark.asyncio
async def test_pnl_and_drawdown(
    session_factory: async_sessionmaker[AsyncSession],
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    # un snapshot ancien (hier) + un récent
    async with session_factory() as session:
        from polycopy.storage.models import PnlSnapshot

        session.add(
            PnlSnapshot(
                timestamp=datetime.now(tz=UTC) - timedelta(hours=25),
                total_usdc=1000.0,
                drawdown_pct=0.0,
                open_positions_count=0,
                is_dry_run=False,
            ),
        )
        session.add(
            PnlSnapshot(
                timestamp=datetime.now(tz=UTC) - timedelta(hours=1),
                total_usdc=1100.0,
                drawdown_pct=3.5,
                open_positions_count=2,
                is_dry_run=False,
            ),
        )
        await session.commit()
    since = datetime.now(tz=UTC) - timedelta(hours=24)
    ctx = await collect_daily_summary_context(session_factory, _settings(), since)
    assert ctx.total_usdc == pytest.approx(1100.0)
    assert ctx.delta_24h_pct == pytest.approx(10.0)
    assert ctx.drawdown_24h_pct == pytest.approx(3.5)


@pytest.mark.asyncio
async def test_discovery_events_counted(
    session_factory: async_sessionmaker[AsyncSession],
    trader_event_repo: TraderEventRepository,
) -> None:
    wallet = "0x" + "1" * 40
    await trader_event_repo.insert(
        TraderEventDTO(
            wallet_address=wallet,
            event_type="promoted_active",
            from_status="shadow",
            to_status="active",
            score_at_event=0.7,
            scoring_version="v1",
            reason=None,
            event_metadata=None,
        ),
    )
    await trader_event_repo.insert(
        TraderEventDTO(
            wallet_address=wallet,
            event_type="demoted_paused",
            from_status="active",
            to_status="paused",
            score_at_event=0.3,
            scoring_version="v1",
            reason=None,
            event_metadata=None,
        ),
    )
    await trader_event_repo.insert(
        TraderEventDTO(
            wallet_address=wallet,
            event_type="skipped_cap",
            from_status=None,
            to_status=None,
            score_at_event=None,
            scoring_version="v1",
            reason="cap",
            event_metadata=None,
        ),
    )
    since = datetime.now(tz=UTC) - timedelta(hours=24)
    ctx = await collect_daily_summary_context(session_factory, _settings(), since)
    assert ctx.discovery_promotions_24h == 1
    assert ctx.discovery_demotions_24h == 1
    assert ctx.discovery_cap_reached_24h == 1


@pytest.mark.asyncio
async def test_alerts_counts_from_memory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    since = datetime.now(tz=UTC) - timedelta(hours=24)
    ctx = await collect_daily_summary_context(
        session_factory,
        _settings(),
        since,
        alerts_counts={"order_filled_large": 5, "pnl_snapshot_drawdown": 2},
    )
    assert ctx.alerts_total_24h == 7
    assert "filled:5" in ctx.alerts_by_type_compact
    assert "drawdown:2" in ctx.alerts_by_type_compact


@pytest.mark.asyncio
async def test_orders_stats_dry_run_counts_simulated(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
) -> None:
    """M13 Bug 7 : en dry_run, SIMULATED est traité comme fill et contribue au volume."""
    base: dict[str, Any] = dict(
        source_tx_hash="0x" + "1" * 64,
        clob_order_id="0x" + "2" * 64,
        condition_id="0x" + "3" * 64,
        asset_id="asset",
        side="BUY",
        size=10.0,
        price=0.5,
        tick_size=0.01,
        neg_risk=False,
        order_type="FOK",
        status="SIMULATED",
        simulated=True,
    )
    # 3 SIMULATED (3 × 10 × 0.5 = 15) + 1 REJECTED.
    for i in range(3):
        await my_order_repo.insert(
            MyOrderDTO(**{**base, "source_tx_hash": f"0x{i:064x}"}),
        )
    rejected = await my_order_repo.insert(
        MyOrderDTO(**{**base, "source_tx_hash": "0x" + "b" * 64, "status": "SENT"}),
    )
    await my_order_repo.update_status(rejected.id, "REJECTED")

    dry_run_settings = Settings(_env_file=None, execution_mode="dry_run")  # type: ignore[call-arg]
    since = datetime(1970, 1, 1, tzinfo=UTC)
    sent, filled, rejected_count, volume = await _orders_stats_since(
        session_factory,
        since,
        dry_run_settings,
    )
    assert filled == 3
    assert rejected_count == 1
    assert volume == pytest.approx(15.0)
    # sent = SENT + FILLED + PARTIALLY_FILLED + SIMULATED = 3 SIMULATED.
    assert sent == 3


@pytest.mark.asyncio
async def test_orders_stats_live_counts_filled(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
) -> None:
    """M13 Bug 7 régression guard : en live, SIMULATED n'entre pas dans filled/volume."""
    base: dict[str, Any] = dict(
        source_tx_hash="0x" + "1" * 64,
        clob_order_id="0x" + "2" * 64,
        condition_id="0x" + "3" * 64,
        asset_id="asset",
        side="BUY",
        size=10.0,
        price=0.5,
        tick_size=0.01,
        neg_risk=False,
        order_type="FOK",
        status="SENT",
        simulated=False,
    )
    # 2 FILLED + 1 SIMULATED.
    filled_a = await my_order_repo.insert(
        MyOrderDTO(**{**base, "source_tx_hash": "0x" + "a" * 64}),
    )
    filled_b = await my_order_repo.insert(
        MyOrderDTO(**{**base, "source_tx_hash": "0x" + "b" * 64}),
    )
    await my_order_repo.insert(
        MyOrderDTO(
            **{**base, "source_tx_hash": "0x" + "c" * 64, "status": "SIMULATED", "simulated": True},
        ),
    )
    await my_order_repo.update_status(filled_a.id, "FILLED")
    await my_order_repo.update_status(filled_b.id, "FILLED")

    live_settings = Settings(_env_file=None, execution_mode="live")  # type: ignore[call-arg]
    since = datetime(1970, 1, 1, tzinfo=UTC)
    _sent, filled, _rejected, volume = await _orders_stats_since(
        session_factory,
        since,
        live_settings,
    )
    assert filled == 2
    assert volume == pytest.approx(10.0)  # 2 × 10 × 0.5 — le SIMULATED est exclu.


@pytest.mark.asyncio
async def test_positions_open_and_value(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,  # noqa: ARG001
) -> None:
    async with session_factory() as session:
        from polycopy.storage.models import MyPosition

        session.add(
            MyPosition(
                condition_id="0x" + "a" * 64,
                asset_id="asset-1",
                size=100.0,
                avg_price=0.5,
                closed_at=None,
            ),
        )
        session.add(
            MyPosition(
                condition_id="0x" + "a" * 64,
                asset_id="asset-2",
                size=50.0,
                avg_price=0.3,
                closed_at=datetime.now(tz=UTC),  # closed → exclu
            ),
        )
        await session.commit()
    since = datetime.now(tz=UTC) - timedelta(hours=24)
    ctx = await collect_daily_summary_context(session_factory, _settings(), since)
    assert ctx.positions_open == 1
    assert ctx.positions_value_usd == pytest.approx(50.0)
