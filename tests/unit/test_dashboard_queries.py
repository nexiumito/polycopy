"""Tests des fonctions ``queries.py`` du dashboard (M4.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.dashboard import queries
from polycopy.storage.dtos import (
    DetectedTradeDTO,
    PnlSnapshotDTO,
    StrategyDecisionDTO,
)
from polycopy.storage.models import MyOrder, MyPosition
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    MyPositionRepository,
    PnlSnapshotRepository,
    StrategyDecisionRepository,
    TargetTraderRepository,
)


def _trade(tx: str, wallet: str = "0xwallet", ts: datetime | None = None) -> DetectedTradeDTO:
    return DetectedTradeDTO(
        tx_hash=tx,
        target_wallet=wallet,
        condition_id="0xcond",
        asset_id="123",
        side="BUY",
        size=10.0,
        usdc_size=5.0,
        price=0.5,
        timestamp=ts or datetime.now(tz=UTC),
        outcome="Yes",
        slug="market-slug",
        raw_json={"tx_hash": tx},
    )


async def _insert_order(
    session_factory: async_sessionmaker[AsyncSession],
    tx: str,
    *,
    status: str,
) -> None:
    """Insère un ``MyOrder`` directement (tout statut accepté, contrairement au DTO)."""
    async with session_factory() as session:
        session.add(
            MyOrder(
                source_tx_hash=tx,
                condition_id="0xcond",
                asset_id="123",
                side="BUY",
                size=1.0,
                price=0.5,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status=status,
                simulated=True,
                transaction_hashes=[],
            ),
        )
        await session.commit()


def _pnl_dto(total: float, *, is_dry_run: bool = False, drawdown: float = 0.0) -> PnlSnapshotDTO:
    return PnlSnapshotDTO(
        total_usdc=total,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        drawdown_pct=drawdown,
        open_positions_count=0,
        cash_pnl_total=None,
        is_dry_run=is_dry_run,
    )


def test_parse_since_valid_tokens() -> None:
    assert queries.parse_since("1h") == timedelta(hours=1)
    assert queries.parse_since("24h") == timedelta(hours=24)
    assert queries.parse_since("7d") == timedelta(days=7)
    assert queries.parse_since("30d") == timedelta(days=30)


def test_parse_since_invalid_fallbacks_to_24h() -> None:
    assert queries.parse_since(None) == timedelta(hours=24)
    assert queries.parse_since("foo") == timedelta(hours=24)
    assert queries.parse_since("") == timedelta(hours=24)


@pytest.mark.asyncio
async def test_fetch_home_kpis_empty_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    kpis = await queries.fetch_home_kpis(session_factory)
    assert kpis.latest_total_usdc is None
    assert kpis.latest_drawdown_pct is None
    assert kpis.open_positions_count == 0
    assert kpis.detected_trades_24h == 0
    assert kpis.orders_24h_by_status == {}
    assert kpis.last_alert_event is None
    assert kpis.last_alert_at is None


@pytest.mark.asyncio
async def test_fetch_home_kpis_with_data(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    my_position_repo: MyPositionRepository,
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    await detected_trade_repo.insert_if_new(_trade("0xtx1"))
    await detected_trade_repo.insert_if_new(_trade("0xtx2"))
    await _insert_order(session_factory, "0xtx1", status="FILLED")
    await _insert_order(session_factory, "0xtx2", status="SIMULATED")
    await my_position_repo.upsert_on_fill("0xcond", "123", "BUY", 1.0, 0.5)
    await pnl_snapshot_repo.insert(_pnl_dto(100.0, drawdown=5.0))

    kpis = await queries.fetch_home_kpis(session_factory)
    assert kpis.latest_total_usdc == 100.0
    assert kpis.latest_drawdown_pct == 5.0
    assert kpis.open_positions_count == 1
    assert kpis.detected_trades_24h == 2
    assert kpis.orders_24h_by_status == {"FILLED": 1, "SIMULATED": 1}


@pytest.mark.asyncio
async def test_list_detected_trades_filter_and_paginate(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    now = datetime.now(tz=UTC)
    for i in range(5):
        await detected_trade_repo.insert_if_new(
            _trade(f"0xa{i}", wallet="0xAlice", ts=now + timedelta(seconds=i)),
        )
    await detected_trade_repo.insert_if_new(_trade("0xb1", wallet="0xBob"))

    all_trades = await queries.list_detected_trades(session_factory)
    assert len(all_trades) == 6

    alice_trades = await queries.list_detected_trades(session_factory, wallet="0xAlice")
    assert len(alice_trades) == 5
    # ordre desc par timestamp
    assert alice_trades[0].tx_hash == "0xa4"

    page = await queries.list_detected_trades(session_factory, limit=2, offset=1)
    assert len(page) == 2


@pytest.mark.asyncio
async def test_list_detected_trades_clamps_limit(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    for i in range(3):
        await detected_trade_repo.insert_if_new(_trade(f"0xx{i}"))
    # limit=500 doit être clampé à 200 en interne — on vérifie via no-crash + résultat correct.
    trades = await queries.list_detected_trades(session_factory, limit=500)
    assert len(trades) == 3


@pytest.mark.asyncio
async def test_list_strategy_decisions_filter(
    session_factory: async_sessionmaker[AsyncSession],
    strategy_decision_repo: StrategyDecisionRepository,
) -> None:
    await strategy_decision_repo.insert(
        StrategyDecisionDTO(
            detected_trade_id=1,
            tx_hash="0xa1",
            decision="APPROVED",
            my_size=1.0,
            my_price=0.5,
            pipeline_state={},
        ),
    )
    await strategy_decision_repo.insert(
        StrategyDecisionDTO(
            detected_trade_id=2,
            tx_hash="0xa2",
            decision="REJECTED",
            reason="slippage_exceeded",
            pipeline_state={},
        ),
    )
    approved = await queries.list_strategy_decisions(session_factory, decision="APPROVED")
    assert len(approved) == 1
    assert approved[0].tx_hash == "0xa1"
    rejected = await queries.list_strategy_decisions(session_factory, decision="REJECTED")
    assert len(rejected) == 1
    assert rejected[0].reason == "slippage_exceeded"


@pytest.mark.asyncio
async def test_count_strategy_reasons(
    session_factory: async_sessionmaker[AsyncSession],
    strategy_decision_repo: StrategyDecisionRepository,
) -> None:
    for reason in ("slippage_exceeded", "slippage_exceeded", "low_liquidity"):
        await strategy_decision_repo.insert(
            StrategyDecisionDTO(
                detected_trade_id=0,
                tx_hash="0x" + reason,
                decision="REJECTED",
                reason=reason,
                pipeline_state={},
            ),
        )
    counts = await queries.count_strategy_reasons(session_factory)
    assert counts == {"slippage_exceeded": 2, "low_liquidity": 1}


@pytest.mark.asyncio
async def test_list_orders_filter_invalid_status_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_order(session_factory, "0xtxA", status="FILLED")
    await _insert_order(session_factory, "0xtxB", status="SIMULATED")
    filled = await queries.list_orders(session_factory, status="FILLED")
    assert len(filled) == 1
    # Status inconnu → ignoré, retourne tout.
    unknown = await queries.list_orders(session_factory, status="BOGUS")
    assert len(unknown) == 2


@pytest.mark.asyncio
async def test_list_positions_filter_state(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_on_fill("0xcondA", "1", "BUY", 1.0, 0.5)
    await my_position_repo.upsert_on_fill("0xcondB", "2", "BUY", 1.0, 0.5)
    # close cond B
    await my_position_repo.upsert_on_fill("0xcondB", "2", "SELL", 1.0, 0.6)

    opened = await queries.list_positions(session_factory, state="open")
    assert len(opened) == 1
    closed = await queries.list_positions(session_factory, state="closed")
    assert len(closed) == 1
    all_rows = await queries.list_positions(session_factory)
    assert len(all_rows) == 2


@pytest.mark.asyncio
async def test_list_positions_enriches_rows_with_invested_payoff_and_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    """PositionRow porte usdc_invested, payoff_max et outcome_label joint."""
    # Seed un trade détecté qui fournit l'outcome "Yes" sur (cond, asset).
    await detected_trade_repo.insert_if_new(_trade("0xsrc1"))
    # Crée la position correspondante (mêmes condition_id + asset_id).
    await my_position_repo.upsert_on_fill("0xcond", "123", "BUY", 3.0, 0.4)

    rows = await queries.list_positions(session_factory, state="open")
    assert len(rows) == 1
    row = rows[0]
    assert row.size == pytest.approx(3.0)
    assert row.avg_price == pytest.approx(0.4)
    assert row.usdc_invested == pytest.approx(1.2)
    assert row.payoff_max == pytest.approx(3.0)
    assert row.outcome_label == "Yes"
    assert row.closed_at is None


@pytest.mark.asyncio
async def test_list_positions_outcome_label_none_when_no_detected_trade(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """Sans DetectedTrade joignable, outcome_label reste None (pas un crash)."""
    await my_position_repo.upsert_on_fill("0xorphan", "42", "BUY", 1.0, 0.5)
    rows = await queries.list_positions(session_factory)
    assert len(rows) == 1
    assert rows[0].outcome_label is None


@pytest.mark.asyncio
async def test_fetch_pnl_series_excludes_dry_run_by_default(
    session_factory: async_sessionmaker[AsyncSession],
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    await pnl_snapshot_repo.insert(_pnl_dto(10.0, is_dry_run=False))
    await pnl_snapshot_repo.insert(_pnl_dto(99.0, is_dry_run=True))
    await pnl_snapshot_repo.insert(_pnl_dto(20.0, is_dry_run=False))

    real = await queries.fetch_pnl_series(session_factory, since=timedelta(hours=1))
    assert real.total_usdc == [10.0, 20.0]
    everything = await queries.fetch_pnl_series(
        session_factory,
        since=timedelta(hours=1),
        include_dry_run=True,
    )
    assert sorted(everything.total_usdc) == [10.0, 20.0, 99.0]


@pytest.mark.asyncio
async def test_fetch_pnl_series_respects_since_window(
    session_factory: async_sessionmaker[AsyncSession],
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    # Un snapshot frais + un truqué ancien (via mutation manuelle).
    recent = await pnl_snapshot_repo.insert(_pnl_dto(50.0))
    old = await pnl_snapshot_repo.insert(_pnl_dto(99.0))

    async with session_factory() as session:
        # On utilise session.merge pour backdater le "vieux" snapshot.
        old.timestamp = datetime.now(tz=UTC) - timedelta(days=10)
        await session.merge(old)
        await session.commit()

    series = await queries.fetch_pnl_series(
        session_factory,
        since=timedelta(hours=1),
    )
    assert series.total_usdc == [50.0]
    # Vérifie qu'on retrouve le vieux en 30d.
    series_wide = await queries.fetch_pnl_series(
        session_factory,
        since=timedelta(days=30),
    )
    assert sorted(series_wide.total_usdc) == [50.0, 99.0]
    # Silence unused var
    _ = recent


@pytest.mark.asyncio
async def test_get_home_alltime_stats_empty_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DB vide → tous les champs valident leur None/0 defensif."""
    stats = await queries.get_home_alltime_stats(session_factory)
    assert stats.realized_pnl_total == 0.0
    assert stats.volume_usd_total == 0.0
    assert stats.fills_count == 0
    assert stats.fills_rate_pct is None
    assert stats.strategy_approve_rate_pct is None
    assert stats.top_trader is None
    assert stats.uptime is None


def test_normalize_home_pnl_mode_fallback() -> None:
    assert queries.normalize_home_pnl_mode(None) == "both"
    assert queries.normalize_home_pnl_mode("") == "both"
    assert queries.normalize_home_pnl_mode("bogus") == "both"
    assert queries.normalize_home_pnl_mode("real") == "real"
    assert queries.normalize_home_pnl_mode("dry_run") == "dry_run"
    assert queries.normalize_home_pnl_mode("both") == "both"


@pytest.mark.asyncio
async def test_get_home_alltime_stats_with_seed(
    session_factory: async_sessionmaker[AsyncSession],
    strategy_decision_repo: StrategyDecisionRepository,
    pnl_snapshot_repo: PnlSnapshotRepository,
    target_trader_repo: TargetTraderRepository,
) -> None:
    """Commit 5 : chaque champ agrégé correctement à partir de données seedées."""
    # 2 FILLED + 1 REJECTED → fills_count=2, fills_rate=66.67%.
    await _insert_order(session_factory, "0xa1", status="FILLED")
    await _insert_order(session_factory, "0xa2", status="FILLED")
    await _insert_order(session_factory, "0xa3", status="REJECTED")

    # 2 APPROVED + 1 REJECTED → approve_rate=66.67%.
    for i, decision in enumerate(["APPROVED", "APPROVED", "REJECTED"]):
        await strategy_decision_repo.insert(
            StrategyDecisionDTO(
                detected_trade_id=i,
                tx_hash=f"0xs{i}",
                decision=decision,  # type: ignore[arg-type]
                reason=None if decision == "APPROVED" else "slippage",
                pipeline_state={},
            ),
        )

    # 1 active trader avec score 0.82.
    trader = await target_trader_repo.insert_shadow("0xtop", label="topcat")
    await target_trader_repo.update_score(
        "0xtop",
        score=0.82,
        scoring_version="v1",
    )
    await target_trader_repo.transition_status("0xtop", new_status="active")

    # 1 snapshot PnL (ancien) → uptime > 0.
    await pnl_snapshot_repo.insert(_pnl_dto(100.0))

    # 1 position dry-run fermée avec realized_pnl = +2.5.
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xcondX",
                asset_id="9",
                size=0.0,
                avg_price=0.4,
                opened_at=datetime.now(tz=UTC) - timedelta(days=1),
                closed_at=datetime.now(tz=UTC),
                simulated=True,
                realized_pnl=2.5,
            ),
        )
        await session.commit()

    stats = await queries.get_home_alltime_stats(session_factory)
    assert stats.fills_count == 2
    assert stats.fills_rate_pct == pytest.approx(2 / 3 * 100.0, abs=0.01)
    # volume = 2 fills × (size=1.0 × price=0.5) = 1.0.
    assert stats.volume_usd_total == pytest.approx(1.0)
    assert stats.strategy_approve_rate_pct == pytest.approx(2 / 3 * 100.0, abs=0.01)
    assert stats.realized_pnl_total == pytest.approx(2.5)
    assert stats.top_trader is not None
    assert stats.top_trader["wallet_address"] == "0xtop"
    assert stats.top_trader["label"] == "topcat"
    assert stats.top_trader["score"] == pytest.approx(0.82)
    assert stats.uptime is not None
    assert stats.uptime.total_seconds() >= 0

    # Silence unused var.
    _ = trader


@pytest.mark.asyncio
async def test_get_home_alltime_stats_pnl_mode_filters(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """pnl_mode=real|dry_run|both segmente correctement le realized_pnl_total."""
    # 1 dry-run fermée +2.5 (realized_pnl dénormalisé).
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xdry",
                asset_id="1",
                size=0.0,
                avg_price=0.4,
                opened_at=datetime.now(tz=UTC) - timedelta(days=1),
                closed_at=datetime.now(tz=UTC),
                simulated=True,
                realized_pnl=2.5,
            ),
        )
        await session.commit()

    # 1 live fermée : BUY 10 @ 0.30 → SELL 10 @ 0.50 → +2.0.
    await my_position_repo.upsert_on_fill("0xlive", "2", "BUY", 10.0, 0.30)
    await my_position_repo.upsert_on_fill("0xlive", "2", "SELL", 10.0, 0.50)
    async with session_factory() as session:
        session.add(
            MyOrder(
                source_tx_hash="0xsrc-live",
                condition_id="0xlive",
                asset_id="2",
                side="BUY",
                size=10.0,
                price=0.30,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="FILLED",
                simulated=False,
                transaction_hashes=[],
            ),
        )
        session.add(
            MyOrder(
                source_tx_hash="0xsrc-live",
                condition_id="0xlive",
                asset_id="2",
                side="SELL",
                size=10.0,
                price=0.50,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="FILLED",
                simulated=False,
                transaction_hashes=[],
            ),
        )
        await session.commit()

    both = await queries.get_home_alltime_stats(session_factory, pnl_mode="both")
    assert both.realized_pnl_total == pytest.approx(2.5 + 2.0)

    only_real = await queries.get_home_alltime_stats(session_factory, pnl_mode="real")
    assert only_real.realized_pnl_total == pytest.approx(2.0)

    only_dry_run = await queries.get_home_alltime_stats(session_factory, pnl_mode="dry_run")
    assert only_dry_run.realized_pnl_total == pytest.approx(2.5)

    # Les champs mode-agnostiques restent identiques entre modes.
    assert both.volume_usd_total == only_real.volume_usd_total == only_dry_run.volume_usd_total
