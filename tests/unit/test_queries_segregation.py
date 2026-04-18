"""Tests M8 §9.11 — ségrégation real / dry_run dans queries dashboard."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.dashboard import queries
from polycopy.storage.dtos import PnlSnapshotDTO
from polycopy.storage.repositories import MyPositionRepository, PnlSnapshotRepository


@pytest.fixture
async def seeded(
    session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    repo = PnlSnapshotRepository(session_factory)
    for is_dry, total in [(False, 1000.0), (False, 1010.0), (True, 950.0), (True, 970.0)]:
        await repo.insert(
            PnlSnapshotDTO(
                total_usdc=total,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                drawdown_pct=0.0,
                open_positions_count=0,
                cash_pnl_total=None,
                is_dry_run=is_dry,
            ),
        )
    return session_factory


async def test_mode_real_returns_only_real(
    seeded: async_sessionmaker[AsyncSession],
) -> None:
    series = await queries.fetch_pnl_series(
        seeded,
        since=timedelta(days=1),
        mode="real",
    )
    assert series.total_usdc == [1000.0, 1010.0]


async def test_mode_dry_run_returns_only_dry(
    seeded: async_sessionmaker[AsyncSession],
) -> None:
    series = await queries.fetch_pnl_series(
        seeded,
        since=timedelta(days=1),
        mode="dry_run",
    )
    assert series.total_usdc == [950.0, 970.0]


async def test_mode_both_returns_all(
    seeded: async_sessionmaker[AsyncSession],
) -> None:
    series = await queries.fetch_pnl_series(
        seeded,
        since=timedelta(days=1),
        mode="both",
    )
    assert sorted(series.total_usdc) == [950.0, 970.0, 1000.0, 1010.0]


async def test_invalid_mode_falls_back_to_default_real(
    seeded: async_sessionmaker[AsyncSession],
) -> None:
    series = await queries.fetch_pnl_series(
        seeded,
        since=timedelta(days=1),
        mode="bogus",
    )
    assert series.total_usdc == [1000.0, 1010.0]


async def test_position_repo_list_open_excludes_virtual(
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_on_fill(
        condition_id="0xR",
        asset_id="A",
        side="BUY",
        size_filled=5.0,
        fill_price=0.5,
    )
    await my_position_repo.upsert_virtual(
        condition_id="0xV",
        asset_id="B",
        side="BUY",
        size_filled=10.0,
        fill_price=0.3,
    )
    open_real = await my_position_repo.list_open()
    open_virtual = await my_position_repo.list_open_virtual()
    assert {p.condition_id for p in open_real} == {"0xR"}
    assert {p.condition_id for p in open_virtual} == {"0xV"}
