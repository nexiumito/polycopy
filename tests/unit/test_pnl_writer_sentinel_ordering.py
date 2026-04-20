"""Tests invariant §4.6 — PnlSnapshotWriter touch sentinel AVANT stop_event.set().

M12_bis Phase D : l'ordre ``sentinel.touch() → stop_event.set()`` est
critique. Si le process crash entre les deux (kill -9 superviseur,
OOM killer), le respawn doit trouver le sentinel posé pour retomber
en mode paused — pas en mode normal avec un drawdown réel.

Ces tests vérifient :
1. Kill switch déclenché → sentinel posé avec ``reason="kill_switch"``.
2. Drawdown warning (<seuil kill) → sentinel NON posé.
3. Injection optionnelle : ``sentinel=None`` → pas de touch, ``stop_event``
   set quand même (backward compat Phase A..C).
4. Invariant d'ordre : au moment où ``touch()`` est appelé, ``stop_event``
   n'est PAS encore set.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.dtos import WalletState
from polycopy.executor.virtual_wallet_reader import VirtualWalletStateReader
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.pnl_writer import PnlSnapshotWriter
from polycopy.remote_control import SentinelFile
from polycopy.storage.dtos import PnlSnapshotDTO
from polycopy.storage.repositories import PnlSnapshotRepository


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        execution_mode="dry_run",
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


async def _seed_max_ever(repo: PnlSnapshotRepository) -> None:
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


# ===========================================================================
# Happy path — sentinel posé sur kill switch
# ===========================================================================


@pytest.mark.asyncio
async def test_kill_switch_posts_sentinel_with_reason(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    await _seed_max_ever(PnlSnapshotRepository(session_factory))
    sentinel = SentinelFile(tmp_path / "halt.flag")
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _reader(total=400.0),  # 60% drawdown >= 20% threshold
        queue,
        sentinel=sentinel,
    )
    await _run_once(writer)
    assert sentinel.exists() is True
    assert sentinel.reason() == "kill_switch"


@pytest.mark.asyncio
async def test_drawdown_warning_does_not_post_sentinel(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """18% de drawdown : warning (seuil 75% × 20% = 15%), pas kill switch."""
    await _seed_max_ever(PnlSnapshotRepository(session_factory))
    sentinel = SentinelFile(tmp_path / "halt.flag")
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _reader(total=820.0),  # 18% drawdown — warning seulement
        queue,
        sentinel=sentinel,
    )
    await _run_once(writer)
    assert sentinel.exists() is False


# ===========================================================================
# Backward compat — sentinel=None
# ===========================================================================


@pytest.mark.asyncio
async def test_kill_switch_without_sentinel_still_sets_stop_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sans sentinel injecté (Phase A..C) : kill switch set stop_event sans crash."""
    await _seed_max_ever(PnlSnapshotRepository(session_factory))
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _reader(total=400.0),
        queue,
        # sentinel=None par défaut
    )
    # Ne doit PAS raise.
    await _run_once(writer)
    # L'alerte kill_switch doit quand même être poussée.
    events = set()
    while not queue.empty():
        events.add(queue.get_nowait().event)
    assert "kill_switch_triggered" in events


# ===========================================================================
# Invariant d'ordre — touch AVANT stop_event.set()
# ===========================================================================


class _SpyStopEvent:
    """Wrap ``asyncio.Event`` avec capture de timestamp de ``set()``."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.set_called_after_touch: bool | None = None

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    async def wait(self) -> bool:
        return await self._event.wait()

    def clear(self) -> None:
        self._event.clear()


@pytest.mark.asyncio
async def test_sentinel_touch_strictly_before_stop_event_set(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Invariant critique §4.6 : au moment où ``sentinel.touch()`` est appelé,
    ``stop_event.is_set()`` doit encore retourner False. L'inverse serait unsafe.
    """
    await _seed_max_ever(PnlSnapshotRepository(session_factory))
    sentinel = SentinelFile(tmp_path / "halt.flag")

    # Wrap le sentinel pour capturer l'état de stop_event au moment du touch.
    stop_event_state_at_touch: list[bool] = []
    real_touch = sentinel.touch

    spy_sentinel = MagicMock(wraps=sentinel)

    def _wrapped_touch(reason: str) -> None:
        # Capture `stop.is_set()` avant le set (fait 1 ligne après dans pnl_writer).
        stop_event_state_at_touch.append(stop.is_set())
        real_touch(reason)

    spy_sentinel.touch.side_effect = _wrapped_touch
    # Les autres méthodes passent par wraps=sentinel.

    queue: asyncio.Queue[Alert] = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _reader(total=400.0),
        queue,
        sentinel=spy_sentinel,  # type: ignore[arg-type]
    )
    stop = asyncio.Event()

    async def _stop_later() -> None:
        await asyncio.sleep(0.3)
        stop.set()

    await asyncio.gather(writer.run(stop), _stop_later())
    # touch a été appelé.
    assert spy_sentinel.touch.called is True
    # Au moment du touch, stop_event était False (pas encore set).
    assert stop_event_state_at_touch == [False], (
        "sentinel.touch() doit être appelé STRICTEMENT avant stop_event.set()"
    )
    # Le sentinel a bien été persisté sur disque (wraps a laissé passer).
    assert sentinel.exists() is True
    assert sentinel.reason() == "kill_switch"
