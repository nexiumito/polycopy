"""Tests du `FeeRateClient` (M16) — respx + cache TTL + LRU + single-flight + fallback.

Cf. spec [docs/specs/M16-dynamic-fees-ev.md](../../docs/specs/M16-dynamic-fees-ev.md) §9.1.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
import tenacity

from polycopy.executor import fee_rate_client as frc_module
from polycopy.executor.fee_rate_client import (
    _CONSERVATIVE_FALLBACK_RATE,
    FeeRateClient,
)


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Désactive les sleeps tenacity pour des tests rapides."""
    monkeypatch.setattr(
        FeeRateClient._fetch.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


# ---------------------------------------------------------------------------
# Tests obligatoires §9.1
# ---------------------------------------------------------------------------


async def test_fee_rate_client_returns_decimal_from_bps(
    sample_fee_rate_crypto: dict[str, int],
) -> None:
    """Happy path : base_fee=1000 bps → Decimal('0.10')."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/fee-rate").mock(return_value=httpx.Response(200, json=sample_fee_rate_crypto))
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            rate = await client.get_fee_rate("ABC")
    assert rate == Decimal("0.10")


async def test_fee_rate_client_returns_zero_for_fee_free_market(
    sample_fee_rate_zero: dict[str, int],
) -> None:
    """Marché fee-free (vaste majorité Polymarket pré-rollout) → Decimal('0')."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/fee-rate").mock(return_value=httpx.Response(200, json=sample_fee_rate_zero))
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            rate = await client.get_fee_rate("FREE")
    assert rate == Decimal("0")


async def test_fee_rate_client_caches_60s(
    sample_fee_rate_crypto: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2 calls successifs au même token_id → 1 seule requête HTTP. Avancer le
    temps de 61 s → 2ᵉ fetch."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/fee-rate").mock(
            return_value=httpx.Response(200, json=sample_fee_rate_crypto)
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            r1 = await client.get_fee_rate("ABC")
            r2 = await client.get_fee_rate("ABC")
            assert route.call_count == 1
            assert r1 == r2 == Decimal("0.10")

            # Force expiration du cache : on patch directement les timestamps.
            for token_id, (rate, _) in list(client._cache.items()):
                client._cache[token_id] = (rate, datetime.now(tz=UTC) - timedelta(seconds=61))

            r3 = await client.get_fee_rate("ABC")
            assert route.call_count == 2
            assert r3 == Decimal("0.10")


async def test_fee_rate_client_fallback_on_network_error() -> None:
    """`TransportError` post-tenacity → fallback Decimal('0.018') (1.80 %)."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/fee-rate").mock(side_effect=httpx.ConnectError("connection refused"))
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            rate = await client.get_fee_rate("UNREACHABLE")
    assert rate == _CONSERVATIVE_FALLBACK_RATE
    assert rate == Decimal("0.018")


async def test_fee_rate_client_single_flight_prevents_redundant_fetches(
    sample_fee_rate_crypto: dict[str, int],
) -> None:
    """3 callers concurrents même token_id → 1 seule requête HTTP."""
    fetch_started = asyncio.Event()
    fetch_release = asyncio.Event()
    call_count = 0

    async def _delayed_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        fetch_started.set()
        await fetch_release.wait()
        return httpx.Response(200, json=sample_fee_rate_crypto)

    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/fee-rate").mock(side_effect=_delayed_handler)
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            # Lance 3 callers simultanés sur le même token_id.
            t1 = asyncio.create_task(client.get_fee_rate("RACE"))
            t2 = asyncio.create_task(client.get_fee_rate("RACE"))
            t3 = asyncio.create_task(client.get_fee_rate("RACE"))
            # Attendre que le 1er fetch soit lancé pour garantir que t2/t3
            # tombent sur le single-flight Future plutôt que sur un cache vide.
            await fetch_started.wait()
            fetch_release.set()
            results = await asyncio.gather(t1, t2, t3)

    assert call_count == 1, f"Expected 1 HTTP call, got {call_count}"
    assert results == [Decimal("0.10"), Decimal("0.10"), Decimal("0.10")]


# ---------------------------------------------------------------------------
# Tests bonus §9.1
# ---------------------------------------------------------------------------


async def test_fee_rate_client_lru_cap_eviction(
    sample_fee_rate_crypto: dict[str, int],
) -> None:
    """`cache_max=2`, 3 tokens A/B/C puis re-A → 4 requêtes (A évincé après B/C)."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/fee-rate").mock(
            return_value=httpx.Response(200, json=sample_fee_rate_crypto)
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http, cache_max=2)
            await client.get_fee_rate("A")  # cache = {A}
            await client.get_fee_rate("B")  # cache = {A, B}
            await client.get_fee_rate("C")  # cache = {B, C} (A évincé)
            await client.get_fee_rate("A")  # refetch → cache = {C, A}
    assert route.call_count == 4


async def test_fee_rate_client_400_invalid_token_returns_fallback() -> None:
    """HTTP 400 ('Invalid token id') → fallback conservateur (pas de retry)."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/fee-rate").mock(
            return_value=httpx.Response(400, json={"error": "Invalid token id"})
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            rate = await client.get_fee_rate("BAD")
    assert rate == Decimal("0.018")


async def test_fee_rate_client_404_returns_zero() -> None:
    """HTTP 404 ('fee rate not found') → Decimal('0') (marché fee-free)."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/fee-rate").mock(
            return_value=httpx.Response(404, json={"error": "fee rate not found for market"})
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            rate = await client.get_fee_rate("FEE_FREE")
    assert rate == Decimal("0")


async def test_fee_rate_client_retries_on_429(
    sample_fee_rate_crypto: dict[str, int],
) -> None:
    """HTTP 429 retried (rate limit Polymarket transient)."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/fee-rate")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(200, json=sample_fee_rate_crypto),
        ]
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            rate = await client.get_fee_rate("THROTTLED")
    assert rate == Decimal("0.10")
    assert route.call_count == 2


async def test_fee_rate_client_retries_on_503(
    sample_fee_rate_crypto: dict[str, int],
) -> None:
    """HTTP 5xx retried (server transient)."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get("/fee-rate")
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json=sample_fee_rate_crypto),
        ]
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            rate = await client.get_fee_rate("DOWN")
    assert rate == Decimal("0.10")
    assert route.call_count == 3


async def test_fee_rate_client_no_secret_leak_in_logs(
    sample_fee_rate_crypto: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aucun event structlog ne doit contenir de marker secret §8.3 spec.

    On patch le logger module-level pour capturer les events sans toucher
    à la config structlog globale (qui peut être PrintLogger en M9+)."""
    captured: list[tuple[str, dict[str, object]]] = []

    class _Recorder:
        def _record(self, level: str, event: str, **kwargs: object) -> None:
            captured.append((level, {"event": event, **kwargs}))

        def debug(self, event: str, **kwargs: object) -> None:
            self._record("debug", event, **kwargs)

        def info(self, event: str, **kwargs: object) -> None:
            self._record("info", event, **kwargs)

        def warning(self, event: str, **kwargs: object) -> None:
            self._record("warning", event, **kwargs)

        def error(self, event: str, **kwargs: object) -> None:
            self._record("error", event, **kwargs)

    monkeypatch.setattr(frc_module, "log", _Recorder())

    # Mix de réponses : 200, 400, 404, transport error.
    # Note : ConnectError sera retried 5x par tenacity (wait disabled), donc
    # on liste assez de side_effects pour couvrir.
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/fee-rate").mock(
            side_effect=[
                httpx.Response(200, json=sample_fee_rate_crypto),
                httpx.Response(404, json={"error": "fee rate not found for market"}),
                httpx.Response(400, json={"error": "Invalid token id"}),
                *[httpx.ConnectError("net down")] * 5,
            ]
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            await client.get_fee_rate("OK")
            await client.get_fee_rate("FREE")
            await client.get_fee_rate("BAD")
            await client.get_fee_rate("NET")

    secret_markers = (
        "POLYMARKET_PRIVATE_KEY",
        "TELEGRAM_BOT_TOKEN",
        "CLOB_API_SECRET",
        "REMOTE_CONTROL_TOTP_SECRET",
        "polymarket_private_key",
        "telegram_bot_token",
    )
    serialized = repr(captured)
    for marker in secret_markers:
        assert marker not in serialized, f"Secret marker {marker!r} leaked in structlog events"
    # Sanity check : on a bien capturé des events.
    assert len(captured) >= 4, f"Expected ≥4 captured events, got {len(captured)}"


async def test_fee_rate_client_inflight_dict_drained_post_fetch(
    sample_fee_rate_crypto: dict[str, int],
) -> None:
    """Le dict `_inflight` ne doit pas accumuler — pop systématique post-fetch."""
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get("/fee-rate").mock(return_value=httpx.Response(200, json=sample_fee_rate_crypto))
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            for i in range(10):
                await client.get_fee_rate(f"T{i}")
            assert len(client._inflight) == 0


async def test_fee_rate_client_module_constants() -> None:
    """Garde-fou : le fallback est bien la valeur worst-case 1.80 %."""
    assert Decimal("0.018") == frc_module._CONSERVATIVE_FALLBACK_RATE
    assert frc_module._CACHE_TTL.total_seconds() == 60.0
