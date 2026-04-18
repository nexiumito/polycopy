"""Tests M8 §9.9 — ``PnlSnapshotWriter`` mode dry-run virtuel.

Couvre :
- Snapshot inséré avec is_dry_run=True quand dry_run.
- Kill switch **JAMAIS** déclenché en dry-run, même drawdown élevé (M4 invariant).
- Alerte ``dry_run_virtual_drawdown`` INFO si drawdown ≥ 50% × seuil.
- En mode réel, comportement M4 inchangé (kill switch + warning).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.dtos import WalletState
from polycopy.executor.virtual_wallet_reader import VirtualWalletStateReader
from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.pnl_writer import PnlSnapshotWriter
from polycopy.storage.dtos import PnlSnapshotDTO
from polycopy.storage.repositories import PnlSnapshotRepository


def _settings(*, dry: bool, m8: bool = True) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=dry,
        polymarket_private_key=None if dry else "0x" + "1" * 64,
        polymarket_funder=None if dry else "0xF",
        pnl_snapshot_interval_seconds=1,
        kill_switch_drawdown_pct=20.0,
        dry_run_realistic_fill=m8,
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
async def test_dry_run_snapshot_marks_is_dry_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(dry=True),
        _virtual_reader(total=1000.0, exposure=400.0, n=2),
        queue,
    )
    await _run_once(writer)
    repo = PnlSnapshotRepository(session_factory)
    latest = await repo.get_latest(only_real=False)
    assert latest is not None
    assert latest.is_dry_run is True


@pytest.mark.asyncio
async def test_dry_run_severe_drawdown_does_not_trigger_kill_switch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = PnlSnapshotRepository(session_factory)
    # Pré-remplit un snapshot dry-run à 1000 → max_ever=1000.
    await repo.insert(
        PnlSnapshotDTO(
            total_usdc=1000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=0.0,
            open_positions_count=0,
            cash_pnl_total=None,
            is_dry_run=True,
        ),
    )
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(dry=True),
        _virtual_reader(total=400.0, exposure=300.0, n=1),
        queue,
    )
    await _run_once(writer)
    # On vérifie qu'AUCUN kill_switch_triggered n'a été émis.
    events: list[str] = []
    while not queue.empty():
        events.append(queue.get_nowait().event)
    assert "kill_switch_triggered" not in events
    # Mais un dry_run_virtual_drawdown INFO peut être présent (drawdown 60%).
    assert "dry_run_virtual_drawdown" in events


@pytest.mark.asyncio
async def test_dry_run_low_drawdown_no_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = PnlSnapshotRepository(session_factory)
    await repo.insert(
        PnlSnapshotDTO(
            total_usdc=1000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=0.0,
            open_positions_count=0,
            cash_pnl_total=None,
            is_dry_run=True,
        ),
    )
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(dry=True),
        _virtual_reader(total=950.0, exposure=200.0, n=1),  # drawdown 5%
        queue,
    )
    await _run_once(writer)
    assert queue.empty()


@pytest.mark.asyncio
async def test_real_mode_still_triggers_kill_switch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-régression M4 : en live, kill switch agit comme avant."""
    repo = PnlSnapshotRepository(session_factory)
    await repo.insert(
        PnlSnapshotDTO(
            total_usdc=1000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=0.0,
            open_positions_count=0,
            cash_pnl_total=None,
            is_dry_run=False,
        ),
    )
    real_reader = AsyncMock(spec=WalletStateReader)
    real_reader.get_state.return_value = WalletState(
        total_position_value_usd=300.0,
        available_capital_usd=400.0,  # total=700, drawdown=30%
        open_positions_count=1,
    )
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(dry=False),
        real_reader,
        queue,
    )
    await _run_once(writer)
    events: list[str] = []
    while not queue.empty():
        events.append(queue.get_nowait().event)
    assert "kill_switch_triggered" in events
