"""Tests du ``MonitoringOrchestrator`` (lance writer + dispatcher en parallèle)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.monitoring import alert_dispatcher as dispatcher_module
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.orchestrator import MonitoringOrchestrator


def _dry_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=True,
        pnl_snapshot_interval_seconds=1,
    )


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_module, "_QUEUE_GET_TIMEOUT_SECONDS", 0.02)


@pytest.mark.asyncio
async def test_orchestrator_runs_without_telegram(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Vérifie que le writer + dispatcher démarrent sans token Telegram."""
    from structlog.testing import capture_logs

    queue: asyncio.Queue[Alert] = asyncio.Queue()
    orch = MonitoringOrchestrator(session_factory, _dry_settings(), queue)
    stop = asyncio.Event()

    async def _stop_later() -> None:
        await asyncio.sleep(0.2)
        stop.set()

    with capture_logs() as logs:
        await asyncio.gather(orch.run_forever(stop), _stop_later())

    event_names = {entry.get("event") for entry in logs}
    assert "monitoring_started" in event_names
    assert "telegram_disabled" in event_names
    assert "pnl_snapshot_writer_started" in event_names
    assert "alert_dispatcher_started" in event_names
    assert "monitoring_stopped" in event_names
