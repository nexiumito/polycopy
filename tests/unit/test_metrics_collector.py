"""Tests MetricsCollector (agrégation positions + activity)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from polycopy.config import Settings
from polycopy.discovery.dtos import RawPosition
from polycopy.discovery.metrics_collector import MetricsCollector


def _settings() -> Settings:
    return Settings(
        target_wallets="0xdummy",
        scoring_lookback_days=90,
    )


def _make_collector(
    positions: list[RawPosition],
    activity: list[dict[str, Any]],
) -> MetricsCollector:
    api = AsyncMock()
    api.get_positions = AsyncMock(return_value=positions)
    api.get_activity_trades = AsyncMock(return_value=activity)
    return MetricsCollector(api, _settings())


async def test_wallet_without_trades_has_hhi_one_and_zero_volume() -> None:
    mc = _make_collector([], [])
    m = await mc.collect("0xa")
    assert m.total_volume_usd == 0.0
    assert m.herfindahl_index == 1.0
    assert m.win_rate == 0.0
    assert m.nb_distinct_markets == 0


async def test_diversity_balanced_across_markets() -> None:
    activity = [
        {"type": "TRADE", "conditionId": "A", "size": "100", "price": "0.5"},
        {"type": "TRADE", "conditionId": "B", "size": "100", "price": "0.5"},
        {"type": "TRADE", "conditionId": "C", "size": "100", "price": "0.5"},
        {"type": "TRADE", "conditionId": "D", "size": "100", "price": "0.5"},
    ]
    mc = _make_collector([], activity)
    m = await mc.collect("0xa")
    # 4 marchés équilibrés → HHI = 4 × (1/4)² = 0.25
    assert m.herfindahl_index == pytest.approx(0.25)
    assert m.total_volume_usd == pytest.approx(200.0)
    assert m.nb_distinct_markets == 4


async def test_single_market_herfindahl_is_one() -> None:
    activity = [
        {"type": "TRADE", "conditionId": "A", "size": "1", "price": "0.5"},
        {"type": "TRADE", "conditionId": "A", "size": "2", "price": "0.3"},
    ]
    mc = _make_collector([], activity)
    m = await mc.collect("0xa")
    assert m.herfindahl_index == pytest.approx(1.0)
    assert m.nb_distinct_markets == 1


async def test_win_rate_counts_cash_pnl_positive() -> None:
    positions = [
        RawPosition(
            conditionId="A",
            asset="1",
            size=10,
            avgPrice=0.5,
            initialValue=100.0,
            currentValue=0.0,
            cashPnl=50.0,
            realizedPnl=50.0,
            totalBought=100.0,
            redeemable=False,
        ),
        RawPosition(
            conditionId="B",
            asset="2",
            size=10,
            avgPrice=0.5,
            initialValue=100.0,
            currentValue=0.0,
            cashPnl=-30.0,
            realizedPnl=-30.0,
            totalBought=100.0,
            redeemable=False,
        ),
        RawPosition(
            conditionId="C",
            asset="3",
            size=10,
            avgPrice=0.5,
            initialValue=100.0,
            currentValue=0.0,
            cashPnl=10.0,
            realizedPnl=10.0,
            totalBought=100.0,
            redeemable=False,
        ),
    ]
    mc = _make_collector(positions, [])
    m = await mc.collect("0xa")
    assert m.resolved_positions_count == 3
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.realized_roi == pytest.approx(30.0 / 300.0)


async def test_resolved_vs_open_positions_split() -> None:
    positions = [
        RawPosition(  # résolue (redeemable)
            conditionId="A",
            asset="1",
            size=10,
            avgPrice=0.5,
            initialValue=100.0,
            currentValue=0.0,
            cashPnl=5.0,
            realizedPnl=5.0,
            totalBought=100.0,
            redeemable=True,
        ),
        RawPosition(  # ouverte
            conditionId="B",
            asset="2",
            size=10,
            avgPrice=0.5,
            initialValue=100.0,
            currentValue=120.0,
            cashPnl=0.0,
            realizedPnl=0.0,
            totalBought=100.0,
            redeemable=False,
        ),
    ]
    mc = _make_collector(positions, [])
    m = await mc.collect("0xa")
    assert m.resolved_positions_count == 1
    assert m.open_positions_count == 1
    assert m.largest_position_value_usd == pytest.approx(120.0)


async def test_metrics_from_real_positions_fixture(
    sample_positions: list[dict[str, Any]],
) -> None:
    """Smoke sur fixture M3 réelle : les calculs ne crashent pas."""
    raw_positions = [RawPosition.model_validate(p) for p in sample_positions]
    mc = _make_collector(raw_positions, [])
    m = await mc.collect("0xa")
    assert m.resolved_positions_count >= 0
    assert m.open_positions_count >= 0
    assert 0.0 <= m.win_rate <= 1.0
    assert m.fetched_at.tzinfo is not None
