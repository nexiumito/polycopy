"""Tests M8 §9.6 — ``DryRunResolutionWatcher``."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.dry_run_resolution_watcher import DryRunResolutionWatcher
from polycopy.storage.repositories import MyPositionRepository
from polycopy.strategy.dtos import MarketMetadata
from polycopy.strategy.gamma_client import GammaApiClient


def _settings(*, m8: bool = True, neg_risk_resolution: bool = True) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=True,
        dry_run_realistic_fill=m8,
        dry_run_resolution_poll_minutes=5,
        dry_run_neg_risk_resolution_enabled=neg_risk_resolution,
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


async def test_neg_risk_market_skipped_when_flag_off(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """M13 M8 v2 opt-out : flag=false préserve le comportement M8 v1."""
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
    watcher = DryRunResolutionWatcher(
        session_factory,
        gamma,
        _settings(neg_risk_resolution=False),
    )
    await watcher._run_once()
    assert len(await my_position_repo.list_open_virtual()) == 1


async def test_neg_risk_yes_wins_resolves_with_positive_pnl(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """M13 M8 v2 happy path : neg_risk + flag=true + YES gagne → close + alert."""
    await my_position_repo.upsert_virtual(
        condition_id="0xN",
        asset_id="Y",
        side="BUY",
        size_filled=10.0,
        fill_price=0.25,
    )
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_markets_by_condition_ids.return_value = [
        _market(
            condition_id="0xN",
            closed=True,
            neg_risk=True,
            outcome_prices='["1.0","0.0"]',
            clob_token_ids='["Y","N"]',
        ),
    ]
    alerts_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=16)
    watcher = DryRunResolutionWatcher(
        session_factory,
        gamma,
        _settings(),
        alerts_queue=alerts_queue,
    )
    await watcher._run_once()

    assert await my_position_repo.list_open_virtual() == []
    pnl = await my_position_repo.sum_realized_pnl_virtual()
    # payout=1.0, avg=0.25, size=10 → (1.0 - 0.25) * 10 = 7.5
    assert pnl == pytest.approx(7.5, abs=1e-9)
    # Alerte émise pour la résolution neg_risk.
    assert alerts_queue.qsize() == 1
    alert = alerts_queue.get_nowait()
    assert alert.event == "dry_run_market_resolved_neg_risk"
    assert alert.level == "INFO"
    assert "7.50" in alert.body or "+7.50" in alert.body


async def test_neg_risk_no_wins_resolves_with_negative_pnl(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """M13 M8 v2 losing candidate : neg_risk + flag=true + NO gagne → close + PnL négatif."""
    # Position sur YES mais NO gagne → perte totale.
    await my_position_repo.upsert_virtual(
        condition_id="0xN2",
        asset_id="Y",
        side="BUY",
        size_filled=10.0,
        fill_price=0.25,
    )
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_markets_by_condition_ids.return_value = [
        _market(
            condition_id="0xN2",
            closed=True,
            neg_risk=True,
            outcome_prices='["0.0","1.0"]',
            clob_token_ids='["Y","N"]',
        ),
    ]
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings())
    await watcher._run_once()
    pnl = await my_position_repo.sum_realized_pnl_virtual()
    # payout=0.0, avg=0.25, size=10 → (0.0 - 0.25) * 10 = -2.5
    assert pnl == pytest.approx(-2.5, abs=1e-9)


async def test_neg_risk_prices_not_converged_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """M13 : neg_risk closed mais outcome_prices pas encore matérialisés → skip."""
    await my_position_repo.upsert_virtual(
        condition_id="0xN3",
        asset_id="Y",
        side="BUY",
        size_filled=10.0,
        fill_price=0.25,
    )
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_markets_by_condition_ids.return_value = [
        _market(
            condition_id="0xN3",
            closed=True,
            neg_risk=True,
            outcome_prices='["0.95","0.05"]',
        ),
    ]
    watcher = DryRunResolutionWatcher(session_factory, gamma, _settings())
    await watcher._run_once()
    # Position reste ouverte, le watcher retentera au prochain cycle.
    assert len(await my_position_repo.list_open_virtual()) == 1


async def test_close_virtual_idempotent_when_already_closed(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """M13 §14.3 : race SELL copié vs resolver → close_virtual idempotent.

    Si la position est déjà fermée (par un SELL copié arrivé entre-temps),
    ``close_virtual`` log + skip au lieu d'écraser le PnL.
    """
    from datetime import timedelta

    pos = await my_position_repo.upsert_virtual(
        condition_id="0xC",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    # Simule le SELL qui a déjà fermé avec un PnL concret.
    first_closed_at = datetime.now(tz=UTC) - timedelta(minutes=5)
    await my_position_repo.close_virtual(
        pos.id,
        closed_at=first_closed_at,
        realized_pnl=3.0,
    )
    # Le resolver arrive ensuite et tente d'écraser.
    later_closed_at = datetime.now(tz=UTC)
    await my_position_repo.close_virtual(
        pos.id,
        closed_at=later_closed_at,
        realized_pnl=99.0,
    )
    # Le PnL du SELL prime.
    pnl = await my_position_repo.sum_realized_pnl_virtual()
    assert pnl == pytest.approx(3.0, abs=1e-9)


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
