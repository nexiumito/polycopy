"""Tests M8 §9.3 — ``ClobOrderbookReader`` (respx + cache TTL + LRU + 404)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
import tenacity

from polycopy.executor.clob_orderbook_reader import ClobOrderbookReader, OrderbookNotFoundError

_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "clob_orderbook_sample.json"


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ClobOrderbookReader._fetch.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


@pytest.fixture
def sample_orderbook() -> dict[str, object]:
    return json.loads(_FIXTURE_PATH.read_text())


async def test_parse_real_fixture(sample_orderbook: dict[str, object]) -> None:
    """Le parser tient face à la fixture réelle capturée."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/book").mock(return_value=httpx.Response(200, json=sample_orderbook))
        async with httpx.AsyncClient() as http:
            reader = ClobOrderbookReader(http, ttl_seconds=5)
            book = await reader.get_orderbook(
                "18690049947242812495755151360212639738977254879109748949267393375856311641700"
            )
    assert book.asset_id == sample_orderbook["asset_id"]
    assert book.raw_hash is not None
    assert len(book.asks) > 0
    # asks triés croissant
    asks_prices = [a.price for a in book.asks]
    assert asks_prices == sorted(asks_prices)
    # Decimal sur prix/size
    assert isinstance(book.asks[0].price, Decimal)
    assert isinstance(book.asks[0].size, Decimal)


async def test_cache_hits_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(ClobOrderbookReader, "_now", staticmethod(lambda: fixed))
    payload = {
        "asset_id": "T",
        "bids": [{"price": "0.4", "size": "100"}],
        "asks": [{"price": "0.6", "size": "100"}],
        "hash": "h",
    }
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/book").mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as http:
            reader = ClobOrderbookReader(http, ttl_seconds=5)
            await reader.get_orderbook("T")
            await reader.get_orderbook("T")
            await reader.get_orderbook("T")
    assert route.call_count == 1


async def test_refetches_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    base = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    times = iter(
        [
            base,
            base,
            base + timedelta(seconds=10),
            base + timedelta(seconds=10),
        ],
    )
    monkeypatch.setattr(ClobOrderbookReader, "_now", staticmethod(lambda: next(times)))
    payload = {"asset_id": "T", "bids": [], "asks": [], "hash": "h"}
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/book").mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as http:
            reader = ClobOrderbookReader(http, ttl_seconds=5)
            await reader.get_orderbook("T")
            await reader.get_orderbook("T")
    assert route.call_count == 2


async def test_lru_eviction_at_max_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(ClobOrderbookReader, "_now", staticmethod(lambda: fixed))
    payload = {"asset_id": "T", "bids": [], "asks": [], "hash": "h"}
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/book").mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as http:
            reader = ClobOrderbookReader(http, ttl_seconds=5, max_entries=3)
            for asset in ("a", "b", "c", "d", "e"):
                await reader.get_orderbook(asset)
    assert len(reader._store) == 3  # type: ignore[attr-defined]
    # LRU : 'a' et 'b' évincés (les + anciens), 'c'/'d'/'e' restants.
    assert "a" not in reader._store  # type: ignore[attr-defined]
    assert "b" not in reader._store  # type: ignore[attr-defined]


async def test_404_raises_orderbook_not_found_error() -> None:
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/book").mock(return_value=httpx.Response(404, json={}))
        async with httpx.AsyncClient() as http:
            reader = ClobOrderbookReader(http, ttl_seconds=5)
            with pytest.raises(OrderbookNotFoundError):
                await reader.get_orderbook("missing")


async def test_retry_on_429_then_success() -> None:
    payload = {"asset_id": "T", "bids": [], "asks": [], "hash": "h"}
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/book")
        route.side_effect = [httpx.Response(429), httpx.Response(200, json=payload)]
        async with httpx.AsyncClient() as http:
            reader = ClobOrderbookReader(http, ttl_seconds=5)
            book = await reader.get_orderbook("T")
    assert book.asset_id == "T"
    assert route.call_count == 2


async def test_bids_sorted_descending() -> None:
    payload = {
        "asset_id": "T",
        "bids": [
            {"price": "0.4", "size": "10"},
            {"price": "0.5", "size": "10"},
            {"price": "0.45", "size": "10"},
        ],
        "asks": [],
        "hash": "h",
    }
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/book").mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as http:
            reader = ClobOrderbookReader(http, ttl_seconds=5)
            book = await reader.get_orderbook("T")
    assert [float(b.price) for b in book.bids] == [0.5, 0.45, 0.4]
