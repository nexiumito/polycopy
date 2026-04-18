"""Test live Telegram (opt-in, nécessite ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``)."""

from __future__ import annotations

import httpx
import pytest

from polycopy.config import Settings
from polycopy.monitoring.telegram_client import TelegramClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_send_real_message() -> None:
    settings = Settings()  # type: ignore[call-arg]
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        pytest.skip("Telegram not configured in .env")
    async with httpx.AsyncClient() as http:
        client = TelegramClient(http, settings)
        sent = await client.send("polycopy integration test — peut être ignoré")
    assert sent is True
