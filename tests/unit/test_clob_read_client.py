"""Tests du `ClobReadClient` (respx + retry + 404)."""

import httpx
import pytest
import respx
import tenacity

from polycopy.strategy.clob_read_client import ClobReadClient


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ClobReadClient._fetch.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


async def test_get_midpoint_happy_path(sample_clob_midpoint: dict[str, str]) -> None:
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/midpoint").mock(return_value=httpx.Response(200, json=sample_clob_midpoint))
        async with httpx.AsyncClient() as http:
            client = ClobReadClient(http)
            mid = await client.get_midpoint("123")
    assert mid == 0.08


async def test_get_midpoint_passes_token_id_param() -> None:
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"mid": "0.5"})

    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/midpoint").mock(side_effect=_handler)
        async with httpx.AsyncClient() as http:
            client = ClobReadClient(http)
            await client.get_midpoint("TOK_X")
    assert captured[0].url.params["token_id"] == "TOK_X"


async def test_get_midpoint_returns_none_on_404() -> None:
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/midpoint").mock(return_value=httpx.Response(404, json={"error": "no book"}))
        async with httpx.AsyncClient() as http:
            client = ClobReadClient(http)
            mid = await client.get_midpoint("dead_token")
    assert mid is None


async def test_get_midpoint_retries_on_429() -> None:
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/midpoint")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(200, json={"mid": "0.42"}),
        ]
        async with httpx.AsyncClient() as http:
            client = ClobReadClient(http)
            mid = await client.get_midpoint("123")
    assert mid == 0.42
    assert route.call_count == 2


async def test_get_midpoint_does_not_retry_404() -> None:
    """Régression 2026-05-22 : un 404 (marché résolu) ne doit PAS être retenté.

    Avant, tenacity retentait tout ``HTTPStatusError`` 5× avec backoff
    (~15 s par position résolue) → ``get_state`` gelait sur un backlog de
    centaines de positions résolues, et le snapshot PnL ne s'écrivait jamais.
    """
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/midpoint").mock(
            return_value=httpx.Response(404, json={"error": "no book"}),
        )
        async with httpx.AsyncClient() as http:
            client = ClobReadClient(http)
            mid = await client.get_midpoint("dead_token")
    assert mid is None
    assert route.call_count == 1  # une seule requête, aucun retry


async def test_get_midpoint_does_not_retry_400() -> None:
    """Tout 4xx (client error) est non-transitoire → pas de retry."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/midpoint").mock(return_value=httpx.Response(400))
        async with httpx.AsyncClient() as http:
            client = ClobReadClient(http)
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_midpoint("bad")
    assert route.call_count == 1
