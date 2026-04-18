"""Tests de l'extension M5 `GammaApiClient.list_top_markets`."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from polycopy.strategy.gamma_client import GammaApiClient

_FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def gamma_top_payload() -> list[dict]:
    return json.loads((_FIXTURES / "gamma_top_markets_sample.json").read_text())


@respx.mock
async def test_list_top_markets_sends_liquiditynum_order(
    gamma_top_payload: list[dict],
) -> None:
    route = respx.get("https://gamma-api.polymarket.com/markets").mock(
        return_value=httpx.Response(200, json=gamma_top_payload),
    )
    async with httpx.AsyncClient() as http:
        client = GammaApiClient(http)
        markets = await client.list_top_markets(limit=20)
    assert len(markets) > 0
    qs = dict(route.calls[-1].request.url.params)
    assert qs["limit"] == "20"
    assert qs["order"] == "liquidityNum"
    assert qs["ascending"] == "false"
    assert qs["active"] == "true"
    assert qs["closed"] == "false"


@respx.mock
async def test_list_top_markets_skip_invalid_entries(
    gamma_top_payload: list[dict],
) -> None:
    """Une entrée cassée ne doit pas empêcher de retourner les autres."""
    payload = [*gamma_top_payload, {"not": "a-market"}]  # bonus casseur
    respx.get("https://gamma-api.polymarket.com/markets").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with httpx.AsyncClient() as http:
        client = GammaApiClient(http)
        markets = await client.list_top_markets(limit=20)
    assert len(markets) == len(gamma_top_payload)  # casseur skipped


@respx.mock
async def test_list_top_markets_rejects_non_list_payload() -> None:
    respx.get("https://gamma-api.polymarket.com/markets").mock(
        return_value=httpx.Response(200, json={"not": "a-list"}),
    )
    async with httpx.AsyncClient() as http:
        client = GammaApiClient(http)
        with pytest.raises(httpx.HTTPStatusError):
            await client.list_top_markets(limit=20)
