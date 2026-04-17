"""Tests du client Data API Polymarket avec respx."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
import tenacity

from polycopy.watcher.data_api_client import DataApiClient


def _trade(tx: str, ts: int = 1_700_000_000) -> dict[str, Any]:
    return {
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
        "title": "T",
        "icon": "",
        "eventSlug": "e",
        "name": "n",
        "pseudonym": "p",
        "bio": "",
        "profileImage": "",
        "profileImageOptimized": "",
    }


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Annule le sleep tenacity pour garder les tests rapides."""
    monkeypatch.setattr(
        DataApiClient._fetch_page.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


async def test_get_trades_happy_path() -> None:
    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        mock.get("/activity").mock(return_value=httpx.Response(200, json=[_trade("0xtx1")]))
        async with httpx.AsyncClient() as http:
            client = DataApiClient(http)
            trades = await client.get_trades("0xWALLET")
    assert len(trades) == 1
    only = trades[0]
    assert only.transaction_hash == "0xtx1"
    assert only.proxy_wallet == "0xwallet"
    assert only.usdc_size == 5.0
    assert only.condition_id == "0xcond"
    assert only.side == "BUY"


async def test_get_trades_passes_user_lowercased_and_required_params() -> None:
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        mock.get("/activity").mock(side_effect=_handler)
        async with httpx.AsyncClient() as http:
            client = DataApiClient(http)
            await client.get_trades("0xWALLET")
    assert len(captured) == 1
    params = captured[0].url.params
    assert params["user"] == "0xwallet"
    assert params["type"] == "TRADE"
    assert params["sortDirection"] == "ASC"
    assert "start" not in params


async def test_get_trades_passes_since_as_unix_seconds() -> None:
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        mock.get("/activity").mock(side_effect=_handler)
        async with httpx.AsyncClient() as http:
            client = DataApiClient(http)
            since = datetime(2026, 1, 1, tzinfo=UTC)
            await client.get_trades("0xwallet", since=since)
    assert int(captured[0].url.params["start"]) == int(since.timestamp())


async def test_get_trades_pagination() -> None:
    page1 = [_trade(f"0xtx{i}", ts=1_700_000_000 + i) for i in range(100)]
    page2 = [_trade("0xtx_last", ts=1_700_000_200)]
    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        route = mock.get("/activity")
        route.side_effect = [
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
        async with httpx.AsyncClient() as http:
            client = DataApiClient(http)
            trades = await client.get_trades("0xwallet")
    assert len(trades) == 101
    assert trades[-1].transaction_hash == "0xtx_last"


async def test_get_trades_filters_non_trade_items() -> None:
    payload = [_trade("0xtx_trade"), {**_trade("0xtx_split"), "type": "SPLIT"}]
    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        mock.get("/activity").mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as http:
            client = DataApiClient(http)
            trades = await client.get_trades("0xwallet")
    assert len(trades) == 1
    assert trades[0].transaction_hash == "0xtx_trade"


async def test_get_trades_retries_on_429_then_succeeds() -> None:
    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        route = mock.get("/activity")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, json=[_trade("0xtx_ok")]),
        ]
        async with httpx.AsyncClient() as http:
            client = DataApiClient(http)
            trades = await client.get_trades("0xwallet")
    assert len(trades) == 1
    assert route.call_count == 3


async def test_get_trades_propagates_after_max_retries() -> None:
    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        mock.get("/activity").mock(side_effect=httpx.ConnectError("boom"))
        async with httpx.AsyncClient() as http:
            client = DataApiClient(http)
            with pytest.raises(httpx.ConnectError):
                await client.get_trades("0xwallet")


async def test_get_trades_parses_real_fixture(
    sample_activity_payload: list[dict[str, Any]],
) -> None:
    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        mock.get("/activity").mock(
            return_value=httpx.Response(200, json=sample_activity_payload),
        )
        async with httpx.AsyncClient() as http:
            client = DataApiClient(http)
            trades = await client.get_trades("0xwallet", limit=len(sample_activity_payload) + 1)
    assert len(trades) == len(sample_activity_payload)
    assert all(t.type == "TRADE" for t in trades)
    assert all(t.side in {"BUY", "SELL"} for t in trades)
