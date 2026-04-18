"""Tests M8 §9.6 — ``DryRunResolutionWatcher``."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.dry_run_resolution_watcher import DryRunResolutionWatcher
from polycopy.storage.repositories import MyPositionRepository
from polycopy.strategy.dtos import MarketMetadata
from polycopy.strategy.gamma_client import GammaApiClient


def _settings(*, m8: bool = True) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=True,
        dry_run_realistic_fill=m8,
        dry_run_resolution_poll_minutes=5,
    )


def _market(
    *,
    condition_id: str,
    closed: bool,
    neg_risk: bool = False,
    outcomes: str = '["Yes","No"]',
    outcome_prices: str = '["1","0"]',
    clob_token_ids: str = '["A","B"]',
) -> MarketMetadata:
    return MarketMetadata(
        id="x",
        conditionId=condition_id,
        active=True,
        closed=closed,
        archived=False,
        clobTokenIds=clob_token_ids,
        outcomes=outcomes,
        outcomePrices=outcome_prices,
        negRisk=neg_risk,
    )


async def test_no_open_positions_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings())
    await watcher._run_once()
    gamma.get_markets_by_condition_ids.assert_not_called()


async def test_open_market_not_closed_keeps_position(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_virtual(
        condition_id="0xC",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_markets_by_condition_ids.return_value = [
        _market(condition_id="0xC", closed=False),
    ]
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings())
    await watcher._run_once()
    assert len(await my_position_repo.list_open_virtual()) == 1


async def test_winning_outcome_closes_with_positive_pnl(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_virtual(
        condition_id="0xC",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_markets_by_condition_ids.return_value = [
        _market(
            condition_id="0xC",
            closed=True,
            outcome_prices='["1","0"]',
            clob_token_ids='["A","B"]',
        ),
    ]
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings())
    await watcher._run_once()
    open_v = await my_position_repo.list_open_virtual()
    assert open_v == []
    pnl = await my_position_repo.sum_realized_pnl_virtual()
    # payout 1.0, avg 0.4, size 10 → (1.0 - 0.4)*10 = 6.0
    assert pnl == pytest.approx(6.0, abs=1e-9)


async def test_losing_outcome_closes_with_negative_pnl(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    # Position sur le token PERDANT B
    await my_position_repo.upsert_virtual(
        condition_id="0xC",
        asset_id="B",
        side="BUY",
        size_filled=10.0,
        fill_price=0.6,
    )
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_markets_by_condition_ids.return_value = [
        _market(
            condition_id="0xC",
            closed=True,
            outcome_prices='["1","0"]',
            clob_token_ids='["A","B"]',
        ),
    ]
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings())
    await watcher._run_once()
    pnl = await my_position_repo.sum_realized_pnl_virtual()
    # payout 0.0, avg 0.6, size 10 → (0 - 0.6) * 10 = -6.0
    assert pnl == pytest.approx(-6.0, abs=1e-9)


async def test_neg_risk_market_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_virtual(
        condition_id="0xC",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_markets_by_condition_ids.return_value = [
        _market(condition_id="0xC", closed=True, neg_risk=True),
    ]
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings())
    await watcher._run_once()
    assert len(await my_position_repo.list_open_virtual()) == 1


async def test_winning_outcome_unknown_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """Si outcome_prices ne dépasse pas 0.99 → skip."""
    await my_position_repo.upsert_virtual(
        condition_id="0xC",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_markets_by_condition_ids.return_value = [
        _market(
            condition_id="0xC",
            closed=True,
            outcome_prices='["0.6","0.4"]',
        ),
    ]
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings())
    await watcher._run_once()
    assert len(await my_position_repo.list_open_virtual()) == 1


async def test_gamma_exception_does_not_kill_loop(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_virtual(
        condition_id="0xC",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_markets_by_condition_ids.side_effect = RuntimeError("boom")
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings())
    stop = asyncio.Event()

    # On démarre la boucle, on stop juste après le 1er cycle
    async def _stop_quickly() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(watcher.run_forever(stop), _stop_quickly())
    # Position toujours ouverte (cycle a échoué)
    assert len(await my_position_repo.list_open_virtual()) == 1


async def test_run_forever_no_op_when_m8_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gamma = AsyncMock(spec=GammaApiClient)
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings(m8=False))
    stop = asyncio.Event()
    # Doit retourner immédiatement sans même set le stop
    await asyncio.wait_for(watcher.run_forever(stop), timeout=0.5)
    gamma.get_markets_by_condition_ids.assert_not_called()
