"""Tests de ``DashboardOrchestrator`` : start / shutdown propre via ``stop_event``."""

from __future__ import annotations

import asyncio
import socket

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard.orchestrator import DashboardOrchestrator


def _loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
    except OSError:
        return False
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.skipif(
    not _loopback_available(),
    reason="127.0.0.1 bind indisponible",
)
@pytest.mark.asyncio
async def test_dashboard_orchestrator_runs_and_stops(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        dashboard_enabled=True,
        dashboard_host="127.0.0.1",
        dashboard_port=_free_port(),
    )
    orchestrator = DashboardOrchestrator(session_factory, settings)
    stop_event = asyncio.Event()

    async def _trigger_stop() -> None:
        await asyncio.sleep(0.3)
        stop_event.set()

    await asyncio.wait_for(
        asyncio.gather(
            orchestrator.run_forever(stop_event),
            _trigger_stop(),
        ),
        timeout=5.0,
    )
