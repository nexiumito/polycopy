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


# --- Régression résolution M8 (audit 2026-05-22) ---------------------------


async def test_get_markets_by_condition_ids_uses_repeated_params_batched() -> None:
    """Params RÉPÉTÉS (jamais de comma-join), lots ≤ batch size, double passage.

    Deux régressions live 2026-05-22, toutes deux silencieuses (Gamma → []) :
    (1) le comma-join ``condition_ids=A,B`` est ignoré ; (2) le filtre par
    défaut exclut les marchés fermés → 2e passage ``closed=true``.
    """
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    cond_ids = [f"0x{i:064x}" for i in range(120)]
    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        mock.get("/markets").mock(side_effect=_handler)
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http)
            await client.get_markets_by_condition_ids(cond_ids)
    # 3 lots (50+50+20) × 2 passages (ouverts + fermés) = 6 requêtes.
    assert len(captured) == 6
    for req in captured:
        values = req.url.params.get_list("condition_ids")
        assert len(values) <= GammaApiClient.CONDITION_IDS_BATCH_SIZE
        assert all("," not in v for v in values)  # jamais de comma-join
    # 3 requêtes filtrent closed=true, 3 sont le passage "ouverts" (sans closed).
    assert sum(r.url.params.get("closed") == "true" for r in captured) == 3
    open_seen: list[str] = []
    for r in captured:
        if "closed" not in r.url.params:
            open_seen.extend(r.url.params.get_list("condition_ids"))
    assert set(open_seen) == set(cond_ids)


async def test_get_markets_by_condition_ids_includes_closed_markets(
    sample_gamma_market: dict[str, Any],
) -> None:
    """Le 2e passage ``closed=true`` ramène les marchés résolus (exclus du
    filtre par défaut) — sans lui le watcher de résolution ne voit jamais de
    marché ``closed=true`` à fermer. Fusion par condition_id."""
    open_mkt = {**sample_gamma_market, "conditionId": "0xOPEN", "closed": False}
    closed_mkt = {**sample_gamma_market, "conditionId": "0xCLOSED", "closed": True}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("closed") == "true":
            return httpx.Response(200, json=[closed_mkt])
        return httpx.Response(200, json=[open_mkt])

    with respx.mock(base_url="https://gamma-api.polymarket.com") as mock:
        mock.get("/markets").mock(side_effect=_handler)
        async with httpx.AsyncClient() as http:
            client = GammaApiClient(http)
            markets = await client.get_markets_by_condition_ids(["0xOPEN", "0xCLOSED"])
    by_cid = {m.condition_id: m for m in markets}
    assert by_cid["0xOPEN"].closed is False
    assert by_cid["0xCLOSED"].closed is True
