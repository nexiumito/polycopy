"""Tests unitaires de ``ClobMarketWSClient`` (M11 §9.3.A).

Mock serveur local via ``websockets.serve`` — pas d'accès réseau externe.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets

from polycopy.config import Settings
from polycopy.strategy.clob_ws_client import (
    BestBidAskEvent,
    BookSnapshot,
    ClobMarketWSClient,
    PriceChangeEvent,
    _compute_mid_from_book,
)

# pytestmark = pytest.mark.asyncio  # asyncio-mode auto déjà actif


def _make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "target_wallets": ["0xabc"],
        "strategy_clob_ws_max_subscribed": 500,
        "strategy_clob_ws_inactivity_unsub_seconds": 300,
        "strategy_clob_ws_health_check_seconds": 30,
    }
    base.update(overrides)
    return Settings(**base)


async def _serve_mock(
    received: list[dict[str, Any]],
    to_send: list[str | dict[str, Any]],
    port_holder: list[int],
    close_after: float | None = None,
) -> None:
    """Lance un mini-serveur WS qui enregistre les messages reçus, pousse ``to_send``."""

    async def handler(ws: websockets.WebSocketServerProtocol) -> None:
        async def reader() -> None:
            try:
                async for msg in ws:
                    try:
                        received.append(json.loads(msg))
                    except json.JSONDecodeError:
                        received.append({"_raw": msg})
            except websockets.ConnectionClosed:
                pass

        asyncio.create_task(reader())
        for item in to_send:
            payload = item if isinstance(item, str) else json.dumps(item)
            await ws.send(payload)
            await asyncio.sleep(0.01)
        if close_after is not None:
            await asyncio.sleep(close_after)
            await ws.close()
        else:
            await asyncio.sleep(0.5)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    port_holder.append(port)
    try:
        await asyncio.sleep(1.5)
    finally:
        server.close()
        await server.wait_closed()


def test_compute_mid_from_book_basic() -> None:
    book = BookSnapshot.model_validate(
        {
            "event_type": "book",
            "asset_id": "a",
            "market": "m",
            "bids": [{"price": "0.07", "size": "100"}, {"price": "0.06", "size": "200"}],
            "asks": [{"price": "0.08", "size": "150"}, {"price": "0.09", "size": "300"}],
            "timestamp": "1",
        },
    )
    assert _compute_mid_from_book(book) == pytest.approx(0.075)


def test_compute_mid_from_book_invalid_returns_none() -> None:
    book = BookSnapshot.model_validate(
        {
            "event_type": "book",
            "asset_id": "a",
            "market": "m",
            "bids": [{"price": "0.09", "size": "100"}],
            "asks": [{"price": "0.07", "size": "100"}],  # crossed
            "timestamp": "1",
        },
    )
    assert _compute_mid_from_book(book) is None


async def test_ws_cache_price_change_updates_mid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un message ``price_change`` avec best_bid/best_ask peuple le cache."""
    settings = _make_settings()
    client = ClobMarketWSClient(settings)
    event = PriceChangeEvent.model_validate(
        {
            "event_type": "price_change",
            "market": "m",
            "price_changes": [
                {
                    "asset_id": "tok1",
                    "price": "0.08",
                    "size": "1500",
                    "side": "SELL",
                    "best_bid": "0.07",
                    "best_ask": "0.08",
                },
            ],
            "timestamp": "0",
        },
    )
    client._apply_price_change(event)
    mid = await client.get_mid_price("tok1")
    assert mid == pytest.approx(0.075)


async def test_ws_cache_best_bid_ask_updates_mid() -> None:
    settings = _make_settings()
    client = ClobMarketWSClient(settings)
    event = BestBidAskEvent.model_validate(
        {
            "event_type": "best_bid_ask",
            "market": "m",
            "asset_id": "tok_x",
            "best_bid": "0.30",
            "best_ask": "0.32",
            "timestamp": "1",
        },
    )
    client._apply_best_bid_ask(event)
    mid = await client.get_mid_price("tok_x")
    assert mid == pytest.approx(0.31)


async def test_ws_cache_stale_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si pas de push depuis > _CACHE_STALE_SECONDS, ``get_mid_price`` retourne None."""
    from polycopy.strategy import clob_ws_client as module

    monkeypatch.setattr(module, "_CACHE_STALE_SECONDS", 0.0)
    settings = _make_settings()
    client = ClobMarketWSClient(settings)
    client._store("tok_stale", 0.5)
    assert await client.get_mid_price("tok_stale") is None


async def test_ws_get_mid_price_unknown_token_returns_none() -> None:
    settings = _make_settings()
    client = ClobMarketWSClient(settings)
    assert await client.get_mid_price("unknown") is None


async def test_ws_max_subscribed_cap_enforced_via_lru() -> None:
    """Cap dur: une 4ᵉ sub avec max=3 évince la plus ancienne.

    Le minimum Pydantic pour ``STRATEGY_CLOB_WS_MAX_SUBSCRIBED`` est 50 ; on
    force la valeur post-construction pour tester la logique LRU isolément.
    """
    settings = _make_settings()
    client = ClobMarketWSClient(settings)
    client._max_subscribed = 3  # bypass Pydantic bound (test-only)
    for tok in ("a", "b", "c"):
        await client.subscribe(tok)
    assert set(client._subscribed.keys()) == {"a", "b", "c"}
    await client.subscribe("d")  # évince 'a' (plus ancien)
    assert "a" not in client._subscribed
    assert {"b", "c", "d"} == set(client._subscribed.keys())


async def test_ws_status_transitions_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une transition up→down déclenche un ``ws_connection_status_change``."""
    logs: list[tuple[str, dict[str, Any]]] = []

    class _Spy:
        def info(self, event: str, **kwargs: Any) -> None:
            logs.append((event, kwargs))

        def warning(self, event: str, **kwargs: Any) -> None:
            logs.append((event, kwargs))

        def exception(self, event: str, **kwargs: Any) -> None:
            logs.append((event, kwargs))

        def debug(self, event: str, **kwargs: Any) -> None:
            pass

    from polycopy.strategy import clob_ws_client as module

    monkeypatch.setattr(module, "log", _Spy())
    settings = _make_settings()
    client = ClobMarketWSClient(settings)
    client._transition_status("up")
    client._transition_status("down")
    events = [name for name, _ in logs]
    assert events.count("ws_connection_status_change") == 2


async def test_ws_end_to_end_connect_subscribe_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connecte au mock, envoie un price_change, vérifie que le cache se peuple."""
    received: list[dict[str, Any]] = []
    port_holder: list[int] = []
    to_send = [
        {
            "event_type": "price_change",
            "market": "m",
            "price_changes": [
                {
                    "asset_id": "tok_e2e",
                    "price": "0.08",
                    "size": "100",
                    "side": "SELL",
                    "best_bid": "0.07",
                    "best_ask": "0.08",
                },
            ],
            "timestamp": "0",
        },
    ]

    server_task = asyncio.create_task(_serve_mock(received, to_send, port_holder))
    # Attendre que le port soit publié.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if port_holder:
            break
    assert port_holder, "mock server failed to bind"
    port = port_holder[0]

    settings = _make_settings(
        strategy_clob_ws_url=f"ws://127.0.0.1:{port}/ws/market",
    )
    client = ClobMarketWSClient(settings)
    stop = asyncio.Event()

    async def _runner() -> None:
        await client.run(stop)

    run_task = asyncio.create_task(_runner())
    try:
        # Attend la connexion
        for _ in range(60):
            await asyncio.sleep(0.03)
            if client.status == "up":
                break
        assert client.status == "up", f"WS did not connect (status={client.status})"

        await client.subscribe("tok_e2e")
        # Attend au moins 1s que le message push + parsing soit traité.
        for _ in range(50):
            await asyncio.sleep(0.03)
            if "tok_e2e" in client._cache:
                break
        mid = await client.get_mid_price("tok_e2e")
        assert mid == pytest.approx(0.075)
    finally:
        stop.set()
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)
