"""Tests du ``StartupNotifier`` (build context + bypass si Telegram disabled)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.startup_notifier import StartupNotifier
from polycopy.monitoring.telegram_client import TelegramClient
from polycopy.storage.repositories import TargetTraderRepository

_TOKEN = "123:abc"
_CHAT = "42"


def _settings(**kw: object) -> Settings:
    base: dict[str, Any] = dict(
        telegram_bot_token=_TOKEN,
        telegram_chat_id=_CHAT,
        dashboard_enabled=True,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _disabled_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest_asyncio.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.mark.asyncio
async def test_startup_skipped_when_telegram_disabled(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    tg = TelegramClient(http_client, _disabled_settings())
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), _settings())
    stop = asyncio.Event()
    await notifier.send_once(stop)
    # Pas d'envoi, pas d'exception.


@pytest.mark.asyncio
async def test_startup_sent_with_context(
    session_factory: async_sessionmaker[AsyncSession],
    target_trader_repo: TargetTraderRepository,
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
) -> None:
    await target_trader_repo.upsert("0x" + "a" * 40, label="Smart Money")
    await target_trader_repo.upsert("0x" + "b" * 40, label=None)

    settings = _settings()
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    stop = asyncio.Event()

    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        await notifier.send_once(stop)

    assert route.called
    body = route.calls[0].request.content.decode()
    # Le message contient la marque polycopy + au moins un wallet
    assert "polycopy" in body
    # Le label Smart Money doit apparaître (échappé)
    assert "Smart Money" in body


@pytest.mark.asyncio
async def test_startup_fail_safe_on_400(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
) -> None:
    settings = _settings()
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    with respx.mock() as mock:
        mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(400, json={"ok": False, "description": "bad"}),
        )
        await notifier.send_once(asyncio.Event())  # pas d'exception propagée


@pytest.mark.asyncio
async def test_startup_noop_if_stop_event_already_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tg = AsyncMock()
    tg.enabled = True
    tg.send = AsyncMock(return_value=True)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), _settings())
    stop = asyncio.Event()
    stop.set()
    await notifier.send_once(stop)
    tg.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_no_pinned_wallets(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
) -> None:
    settings = _settings()
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        await notifier.send_once(asyncio.Event())
    assert route.called
    body = route.calls[0].request.content.decode()
    assert "Aucun wallet pinned" in body
