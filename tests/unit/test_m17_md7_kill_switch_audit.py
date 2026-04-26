"""Tests M17 MD.7 — kill switch `TraderEvent` audit trail (audit H-005).

Avant M17 : kill switch écrit Telegram + sentinel + stop_event mais n'écrit
**jamais** dans `trader_events` → milestone `/pnl` "Kill switch" toujours
vide. Audit muet sur l'événement le plus important du bot.

MD.7 : `PnlSnapshotWriter._maybe_trigger_alerts` insert
`TraderEventDTO(wallet_address=None, event_type="kill_switch", event_metadata={...})`
**AVANT** `push_alert` AVANT `touch_sentinel` AVANT `stop_event.set()`.
Ordre strict — cf. CLAUDE.md §Sécurité M12_bis Phase D.

Couvre :
- Insert TraderEvent system-level (wallet_address=None, metadata complet).
- Ordre strict event → alert → sentinel → stop_event.
- Insert DB échoue → kill switch fire quand même (try/except large).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.dtos import WalletState
from polycopy.executor.virtual_wallet_reader import VirtualWalletStateReader
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.pnl_writer import PnlSnapshotWriter
from polycopy.remote_control import SentinelFile
from polycopy.storage.dtos import PnlSnapshotDTO
from polycopy.storage.models import TraderEvent
from polycopy.storage.repositories import (
    PnlSnapshotRepository,
    TraderEventRepository,
)


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


async def _seed_max_ever(
    repo: PnlSnapshotRepository,
    *,
    execution_mode: str = "dry_run",
) -> None:
    await repo.insert(
        PnlSnapshotDTO(
            total_usdc=1000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=0.0,
            open_positions_count=0,
            cash_pnl_total=None,
            is_dry_run=execution_mode != "live",
            execution_mode=execution_mode,  # type: ignore[arg-type]
        ),
    )


async def _run_once(writer: PnlSnapshotWriter, timeout: float = 0.3) -> None:  # noqa: ASYNC109
    stop = asyncio.Event()

    async def _stop_later() -> None:
        await asyncio.sleep(timeout)
        stop.set()

    await asyncio.gather(writer.run(stop), _stop_later())


@pytest.mark.asyncio
async def test_kill_switch_writes_trader_event_system_level(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Kill switch → TraderEvent(wallet_address=None, event_type='kill_switch')."""
    await _seed_max_ever(PnlSnapshotRepository(session_factory))
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    sentinel = SentinelFile(tmp_path / "halt.flag")
    events_repo = TraderEventRepository(session_factory)
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _reader(total=400.0),  # 60% drawdown ≥ seuil 20%
        queue,
        sentinel=sentinel,
        events_repo=events_repo,
    )
    await _run_once(writer)
    # Vérifie que l'event kill_switch existe avec wallet_address=None.
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(TraderEvent).where(
                        TraderEvent.event_type == "kill_switch",
                    ),
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 1
    event = rows[0]
    assert event.wallet_address is None
    metadata = event.event_metadata or {}
    assert metadata["execution_mode"] == "dry_run"
    assert metadata["threshold"] == 20.0
    assert metadata["total_usdc"] == pytest.approx(400.0)
    assert metadata["max_total_usdc"] == pytest.approx(1000.0)
    assert metadata["drawdown_pct"] == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_kill_switch_order_strict(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Ordre strict : event → alert → sentinel → stop_event.set().

    Capture la séquence via instrumentation des side-effects sur :
    - ``events_repo.insert``
    - ``alerts_queue.put_nowait``
    - ``sentinel.touch``
    - ``stop_event.set``
    """
    await _seed_max_ever(PnlSnapshotRepository(session_factory))

    sequence: list[str] = []

    # 1. events_repo.insert wrappé.
    real_repo = TraderEventRepository(session_factory)
    real_insert = real_repo.insert

    async def _wrapped_insert(dto):  # type: ignore[no-untyped-def]
        sequence.append("event")
        return await real_insert(dto)

    real_repo.insert = _wrapped_insert  # type: ignore[method-assign]

    # 2. alerts queue avec spy put_nowait.
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    real_put = queue.put_nowait

    def _wrapped_put(alert: Alert) -> None:
        if alert.event == "kill_switch_triggered":
            sequence.append("alert")
        real_put(alert)

    queue.put_nowait = _wrapped_put  # type: ignore[method-assign]

    # 3. sentinel.touch wrappé.
    sentinel = SentinelFile(tmp_path / "halt.flag")
    real_touch = sentinel.touch

    def _wrapped_touch(reason: str) -> None:
        sequence.append("sentinel")
        real_touch(reason)

    sentinel.touch = _wrapped_touch  # type: ignore[method-assign]

    # 4. stop_event.set wrappé.
    stop = asyncio.Event()
    real_set = stop.set

    def _wrapped_set() -> None:
        sequence.append("stop")
        real_set()

    stop.set = _wrapped_set  # type: ignore[method-assign]

    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _reader(total=400.0),
        queue,
        sentinel=sentinel,
        events_repo=real_repo,
    )

    async def _stop_later() -> None:
        await asyncio.sleep(0.3)
        if not stop.is_set():
            real_set()

    await asyncio.gather(writer.run(stop), _stop_later())

    # Filtre les éventuels stop additionnels du timeout.
    relevant = [s for s in sequence if s in ("event", "alert", "sentinel", "stop")]
    # Le 1ᵉʳ "stop" déclenché par le writer doit être précédé par event/alert/sentinel.
    first_stop_idx = relevant.index("stop")
    pre_stop = relevant[:first_stop_idx]
    assert pre_stop == ["event", "alert", "sentinel"], (
        f"Ordre strict violé : {pre_stop} ≠ ['event', 'alert', 'sentinel']"
    )


@pytest.mark.asyncio
async def test_kill_switch_event_insert_failure_does_not_block_stop(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Si events_repo.insert raise, le kill switch fire quand même (try/except large)."""
    await _seed_max_ever(PnlSnapshotRepository(session_factory))
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    sentinel = SentinelFile(tmp_path / "halt.flag")

    # Mock un repo qui raise sur insert.
    failing_repo = AsyncMock(spec=TraderEventRepository)
    failing_repo.insert.side_effect = RuntimeError("DB locked")

    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        _reader(total=400.0),
        queue,
        sentinel=sentinel,
        events_repo=failing_repo,
    )
    await _run_once(writer)

    # Telegram alert + sentinel + stop_event ont fire malgré l'insert raté.
    events_drained: list[str] = []
    while not queue.empty():
        events_drained.append(queue.get_nowait().event)
    assert "kill_switch_triggered" in events_drained
    assert sentinel.exists() is True
    assert sentinel.reason() == "kill_switch"


# Garde la trace temporelle (datetime UTC) cohérente avec les autres tests
# sentinel-ordering — pour debug futur si la séquence change.
def _approx_now() -> datetime:
    return datetime.now(tz=UTC)
