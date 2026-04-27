"""Tests du `FeeRateClient` V2 (M18 ME.3) — `/clob-markets/{cid}` endpoint.

Cf. spec M18 §5.3 + §9.1.

Le contrat M16 ``get_fee_rate(token_id) -> Decimal`` est préservé comme alias
deprecated — backward-compat 1 version (M19+ drop).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
import tenacity

from polycopy.executor import fee_rate_client as frc_module
from polycopy.executor.fee_rate_client import FeeQuote, FeeRateClient


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Désactive les sleeps tenacity pour des tests rapides."""
    monkeypatch.setattr(
        FeeRateClient._fetch_v2.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


# ---------------------------------------------------------------------------
# ME.3 — FeeQuote DTO
# ---------------------------------------------------------------------------


def test_fee_quote_dto_validates_decimal_and_int() -> None:
    """`FeeQuote(rate=Decimal, exponent=int)` OK + bornes [0, 4]."""
    from pydantic import ValidationError

    q = FeeQuote(rate=Decimal("0.072"), exponent=1)
    assert q.rate == Decimal("0.072")
    assert q.exponent == 1

    with pytest.raises(ValidationError):
        FeeQuote(rate=Decimal("0"), exponent=-1)
    with pytest.raises(ValidationError):
        FeeQuote(rate=Decimal("0"), exponent=5)


def test_fee_quote_zero_classmethod() -> None:
    """`FeeQuote.zero()` = `(Decimal('0'), 0)`."""
    q = FeeQuote.zero()
    assert q.rate == Decimal("0")
    assert q.exponent == 0


def test_fee_quote_conservative_fallback_classmethod() -> None:
    """`FeeQuote.conservative_fallback()` = `(0.018, 1)` — worst-case 1.80% à p=0.5."""
    q = FeeQuote.conservative_fallback()
    assert q.rate == Decimal("0.018")
    assert q.exponent == 1


# ---------------------------------------------------------------------------
# ME.3 — get_fee_quote V2 path nominal (avec condition_id)
# ---------------------------------------------------------------------------


async def test_get_fee_quote_v2_endpoint_with_condition_id(
    sample_clob_v2_market_crypto: dict[str, Any],
) -> None:
    """Path nominal V2 : `condition_id` fourni → call direct `/clob-markets/{cid}`.

    Fixture crypto fee-enabled (`fd:{r:0.072,e:1,to:true}`) → FeeQuote(0.072, 1).
    """
    cid = sample_clob_v2_market_crypto["cid"]
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get(f"/clob-markets/{cid}").mock(
            return_value=httpx.Response(200, json=sample_clob_v2_market_crypto)
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            quote = await client.get_fee_quote("ABC", condition_id=cid)
    assert quote.rate == Decimal("0.072")
    assert quote.exponent == 1
    assert route.call_count == 1


async def test_get_fee_quote_fee_free_market_returns_zero(
    sample_clob_v2_market_fee_free: dict[str, Any],
) -> None:
    """Fixture politics fee-free (sans `fd`) → FeeQuote.zero()."""
    cid = sample_clob_v2_market_fee_free["cid"]
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get(f"/clob-markets/{cid}").mock(
            return_value=httpx.Response(200, json=sample_clob_v2_market_fee_free)
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            quote = await client.get_fee_quote("TOK", condition_id=cid)
    assert quote == FeeQuote.zero()


async def test_get_fee_quote_404_returns_zero() -> None:
    """HTTP 404 sur `/clob-markets/{cid}` → FeeQuote.zero() (marché inconnu)."""
    cid = "0xunknownmarket"
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get(f"/clob-markets/{cid}").mock(
            return_value=httpx.Response(404, json={"error": "market not found"})
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            quote = await client.get_fee_quote("TOK", condition_id=cid)
    assert quote == FeeQuote.zero()


async def test_get_fee_quote_5xx_returns_conservative_fallback() -> None:
    """HTTP 5xx post-tenacity → FeeQuote.conservative_fallback()."""
    cid = "0xdownmarket"
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get(f"/clob-markets/{cid}").mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            quote = await client.get_fee_quote("TOK", condition_id=cid)
    assert quote == FeeQuote.conservative_fallback()


async def test_get_fee_quote_network_error_returns_conservative_fallback() -> None:
    """`TransportError` post-tenacity → FeeQuote.conservative_fallback()."""
    cid = "0xunreachable"
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get(f"/clob-markets/{cid}").mock(side_effect=httpx.ConnectError("conn refused"))
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            quote = await client.get_fee_quote("TOK", condition_id=cid)
    assert quote == FeeQuote.conservative_fallback()


async def test_get_fee_quote_caches_60s(
    sample_clob_v2_market_crypto: dict[str, Any],
) -> None:
    """2 calls successifs même cid → 1 seule requête. +61s → re-fetch."""
    cid = sample_clob_v2_market_crypto["cid"]
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get(f"/clob-markets/{cid}").mock(
            return_value=httpx.Response(200, json=sample_clob_v2_market_crypto)
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            q1 = await client.get_fee_quote("TOK", condition_id=cid)
            q2 = await client.get_fee_quote("TOK", condition_id=cid)
            assert q1 == q2
            assert route.call_count == 1

            # Force expiration du cache.
            for k, (q, _) in list(client._cache.items()):
                client._cache[k] = (q, datetime.now(tz=UTC) - timedelta(seconds=61))
            await client.get_fee_quote("TOK", condition_id=cid)
            assert route.call_count == 2


async def test_get_fee_quote_single_flight_prevents_redundant_fetches(
    sample_clob_v2_market_crypto: dict[str, Any],
) -> None:
    """3 callers concurrents même cid → 1 seule requête HTTP."""
    cid = sample_clob_v2_market_crypto["cid"]
    fetch_started = asyncio.Event()
    fetch_release = asyncio.Event()
    call_count = 0

    async def _delayed_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        fetch_started.set()
        await fetch_release.wait()
        return httpx.Response(200, json=sample_clob_v2_market_crypto)

    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get(f"/clob-markets/{cid}").mock(side_effect=_delayed_handler)
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            t1 = asyncio.create_task(client.get_fee_quote("A", condition_id=cid))
            t2 = asyncio.create_task(client.get_fee_quote("B", condition_id=cid))
            t3 = asyncio.create_task(client.get_fee_quote("C", condition_id=cid))
            await fetch_started.wait()
            fetch_release.set()
            results = await asyncio.gather(t1, t2, t3)
    assert call_count == 1
    assert all(r.rate == Decimal("0.072") for r in results)


async def test_get_fee_quote_lru_cap_eviction(
    sample_clob_v2_market_crypto: dict[str, Any],
) -> None:
    """`cache_max=2` : 3 cids → A évincé."""
    template = sample_clob_v2_market_crypto.copy()
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        for cid in ("0xA", "0xB", "0xC"):
            payload = {**template, "cid": cid}
            mock.get(f"/clob-markets/{cid}").mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http, cache_max=2)
            await client.get_fee_quote("t", condition_id="0xA")
            await client.get_fee_quote("t", condition_id="0xB")
            await client.get_fee_quote("t", condition_id="0xC")
    assert "0xA" not in client._cache
    assert set(client._cache.keys()) == {"0xB", "0xC"}


async def test_get_fee_quote_retries_on_429(
    sample_clob_v2_market_crypto: dict[str, Any],
) -> None:
    """HTTP 429 retried (rate limit Polymarket transient)."""
    cid = sample_clob_v2_market_crypto["cid"]
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        route = mock.get(f"/clob-markets/{cid}")
        route.side_effect = [
            httpx.Response(429),
            httpx.Response(200, json=sample_clob_v2_market_crypto),
        ]
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            quote = await client.get_fee_quote("t", condition_id=cid)
    assert quote.rate == Decimal("0.072")
    assert route.call_count == 2


# ---------------------------------------------------------------------------
# ME.3 — Fallback Gamma path (condition_id None)
# ---------------------------------------------------------------------------


async def test_get_fee_quote_fallback_gamma_when_no_condition_id(
    sample_clob_v2_market_crypto: dict[str, Any],
) -> None:
    """Sans `condition_id` → fallback Gamma `/markets-by-token/{token_id}`."""
    cid = sample_clob_v2_market_crypto["cid"]
    token_id = "TOK-XYZ"

    with (
        respx.mock(base_url="https://gamma-api.polymarket.com") as gamma_mock,
        respx.mock(base_url="https://clob.polymarket.com") as clob_mock,
    ):
        gamma_mock.get(f"/markets-by-token/{token_id}").mock(
            return_value=httpx.Response(200, json={"conditionId": cid})
        )
        clob_mock.get(f"/clob-markets/{cid}").mock(
            return_value=httpx.Response(200, json=sample_clob_v2_market_crypto)
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            quote = await client.get_fee_quote(token_id)
    assert quote.rate == Decimal("0.072")
    # Cache mapping token → cid.
    assert client._token_to_cid[token_id] == cid


# ---------------------------------------------------------------------------
# ME.3 — Backward-compat M16 alias deprecated `get_fee_rate`
# ---------------------------------------------------------------------------


async def test_get_fee_rate_legacy_alias_returns_quote_rate_with_warning(
    sample_clob_v2_market_crypto: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_fee_rate(token)` retourne `quote.rate` + warning structlog 1× par token."""
    cid = sample_clob_v2_market_crypto["cid"]
    token_id = "LEGACY-TOK"

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

    with (
        respx.mock(base_url="https://gamma-api.polymarket.com") as gamma_mock,
        respx.mock(base_url="https://clob.polymarket.com") as clob_mock,
    ):
        gamma_mock.get(f"/markets-by-token/{token_id}").mock(
            return_value=httpx.Response(200, json={"conditionId": cid})
        )
        clob_mock.get(f"/clob-markets/{cid}").mock(
            return_value=httpx.Response(200, json=sample_clob_v2_market_crypto)
        )
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            r1 = await client.get_fee_rate(token_id)
            r2 = await client.get_fee_rate(token_id)

    assert r1 == Decimal("0.072")
    assert r1 == r2
    deprecated_events = [
        e for _, e in captured if e["event"] == "fee_rate_client_get_fee_rate_deprecated"
    ]
    # Doit être exactement 1 (LRU dedup).
    assert len(deprecated_events) == 1


# ---------------------------------------------------------------------------
# ME.3 — Modules constants + invariants
# ---------------------------------------------------------------------------


async def test_fee_rate_client_module_constants() -> None:
    """Garde-fou : le fallback est bien la valeur worst-case 1.80 %."""
    assert Decimal("0.018") == frc_module._CONSERVATIVE_FALLBACK_RATE
    assert frc_module._CACHE_TTL.total_seconds() == 60.0


async def test_fee_rate_client_inflight_dict_drained_post_fetch(
    sample_clob_v2_market_crypto: dict[str, Any],
) -> None:
    """Le dict `_inflight` ne doit pas accumuler — pop systématique post-fetch."""
    template = sample_clob_v2_market_crypto.copy()
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        for i in range(10):
            cid = f"0x{i:064x}"
            payload = {**template, "cid": cid}
            mock.get(f"/clob-markets/{cid}").mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            for i in range(10):
                cid = f"0x{i:064x}"
                await client.get_fee_quote("t", condition_id=cid)
            assert len(client._inflight) == 0


async def test_fee_rate_client_no_secret_leak_in_logs(
    sample_clob_v2_market_crypto: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aucun event structlog ne doit contenir de marker secret §8.7 spec."""
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

    cid_ok = "0xokmarket"
    cid_404 = "0xnotfound"
    cid_down = "0xdownmarket"
    with respx.mock(base_url="https://clob.polymarket.com") as mock:
        mock.get(f"/clob-markets/{cid_ok}").mock(
            return_value=httpx.Response(200, json=sample_clob_v2_market_crypto)
        )
        mock.get(f"/clob-markets/{cid_404}").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        mock.get(f"/clob-markets/{cid_down}").mock(side_effect=[httpx.ConnectError("net down")] * 5)
        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http)
            await client.get_fee_quote("t1", condition_id=cid_ok)
            await client.get_fee_quote("t2", condition_id=cid_404)
            await client.get_fee_quote("t3", condition_id=cid_down)

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
        assert marker not in serialized, f"Secret marker {marker!r} leaked"
    assert len(captured) >= 3, f"Expected ≥3 captured events, got {len(captured)}"
