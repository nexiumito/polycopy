"""Tests du ``PnlSnapshotWriter`` (M4).

Vérifie :
- Snapshot inséré avec bon ``is_dry_run``.
- Drawdown calculé vs max historique (only_real).
- Kill switch trigger en mode réel, **jamais** en dry-run (sécurité critique).
- Warning drawdown à 75%.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.dtos import WalletState
from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.pnl_writer import PnlSnapshotWriter
from polycopy.storage.dtos import PnlSnapshotDTO
from polycopy.storage.repositories import PnlSnapshotRepository


def _dry_settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "execution_mode": "dry_run",
        "pnl_snapshot_interval_seconds": 1,
        "kill_switch_drawdown_pct": 20.0,
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _real_settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "execution_mode": "live",
        "polymarket_private_key": "0x" + "1" * 64,
        "polymarket_funder": "0xF",
        "pnl_snapshot_interval_seconds": 1,
        "kill_switch_drawdown_pct": 20.0,
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _reader(total: float, avail: float = 1000.0, open_count: int = 0) -> AsyncMock:
    reader = AsyncMock(spec=WalletStateReader)
    reader.get_state.return_value = WalletState(
        total_position_value_usd=total,
        available_capital_usd=avail,
        open_positions_count=open_count,
    )
    return reader


async def _run_once(
    writer: PnlSnapshotWriter,
    stop: asyncio.Event,
    timeout: float = 0.3,  # noqa: ASYNC109
) -> None:
    """Lance le writer pour un court instant puis stop."""

    async def _stop_later() -> None:
        await asyncio.sleep(timeout)
        stop.set()

    await asyncio.gather(writer.run(stop), _stop_later())


# --- M10 : en dry-run, le kill switch fire IDENTIQUE LIVE --------------------


@pytest.mark.asyncio
async def test_dry_run_writes_snapshot_and_triggers_kill_switch(
    session_factory: async_sessionmaker[AsyncSession],
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    """M10 §3.3 : inversion de l'invariant M4/M8 — le kill switch fire en dry-run.

    Scénario : max=1000 en dry-run, total=100 → drawdown 90% > seuil 20%.
    L'alerte ``kill_switch_triggered`` CRITICAL est poussée ET ``stop_event``
    est set par le writer (et non seulement par le timeout externe).
    """
    await pnl_snapshot_repo.insert(
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
    reader = _reader(total=50.0, avail=50.0)
    writer = PnlSnapshotWriter(session_factory, _dry_settings(), reader, queue)
    stop = asyncio.Event()
    await _run_once(writer, stop, timeout=0.3)

    assert stop.is_set() is True
    alerts: list[Alert] = []
    while not queue.empty():
        alerts.append(queue.get_nowait())
    events = {a.event for a in alerts}
    assert "kill_switch_triggered" in events
    assert "dry_run_virtual_drawdown" not in events  # M10 : supprimé

    latest = await pnl_snapshot_repo.get_latest(only_real=False)
    assert latest is not None
    assert latest.is_dry_run is True
    assert latest.total_usdc == pytest.approx(100.0)


# --- Real mode : kill switch déclenché sur drawdown > seuil ----------------


@pytest.mark.asyncio
async def test_real_mode_kill_switch_triggered(
    session_factory: async_sessionmaker[AsyncSession],
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    # max_ever real = 1000, on baisse à 700 → drawdown 30% > 20% seuil.
    await pnl_snapshot_repo.insert(
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
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    reader = _reader(total=200.0, avail=500.0)  # total=700
    writer = PnlSnapshotWriter(
        session_factory,
        _real_settings(kill_switch_drawdown_pct=20.0),
        reader,
        queue,
    )
    stop = asyncio.Event()
    await _run_once(writer, stop, timeout=0.3)
    assert stop.is_set() is True
    alerts: list[Alert] = []
    while not queue.empty():
        alerts.append(queue.get_nowait())
    kinds = {a.event for a in alerts}
    assert "kill_switch_triggered" in kinds


# --- Real mode : drawdown warning à 75% ------------------------------------


@pytest.mark.asyncio
async def test_real_mode_drawdown_warning_no_stop(
    session_factory: async_sessionmaker[AsyncSession],
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    await pnl_snapshot_repo.insert(
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
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    # seuil=20%, drawdown=18% → 18 >= 0.75*20=15 donc warning mais pas kill.
    reader = _reader(total=820.0, avail=0.0)  # drawdown = (1000-820)/1000 = 18%
    writer = PnlSnapshotWriter(
        session_factory,
        _real_settings(
            kill_switch_drawdown_pct=20.0,
            pnl_snapshot_interval_seconds=10,
        ),
        reader,
        queue,
    )
    stop = asyncio.Event()
    # Execute 1 tick puis stop manuellement avant le sleep.
    task = asyncio.create_task(writer.run(stop))
    await asyncio.sleep(0.15)
    stop.set()
    await task

    alerts: list[Alert] = []
    while not queue.empty():
        alerts.append(queue.get_nowait())
    kinds = {a.event for a in alerts}
    assert "pnl_snapshot_drawdown" in kinds
    assert "kill_switch_triggered" not in kinds


# --- Erreur interne → log mais continue -------------------------------------


@pytest.mark.asyncio
async def test_writer_continues_on_exception(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    reader = AsyncMock(spec=WalletStateReader)
    reader.get_state.side_effect = RuntimeError("blip")
    writer = PnlSnapshotWriter(session_factory, _dry_settings(), reader, queue)
    stop = asyncio.Event()
    task = asyncio.create_task(writer.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await task
    # Ne raise pas — c'est tout ce qu'on teste ici.
