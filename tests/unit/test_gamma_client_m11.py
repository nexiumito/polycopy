"""Tests M11 du cache adaptatif ``GammaApiClient`` (§9.3.C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
import tenacity

from polycopy.config import Settings
from polycopy.strategy.gamma_client import GammaApiClient


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        GammaApiClient._fetch.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"target_wallets": []}
    base.update(overrides)
    return Settings(**base)


def _active_market() -> dict[str, Any]:
    return {
        "id": "m1",
        "conditionId": "0xc1",
        "active": True,
        "closed": False,
        "archived": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "liquidityClob": 5000.0,
        "clobTokenIds": "[]",
        "outcomes": "[]",
        "outcomePrices": "[]",
    }


def _resolved_market() -> dict[str, Any]:
    m = _active_market()
    m["closed"] = True
    return m


async def test_gamma_client_uses_adaptive_ttl_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marché résolu → TTL sentinel → pas de refetch même après 1h."""
    settings = _settings(strategy_gamma_adaptive_cache_enabled=True)
    base = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    # 2 appels : check (si cache), check+insert si miss. M2 pattern.
    times = iter(
        [
            base,  # insert cached_at
            base + timedelta(hours=1),  # check again after 1h
        ],
    )
    monkeypatch.setattr(GammaApiClient, "_now", staticmethod(lambda: next(times)))

    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        route = mock.get("/markets").mock(
            return_value=httpx.Response(200, json=[_resolved_market()]),
        )
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http, settings=settings)
            await client.get_market("0xc1")  # miss → fetch #1, insert
            await client.get_market("0xc1")  # 1h later → TTL sentinel → HIT
    assert route.call_count == 1


async def test_gamma_client_fallback_to_uniform_ttl_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off → TTL 60s uniforme M2 (HIT après 30s même si résolu)."""
    settings = _settings(strategy_gamma_adaptive_cache_enabled=False)
    base = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    times = iter(
        [
            base,  # insert cached_at
            base + timedelta(seconds=30),  # check, 30s < 60 → HIT
        ],
    )
    monkeypatch.setattr(GammaApiClient, "_now", staticmethod(lambda: next(times)))

    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        route = mock.get("/markets").mock(
            return_value=httpx.Response(200, json=[_resolved_market()]),
        )
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http, settings=settings)
            await client.get_market("0xc1")
            await client.get_market("0xc1")
    assert route.call_count == 1


async def test_gamma_client_counts_hits_and_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Les compteurs ``_hits`` / ``_misses`` s'incrémentent correctement."""
    settings = _settings()
    fixed = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(GammaApiClient, "_now", staticmethod(lambda: fixed))

    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        mock.get("/markets").mock(
            return_value=httpx.Response(200, json=[_active_market()]),
        )
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http, settings=settings)
            await client.get_market("0xc1")  # miss
            await client.get_market("0xc1")  # hit
            await client.get_market("0xc1")  # hit
    assert client._misses == 1
    assert client._hits == 2
