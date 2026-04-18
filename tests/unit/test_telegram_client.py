"""Tests du ``TelegramClient`` (respx) + vérif no-leak token via caplog."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx

from polycopy.config import Settings
from polycopy.monitoring.telegram_client import TelegramClient

_TOKEN = "123456789:ABCdefghijklmnopqrstuvwxyz_secret_token_"
_CHAT_ID = "987654"


def _disabled_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _enabled_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token=_TOKEN,
        telegram_chat_id=_CHAT_ID,
    )


@pytest_asyncio.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


# --- Disabled mode (spec §0.3 + §3.5) --------------------------------------


@pytest.mark.asyncio
async def test_disabled_mode_no_network(http_client: httpx.AsyncClient) -> None:
    client = TelegramClient(http_client, _disabled_settings())
    assert client.enabled is False
    with respx.mock(base_url="https://api.telegram.org") as mock:
        sent = await client.send("hello")
    assert sent is False
    assert len(mock.calls) == 0


# --- Enabled happy path -----------------------------------------------------


@pytest.mark.asyncio
async def test_enabled_send_ok(
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
) -> None:
    client = TelegramClient(http_client, _enabled_settings())
    assert client.enabled is True
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        ok = await client.send("hello polycopy")
    assert ok is True
    assert route.called
    body = route.calls[0].request.content.decode()
    assert _CHAT_ID in body
    assert "hello polycopy" in body


# --- 429 → retry → succès ---------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_429_then_success(
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
) -> None:
    client = TelegramClient(http_client, _enabled_settings())
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            side_effect=[
                httpx.Response(429, json={"ok": False, "error_code": 429}),
                httpx.Response(200, json=sample_telegram_response),
            ],
        )
        ok = await client.send("retry me")
    assert ok is True
    assert route.call_count == 2


# --- 400 → no retry, False --------------------------------------------------


@pytest.mark.asyncio
async def test_400_no_retry_returns_false(http_client: httpx.AsyncClient) -> None:
    client = TelegramClient(http_client, _enabled_settings())
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "Bad Request"},
            ),
        )
        ok = await client.send("bad markdown *unclosed")
    assert ok is False
    assert route.call_count == 1


# --- 500 → retries exhausted → False ---------------------------------------


@pytest.mark.asyncio
async def test_500_retries_exhausted(http_client: httpx.AsyncClient) -> None:
    client = TelegramClient(http_client, _enabled_settings())
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(500),
        )
        ok = await client.send("server down")
    assert ok is False
    assert route.call_count == 3


# --- Aucun log du token (critère §12 spec) ---------------------------------


@pytest.mark.asyncio
async def test_token_never_leaked_in_logs(
    http_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="polycopy.monitoring.telegram_client")
    caplog.set_level(logging.DEBUG, logger="polycopy")
    client = TelegramClient(http_client, _enabled_settings())
    with respx.mock() as mock:
        mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "Bad Request"},
            ),
        )
        await client.send("ignored")
    for rec in caplog.records:
        assert _TOKEN not in rec.getMessage(), f"Token leaked in log record: {rec.message!r}"
