"""Tests DiscoveryDataApiClient (respx mocks)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from polycopy.discovery.data_api_client import DiscoveryDataApiClient

_FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def holders_payload() -> list[dict]:
    return json.loads((_FIXTURES / "data_api_holders_sample.json").read_text())


@pytest.fixture
def global_trades_payload() -> list[dict]:
    return json.loads((_FIXTURES / "data_api_trades_global_sample.json").read_text())


@pytest.fixture
def value_payload() -> list[dict]:
    return json.loads((_FIXTURES / "data_api_value_sample.json").read_text())


@pytest.fixture
def positions_payload() -> list[dict]:
    return json.loads((_FIXTURES / "data_api_positions_sample.json").read_text())


@respx.mock
async def test_get_holders_dedup_across_outcome_groups(
    holders_payload: list[dict],
) -> None:
    respx.get("https://data-api.polymarket.com/holders").mock(
        return_value=httpx.Response(200, json=holders_payload),
    )
    async with httpx.AsyncClient() as http:
        client = DiscoveryDataApiClient(http)
        holders = await client.get_holders("0xcond", limit=20)
    wallets = [h.proxy_wallet for h in holders]
    # Dédup par proxyWallet garanti.
    assert len(wallets) == len(set(wallets))
    # Au moins 1 holder extrait (fixture réelle ≥ 20).
    assert len(wallets) > 0


@respx.mock
async def test_get_holders_404_returns_empty() -> None:
    respx.get("https://data-api.polymarket.com/holders").mock(
        return_value=httpx.Response(404),
    )
    async with httpx.AsyncClient() as http:
        client = DiscoveryDataApiClient(http)
        assert await client.get_holders("0xunknown") == []


@respx.mock
async def test_get_global_trades_filters_server_side(
    global_trades_payload: list[dict],
) -> None:
    """Vérifie que les params `filterAmount=100, takerOnly=true, filterType=CASH` sont envoyés."""
    route = respx.get("https://data-api.polymarket.com/trades").mock(
        return_value=httpx.Response(200, json=global_trades_payload),
    )
    async with httpx.AsyncClient() as http:
        client = DiscoveryDataApiClient(http)
        trades = await client.get_global_trades(limit=500, min_usdc_size=100.0)
    assert len(trades) > 0
    # Le param filterAmount doit être présent.
    last_req = route.calls[-1].request
    qs = dict(last_req.url.params)
    assert qs["filterAmount"] == "100"
    assert qs["takerOnly"] == "true"
    assert qs["filterType"] == "CASH"


@respx.mock
async def test_get_value_handles_empty_response() -> None:
    respx.get("https://data-api.polymarket.com/value").mock(
        return_value=httpx.Response(200, json=[]),
    )
    async with httpx.AsyncClient() as http:
        client = DiscoveryDataApiClient(http)
        assert await client.get_value("0xunknown") == 0.0


@respx.mock
async def test_get_value_parses_single_entry(value_payload: list[dict]) -> None:
    respx.get("https://data-api.polymarket.com/value").mock(
        return_value=httpx.Response(200, json=value_payload),
    )
    async with httpx.AsyncClient() as http:
        client = DiscoveryDataApiClient(http)
        v = await client.get_value("0xabc")
    assert v == pytest.approx(float(value_payload[0]["value"]))


@respx.mock
async def test_get_positions_uses_correct_params(
    positions_payload: list[dict],
) -> None:
    route = respx.get("https://data-api.polymarket.com/positions").mock(
        return_value=httpx.Response(200, json=positions_payload),
    )
    async with httpx.AsyncClient() as http:
        client = DiscoveryDataApiClient(http)
        positions = await client.get_positions("0xUser")
    assert len(positions) == len(positions_payload)
    qs = dict(route.calls[-1].request.url.params)
    assert qs["sortBy"] == "CASHPNL"
    assert qs["sortDirection"] == "DESC"
    assert qs["user"] == "0xuser"


@respx.mock
async def test_get_activity_trades_filters_type() -> None:
    raw = [
        {"type": "TRADE", "conditionId": "0xa", "size": "1", "price": "0.5"},
        {"type": "SPLIT", "conditionId": "0xb"},  # filtered out
        {"type": "TRADE", "conditionId": "0xc", "size": "2", "price": "0.3"},
    ]
    respx.get("https://data-api.polymarket.com/activity").mock(
        return_value=httpx.Response(200, json=raw),
    )
    async with httpx.AsyncClient() as http:
        client = DiscoveryDataApiClient(http)
        activity = await client.get_activity_trades("0xabc")
    assert len(activity) == 2
    assert all(t["type"] == "TRADE" for t in activity)
