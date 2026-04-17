"""Tests du `GammaApiClient` (respx + cache TTL + retry)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
import tenacity

from polycopy.strategy.gamma_client import GammaApiClient


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        GammaApiClient._fetch.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


async def test_get_market_happy_path(sample_gamma_market: dict[str, Any]) -> None:
    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        mock.get("/markets").mock(
            return_value=httpx.Response(200, json=[sample_gamma_market]),
        )
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http)
            market = await client.get_market(sample_gamma_market["conditionId"])
    assert market is not None
    assert market.condition_id == sample_gamma_market["conditionId"]
    assert market.active is True


async def test_get_market_passes_condition_ids_param() -> None:
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        mock.get("/markets").mock(side_effect=_handler)
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http)
            await client.get_market("0xCOND")
    assert captured[0].url.params["condition_ids"] == "0xCOND"


async def test_get_market_returns_none_when_empty_array() -> None:
    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        mock.get("/markets").mock(return_value=httpx.Response(200, json=[]))
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http)
            result = await client.get_market("0xunknown")
    assert result is None


async def test_get_market_caches_within_ttl(
    sample_gamma_market: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(GammaApiClient, "_now", staticmethod(lambda: fixed_now))

    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        route = mock.get("/markets").mock(
            return_value=httpx.Response(200, json=[sample_gamma_market]),
        )
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http)
            await client.get_market("0xc")
            await client.get_market("0xc")
            await client.get_market("0xc")
    assert route.call_count == 1


async def test_get_market_refetches_after_ttl_expires(
    sample_gamma_market: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    # call#1 (miss) consomme 1 _now() pour stocker en cache.
    # call#2 consomme 2 _now() : 1 pour le check (déclenche expiry car +61s),
    # puis 1 pour stocker la nouvelle valeur en cache.
    times = iter([base, base + timedelta(seconds=61), base + timedelta(seconds=61)])
    monkeypatch.setattr(GammaApiClient, "_now", staticmethod(lambda: next(times)))

    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        route = mock.get("/markets").mock(
            return_value=httpx.Response(200, json=[sample_gamma_market]),
        )
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http)
            await client.get_market("0xc")  # cache miss → fetch #1
            await client.get_market("0xc")  # 61s plus tard → cache expired → fetch #2
    assert route.call_count == 2


async def test_get_market_retries_on_429() -> None:
    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        route = mock.get("/markets")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(200, json=[]),
        ]
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http)
            result = await client.get_market("0xc")
    assert result is None
    assert route.call_count == 2
