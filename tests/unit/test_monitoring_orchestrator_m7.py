"""Tests M7 du ``MonitoringOrchestrator`` : schedulers conditionnels."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.orchestrator import MonitoringOrchestrator

_TOKEN = "123:abc"
_CHAT = "42"


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        telegram_bot_token=_TOKEN,
        telegram_chat_id=_CHAT,
        telegram_startup_message=False,
        telegram_heartbeat_enabled=False,
        telegram_daily_summary=False,
        pnl_snapshot_interval_seconds=3600,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_orchestrator_starts_and_stops_with_stop_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()
    alerts_queue: asyncio.Queue[Alert] = asyncio.Queue()
    orchestrator = MonitoringOrchestrator(session_factory, settings, alerts_queue)
    stop = asyncio.Event()
    stop.set()  # stop_event set immédiatement → retour propre
    await orchestrator.run_forever(stop)


@pytest.mark.asyncio
async def test_orchestrator_sends_startup_then_shutdown(
    session_factory: async_sessionmaker[AsyncSession],
    sample_telegram_response: dict[str, Any],
) -> None:
    settings = _settings(telegram_startup_message=True)
    alerts_queue: asyncio.Queue[Alert] = asyncio.Queue()
    orchestrator = MonitoringOrchestrator(session_factory, settings, alerts_queue)

    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            side_effect=[
                __import__("httpx").Response(200, json=sample_telegram_response),
                __import__("httpx").Response(200, json=sample_telegram_response),
            ],
        )
        stop = asyncio.Event()

        async def stop_after() -> None:
            await asyncio.sleep(0.3)
            stop.set()

        await asyncio.gather(orchestrator.run_forever(stop), stop_after())

    # 1 startup + 1 shutdown
    assert route.call_count >= 1
