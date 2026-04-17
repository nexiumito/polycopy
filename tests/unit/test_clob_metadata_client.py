"""Tests du `ClobMetadataClient` (tick-size + cache + retry)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
import tenacity

from polycopy.executor.clob_metadata_client import ClobMetadataClient


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ClobMetadataClient._fetch.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


async def test_get_tick_size_happy_path(sample_tick_size: dict[str, float]) -> None:
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/tick-size").mock(return_value=httpx.Response(200, json=sample_tick_size))
        async with httpx.AsyncClient() as http:
            client = ClobMetadataClient(http)
            ts = await client.get_tick_size("123")
    assert ts == 0.01


async def test_get_tick_size_passes_token_id() -> None:
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"minimum_tick_size": 0.001})

    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/tick-size").mock(side_effect=_handler)
        async with httpx.AsyncClient() as http:
            client = ClobMetadataClient(http)
            await client.get_tick_size("TOK_X")
    assert captured[0].url.params["token_id"] == "TOK_X"


async def test_get_tick_size_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(ClobMetadataClient, "_now", staticmethod(lambda: fixed_now))

    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/tick-size").mock(
            return_value=httpx.Response(200, json={"minimum_tick_size": 0.01}),
        )
        async with httpx.AsyncClient() as http:
            client = ClobMetadataClient(http)
            await client.get_tick_size("123")
            await client.get_tick_size("123")
            await client.get_tick_size("123")
    assert route.call_count == 1


async def test_get_tick_size_refetches_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    times = iter([base, base + timedelta(minutes=6), base + timedelta(minutes=6)])
    monkeypatch.setattr(ClobMetadataClient, "_now", staticmethod(lambda: next(times)))

    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/tick-size").mock(
            return_value=httpx.Response(200, json={"minimum_tick_size": 0.01}),
        )
        async with httpx.AsyncClient() as http:
            client = ClobMetadataClient(http)
            await client.get_tick_size("123")
            await client.get_tick_size("123")
    assert route.call_count == 2


async def test_get_tick_size_retries_on_429() -> None:
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/tick-size")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(200, json={"minimum_tick_size": 0.01}),
        ]
        async with httpx.AsyncClient() as http:
            client = ClobMetadataClient(http)
            ts = await client.get_tick_size("123")
    assert ts == 0.01
    assert route.call_count == 2
