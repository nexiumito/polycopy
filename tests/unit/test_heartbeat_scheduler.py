"""Tests du ``HeartbeatScheduler`` : intervalle + skip si critical recent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.monitoring.alert_dispatcher import AlertDispatcher
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.heartbeat_scheduler import HeartbeatScheduler
from polycopy.monitoring.telegram_client import TelegramClient

_TOKEN = "123:abc"
_CHAT = "42"


def _settings(interval_hours: int = 12, **kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        telegram_bot_token=_TOKEN,
        telegram_chat_id=_CHAT,
        telegram_heartbeat_enabled=True,
        telegram_heartbeat_interval_hours=interval_hours,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _disabled_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest_asyncio.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


class _FakeDispatcher:
    def __init__(self, critical: bool = False) -> None:
        self._critical = critical

    def has_recent_critical(self, window: timedelta) -> bool:
        return self._critical


@pytest.mark.asyncio
async def test_heartbeat_noop_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
) -> None:
    settings = _disabled_settings()
    tg = TelegramClient(http_client, settings)
    dispatcher = _FakeDispatcher()
    scheduler = HeartbeatScheduler(
        session_factory,
        tg,
        AlertRenderer(),
        settings,
        dispatcher,  # type: ignore[arg-type]
    )
    stop = asyncio.Event()
    stop.set()
    await scheduler.run(stop)  # retour immédiat, pas d'envoi


@pytest.mark.asyncio
async def test_heartbeat_fires_after_interval(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(interval_hours=1)
    tg = TelegramClient(http_client, settings)
    dispatcher = _FakeDispatcher(critical=False)
    scheduler = HeartbeatScheduler(
        session_factory,
        tg,
        AlertRenderer(),
        settings,
        dispatcher,  # type: ignore[arg-type]
    )

    # Compresse le timeout en bypassant asyncio.wait_for via monkeypatch
    original_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:  # noqa: ASYNC109
        return await original_wait_for(awaitable, 0.05)

    monkeypatch.setattr(
        "polycopy.monitoring.heartbeat_scheduler.asyncio.wait_for",
        fake_wait_for,
    )

    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        stop = asyncio.Event()

        async def stop_later() -> None:
            await asyncio.sleep(0.2)
            stop.set()

        await asyncio.gather(scheduler.run(stop), stop_later())

    assert route.called
    body = route.calls[0].request.content.decode()
    assert "polycopy actif" in body


@pytest.mark.asyncio
async def test_heartbeat_skipped_when_recent_critical(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(interval_hours=1)
    tg = TelegramClient(http_client, settings)
    dispatcher = _FakeDispatcher(critical=True)
    scheduler = HeartbeatScheduler(
        session_factory,
        tg,
        AlertRenderer(),
        settings,
        dispatcher,  # type: ignore[arg-type]
    )
    original_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:  # noqa: ASYNC109
        return await original_wait_for(awaitable, 0.05)

    monkeypatch.setattr(
        "polycopy.monitoring.heartbeat_scheduler.asyncio.wait_for",
        fake_wait_for,
    )

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200),
        )
        stop = asyncio.Event()

        async def stop_later() -> None:
            await asyncio.sleep(0.2)
            stop.set()

        await asyncio.gather(scheduler.run(stop), stop_later())

    # Critical recent → heartbeat sauté.
    assert not route.called


@pytest.mark.asyncio
async def test_dispatcher_has_recent_critical_integration(
    http_client: httpx.AsyncClient,
) -> None:
    """Vérifie que dispatcher.has_recent_critical reflète l'état interne."""
    settings = _settings()
    tg = AsyncMock()
    tg.enabled = True
    tg.send = AsyncMock(return_value=True)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    dispatcher = AlertDispatcher(queue, tg, settings)

    # Avant toute alerte
    assert dispatcher.has_recent_critical(timedelta(minutes=5)) is False
    # Injection d'un timestamp critique fictif
    dispatcher._last_critical_at = datetime.now(tz=UTC)  # noqa: SLF001
    assert dispatcher.has_recent_critical(timedelta(minutes=5)) is True
    # Fenêtre trop courte
    dispatcher._last_critical_at = datetime.now(tz=UTC) - timedelta(hours=2)  # noqa: SLF001
    assert dispatcher.has_recent_critical(timedelta(minutes=5)) is False
