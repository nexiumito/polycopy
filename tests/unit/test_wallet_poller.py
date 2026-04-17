"""Tests du WalletPoller."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

import polycopy.watcher.wallet_poller as wp_module
from polycopy.storage.repositories import DetectedTradeRepository
from polycopy.watcher.data_api_client import DataApiClient
from polycopy.watcher.dtos import TradeActivity
from polycopy.watcher.wallet_poller import WalletPoller


def _activity(tx: str, ts: int) -> TradeActivity:
    payload: dict[str, Any] = {
        "type": "TRADE",
        "proxyWallet": "0xwallet",
        "timestamp": ts,
        "conditionId": "0xcond",
        "asset": "123",
        "side": "BUY",
        "size": 10.0,
        "usdcSize": 5.0,
        "price": 0.5,
        "transactionHash": tx,
        "outcome": "Yes",
        "slug": "slug",
        "outcomeIndex": 0,
    }
    return TradeActivity.model_validate(payload)


async def _stop_after(stop_event: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    stop_event.set()


async def test_poller_persists_new_trades_and_dedups(
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    base_ts = int(datetime.now(tz=UTC).timestamp())
    client = AsyncMock(spec=DataApiClient)
    client.get_trades.side_effect = [
        [_activity(f"0xtx{i}", ts=base_ts + i) for i in range(3)],
        [],
        [],
    ]
    stop_event = asyncio.Event()
    poller = WalletPoller("0xWALLET", client, detected_trade_repo, interval_seconds=0)
    await asyncio.gather(poller.run(stop_event), _stop_after(stop_event, 0.1))
    assert await detected_trade_repo.count_for_wallet("0xwallet") == 3

    # Re-running with the same trades returned must not duplicate.
    client.get_trades.side_effect = [
        [_activity(f"0xtx{i}", ts=base_ts + i) for i in range(3)],
        [],
        [],
    ]
    stop_event2 = asyncio.Event()
    poller2 = WalletPoller("0xwallet", client, detected_trade_repo, interval_seconds=0)
    await asyncio.gather(poller2.run(stop_event2), _stop_after(stop_event2, 0.1))
    assert await detected_trade_repo.count_for_wallet("0xwallet") == 3


async def test_poller_keeps_running_on_api_error(
    detected_trade_repo: DetectedTradeRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wp_module, "_BACKOFF_AFTER_ERROR_SECONDS", 0.01)
    base_ts = int(datetime.now(tz=UTC).timestamp())
    client = AsyncMock(spec=DataApiClient)
    client.get_trades.side_effect = [
        RuntimeError("boom"),
        *([[_activity("0xtx_ok", ts=base_ts)]] * 10),
    ]
    stop_event = asyncio.Event()
    poller = WalletPoller("0xwallet", client, detected_trade_repo, interval_seconds=0)
    await asyncio.gather(poller.run(stop_event), _stop_after(stop_event, 0.15))
    assert await detected_trade_repo.count_for_wallet("0xwallet") == 1
    assert client.get_trades.await_count >= 2


async def test_poller_resumes_from_last_persisted_timestamp(
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    """Au boot, poller passe `since=last_ts` au client si la DB en contient un."""
    base_ts = int(datetime.now(tz=UTC).timestamp())
    seed = _activity("0xseed", ts=base_ts - 60)
    poller_seed = WalletPoller(
        "0xwallet",
        AsyncMock(spec=DataApiClient),
        detected_trade_repo,
        interval_seconds=0,
    )
    await detected_trade_repo.insert_if_new(poller_seed._to_dto(seed))

    captured_since: list[datetime | None] = []

    async def _capture(
        wallet: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[TradeActivity]:
        del wallet, limit
        captured_since.append(since)
        return []

    client = AsyncMock(spec=DataApiClient)
    client.get_trades.side_effect = _capture

    stop_event = asyncio.Event()
    poller = WalletPoller("0xwallet", client, detected_trade_repo, interval_seconds=0)
    await asyncio.gather(poller.run(stop_event), _stop_after(stop_event, 0.05))
    assert captured_since
    assert captured_since[0] is not None
    assert int(captured_since[0].timestamp()) == base_ts - 60
