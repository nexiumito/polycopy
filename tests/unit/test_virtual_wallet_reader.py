"""Tests M8 §9.5 — ``VirtualWalletStateReader``.

Couvre :
- 0 position virtuelle → total = virtual_capital, exposure=0, unrealized=0.
- 2 positions ouvertes + midpoints mockés → unrealized correct.
- 1 position close avec realized_pnl → somme dans total_usdc.
- midpoint 404 / exception → log warning + skip position.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.virtual_wallet_reader import VirtualWalletStateReader
from polycopy.storage.repositories import MyPositionRepository
from polycopy.strategy.clob_read_client import ClobReadClient


def _settings(virtual_capital: float = 1000.0) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=True,
        dry_run_realistic_fill=True,
        dry_run_virtual_capital_usd=virtual_capital,
    )


async def test_no_positions_returns_capital_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clob_read = AsyncMock(spec=ClobReadClient)
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings(2000.0))
    state = await reader.get_state()
    assert state.open_positions_count == 0
    assert state.total_position_value_usd == 0.0
    assert state.available_capital_usd == 2000.0


async def test_open_positions_unrealized_via_midpoint(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    await my_position_repo.upsert_virtual(
        condition_id="0xb",
        asset_id="B",
        side="BUY",
        size_filled=20.0,
        fill_price=0.5,
    )
    clob_read = AsyncMock(spec=ClobReadClient)
    clob_read.get_midpoint.side_effect = lambda asset_id: {
        "A": 0.5,  # +0.10 unrealized vs avg 0.4 × 10 = +1.0
        "B": 0.45,  # -0.05 unrealized vs avg 0.5 × 20 = -1.0
    }[asset_id]
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    state = await reader.get_state()
    # exposure = 10*0.5 + 20*0.45 = 5 + 9 = 14
    assert state.total_position_value_usd == pytest.approx(14.0, abs=1e-9)
    # total = capital + realized (0) + unrealized (0)
    total = state.total_position_value_usd + state.available_capital_usd
    assert total == pytest.approx(1000.0, abs=1e-9)


async def test_realized_pnl_added_to_total(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    pos = await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    assert pos is not None
    await my_position_repo.close_virtual(pos.id, closed_at=datetime.now(tz=UTC), realized_pnl=15.0)
    clob_read = AsyncMock(spec=ClobReadClient)
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    state = await reader.get_state()
    total = state.total_position_value_usd + state.available_capital_usd
    assert total == pytest.approx(1015.0, abs=1e-9)


async def test_midpoint_failure_skips_position_no_crash(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    clob_read = AsyncMock(spec=ClobReadClient)
    clob_read.get_midpoint.side_effect = httpx.HTTPError("boom")
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    state = await reader.get_state()
    assert state.total_position_value_usd == 0.0
    # Capital intact (capital + skipped position)
    assert state.available_capital_usd == 1000.0
