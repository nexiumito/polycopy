"""Tests M8 — ``ExecutorOrchestrator`` lance ``DryRunResolutionWatcher`` conditionnellement."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor import dry_run_resolution_watcher as watcher_module
from polycopy.executor import orchestrator as orchestrator_module
from polycopy.executor.orchestrator import ExecutorOrchestrator
from polycopy.monitoring.dtos import Alert
from polycopy.strategy.dtos import OrderApproved


def _settings(*, m8: bool) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=True,
        dry_run_realistic_fill=m8,
        dry_run_resolution_poll_minutes=5,
    )


@pytest.mark.asyncio
async def test_resolution_watcher_launched_when_m8_enabled(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator_module, "_QUEUE_GET_TIMEOUT_SECONDS", 0.02)
    fake_run_forever = AsyncMock()
    monkeypatch.setattr(
        watcher_module.DryRunResolutionWatcher,
        "run_forever",
        fake_run_forever,
    )
    approved_q: asyncio.Queue[OrderApproved] = asyncio.Queue()
    alerts_q: asyncio.Queue[Alert] = asyncio.Queue()
    orch = ExecutorOrchestrator(
        session_factory,
        _settings(m8=True),
        approved_q,
        alerts_queue=alerts_q,
    )
    stop = asyncio.Event()

    async def _stop_quickly() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(orch.run_forever(stop), _stop_quickly())
    fake_run_forever.assert_awaited()


@pytest.mark.asyncio
async def test_resolution_watcher_not_launched_when_m8_disabled(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator_module, "_QUEUE_GET_TIMEOUT_SECONDS", 0.02)
    fake_run_forever = AsyncMock()
    monkeypatch.setattr(
        watcher_module.DryRunResolutionWatcher,
        "run_forever",
        fake_run_forever,
    )
    approved_q: asyncio.Queue[OrderApproved] = asyncio.Queue()
    alerts_q: asyncio.Queue[Alert] = asyncio.Queue()
    orch = ExecutorOrchestrator(
        session_factory,
        _settings(m8=False),
        approved_q,
        alerts_queue=alerts_q,
    )
    stop = asyncio.Event()

    async def _stop_quickly() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(orch.run_forever(stop), _stop_quickly())
    fake_run_forever.assert_not_called()
