"""Tests M17 MD.6 — `PnlSnapshotDTO.realized_pnl + unrealized_pnl` peuplés.

Avant M17 : les deux colonnes hardcodées à ``0.0`` dans le writer →
graphes ``/pnl`` plats (audit H-002) + divergence /home ↔ /performance
(audit C-005 effet). MD.6 alimente le DTO avec
``MyPosition.realized_pnl`` agrégé par mode.

Convergence garantie : la card ``/home`` PnL latent calcule inline
``total - initial - realized`` ; le snapshot ``pnl_snapshots`` le calcule
identiquement (même formule, même source de réalité). Écart < 1 cent
attendu.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.dtos import WalletState
from polycopy.executor.virtual_wallet_reader import VirtualWalletStateReader
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.pnl_writer import PnlSnapshotWriter
from polycopy.storage.models import MyPosition
from polycopy.storage.repositories import PnlSnapshotRepository


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        execution_mode="dry_run",
        pnl_snapshot_interval_seconds=1,
        kill_switch_drawdown_pct=20.0,
        dry_run_realistic_fill=True,
        dry_run_initial_capital_usd=1000.0,
    )


def _virtual_reader(total: float, exposure: float, n: int) -> AsyncMock:
    reader = AsyncMock(spec=VirtualWalletStateReader)
    reader.get_state.return_value = WalletState(
        total_position_value_usd=exposure,
        available_capital_usd=total - exposure,
        open_positions_count=n,
    )
    return reader


async def _run_once(writer: PnlSnapshotWriter, timeout: float = 0.2) -> None:  # noqa: ASYNC109
    stop = asyncio.Event()

    async def _stop_later() -> None:
        await asyncio.sleep(timeout)
        stop.set()

    await asyncio.gather(writer.run(stop), _stop_later())


@pytest.mark.asyncio
async def test_pnl_snapshot_populates_realized_pnl_nonzero_when_positions_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Une position virtuelle close avec realized_pnl=15 → snapshot reflète."""
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xa",
                asset_id="A",
                size=10.0,
                avg_price=0.4,
                simulated=True,
                closed_at=datetime.now(tz=UTC),
                realized_pnl=15.0,
            ),
        )
        await session.commit()
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _virtual_reader(total=1015.0, exposure=0.0, n=0),
        queue,
    )
    await _run_once(writer)
    repo = PnlSnapshotRepository(session_factory)
    latest = await repo.get_latest(only_real=False)
    assert latest is not None
    assert latest.realized_pnl == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_pnl_snapshot_unrealized_matches_formula(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """unrealized = total - initial - realized_cumulative (cohérent /home).

    Setup : initial = 1000, realized cumulé = 15, total live = 1050
    → unrealized = 1050 - 1000 - 15 = 35.
    """
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xa",
                asset_id="A",
                size=10.0,
                avg_price=0.4,
                simulated=True,
                closed_at=datetime.now(tz=UTC),
                realized_pnl=15.0,
            ),
        )
        await session.commit()
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _virtual_reader(total=1050.0, exposure=200.0, n=1),
        queue,
    )
    await _run_once(writer)
    repo = PnlSnapshotRepository(session_factory)
    latest = await repo.get_latest(only_real=False)
    assert latest is not None
    assert latest.realized_pnl == pytest.approx(15.0)
    assert latest.unrealized_pnl == pytest.approx(35.0)


@pytest.mark.asyncio
async def test_pnl_snapshot_realized_filtered_by_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """En dry_run, seules les positions virtuelles fermées sont sommées.

    Setup : 1 virtuelle (15) + 1 live (50) closed → snapshot dry_run
    n'affiche que les 15 virtuels.
    """
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xa",
                asset_id="A",
                size=10.0,
                avg_price=0.4,
                simulated=True,
                closed_at=datetime.now(tz=UTC),
                realized_pnl=15.0,
            ),
        )
        session.add(
            MyPosition(
                condition_id="0xb",
                asset_id="B",
                size=5.0,
                avg_price=0.3,
                simulated=False,
                closed_at=datetime.now(tz=UTC),
                realized_pnl=50.0,
            ),
        )
        await session.commit()
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _virtual_reader(total=1015.0, exposure=0.0, n=0),
        queue,
    )
    await _run_once(writer)
    repo = PnlSnapshotRepository(session_factory)
    latest = await repo.get_latest(only_real=False)
    assert latest is not None
    # Le snapshot dry_run agrège uniquement les positions simulated=True.
    assert latest.realized_pnl == pytest.approx(15.0)
