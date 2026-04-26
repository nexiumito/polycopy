"""Tests M10 §3.3 + §8.3 — parité dry-run ↔ live du kill switch.

M10 inverse l'invariant M4/M8. Ces tests vérifient que :
- Kill switch fire en SIMULATION.
- Kill switch fire en DRY_RUN.
- Drawdown warning 75 % fire en DRY_RUN.
- L'event legacy ``dry_run_virtual_drawdown`` n'est plus émis nulle part.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.dtos import WalletState
from polycopy.executor.virtual_wallet_reader import VirtualWalletStateReader
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.pnl_writer import PnlSnapshotWriter
from polycopy.storage.dtos import PnlSnapshotDTO
from polycopy.storage.repositories import PnlSnapshotRepository


def _settings(mode: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        execution_mode=mode,  # type: ignore[arg-type]
        polymarket_private_key="0x" + "1" * 64 if mode == "live" else None,
        polymarket_funder="0xF" if mode == "live" else None,
        pnl_snapshot_interval_seconds=1,
        kill_switch_drawdown_pct=20.0,
    )


def _reader(total: float) -> AsyncMock:
    reader = AsyncMock(spec=VirtualWalletStateReader)
    reader.get_state.return_value = WalletState(
        total_position_value_usd=0.0,
        available_capital_usd=total,
        open_positions_count=0,
    )
    return reader


async def _run_once(writer: PnlSnapshotWriter, timeout: float = 0.3) -> None:  # noqa: ASYNC109
    stop = asyncio.Event()

    async def _stop_later() -> None:
        await asyncio.sleep(timeout)
        stop.set()

    await asyncio.gather(writer.run(stop), _stop_later())


async def _seed_max_ever(
    repo: PnlSnapshotRepository,
    *,
    is_dry_run: bool,
    execution_mode: str | None = None,
) -> None:
    """Seed un snapshot baseline.

    M17 MD.3 : ``execution_mode`` permet de seed un mode tri-state strict
    (``simulation`` / ``dry_run`` / ``live``). Si non fourni, dérive de
    ``is_dry_run`` (rétrocompat).
    """
    await repo.insert(
        PnlSnapshotDTO(
            total_usdc=1000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=0.0,
            open_positions_count=0,
            cash_pnl_total=None,
            is_dry_run=is_dry_run,
            execution_mode=execution_mode,  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_kill_switch_fires_in_dry_run_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = PnlSnapshotRepository(session_factory)
    await _seed_max_ever(repo, is_dry_run=True)
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings("dry_run"),
        _reader(total=400.0),  # drawdown 60% ≥ 20%
        queue,
    )
    await _run_once(writer)
    events = {a.event for a in _drain(queue)}
    assert "kill_switch_triggered" in events


@pytest.mark.asyncio
async def test_kill_switch_fires_in_simulation_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """M17 MD.3 : SIMULATION lit son propre bucket (plus de pollution SIM+DRY).

    Avant MD.3, SIMULATION agrégeait avec DRY_RUN via ``is_dry_run=True`` —
    ce qui causait le bug C-003 (un backtest SIM à $50k polluait la baseline
    DRY_RUN à $1k → faux-positif kill switch). Post-MD.3, chaque mode lit
    sa propre baseline strict.
    """
    repo = PnlSnapshotRepository(session_factory)
    await _seed_max_ever(repo, is_dry_run=True, execution_mode="simulation")
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings("simulation"),
        _reader(total=400.0),
        queue,
    )
    await _run_once(writer)
    events = {a.event for a in _drain(queue)}
    assert "kill_switch_triggered" in events


@pytest.mark.asyncio
async def test_drawdown_warning_fires_in_dry_run_mode(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seuil 20 %, drawdown 18 % ⇒ WARNING pnl_snapshot_drawdown, pas de kill."""
    repo = PnlSnapshotRepository(session_factory)
    await _seed_max_ever(repo, is_dry_run=True)
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings("dry_run"),
        _reader(total=820.0),  # drawdown 18 %
        queue,
    )
    await _run_once(writer)
    events = {a.event for a in _drain(queue)}
    assert "pnl_snapshot_drawdown" in events
    assert "kill_switch_triggered" not in events


@pytest.mark.asyncio
async def test_no_dry_run_virtual_drawdown_event_emitted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-régression M10 : l'event legacy INFO est supprimé, ne doit jamais sortir."""
    repo = PnlSnapshotRepository(session_factory)
    await _seed_max_ever(repo, is_dry_run=True)
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings("dry_run"),
        _reader(total=400.0),  # drawdown 60 %, legacy aurait émis INFO en plus
        queue,
    )
    await _run_once(writer)
    events = [a.event for a in _drain(queue)]
    assert "dry_run_virtual_drawdown" not in events


def _drain(q: asyncio.Queue[Alert]) -> list[Alert]:
    out: list[Alert] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out
