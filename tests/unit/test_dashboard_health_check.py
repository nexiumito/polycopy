"""Tests respx du health check Gamma + Data API (M6 §4)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import respx

from polycopy.dashboard.health_check import (
    DATA_API_URL,
    GAMMA_URL,
    ExternalHealthChecker,
    ExternalHealthSnapshot,
)


@pytest_asyncio.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.mark.asyncio
async def test_unknown_snapshot_initial() -> None:
    snap = ExternalHealthSnapshot.unknown()
    assert snap.gamma_status == "unknown"
    assert snap.data_api_status == "unknown"
    assert snap.gamma_latency_ms is None


@pytest.mark.asyncio
async def test_check_ok_both(http_client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.head(GAMMA_URL).respond(200)
        router.head(DATA_API_URL).respond(200)
        checker = ExternalHealthChecker(http_client)
        snap = await checker.check()

    assert snap.gamma_status == "ok"
    assert snap.data_api_status == "ok"
    assert snap.gamma_latency_ms is not None
    assert snap.gamma_latency_ms >= 0


@pytest.mark.asyncio
async def test_check_gamma_timeout(http_client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.head(GAMMA_URL).mock(side_effect=httpx.TimeoutException("timeout"))
        router.head(DATA_API_URL).respond(200)
        checker = ExternalHealthChecker(http_client)
        snap = await checker.check()

    assert snap.gamma_status == "degraded"
    assert snap.gamma_latency_ms is None
    assert snap.data_api_status == "ok"


@pytest.mark.asyncio
async def test_check_data_api_5xx_marked_degraded(http_client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.head(GAMMA_URL).respond(200)
        router.head(DATA_API_URL).respond(503)
        checker = ExternalHealthChecker(http_client)
        snap = await checker.check()

    assert snap.gamma_status == "ok"
    assert snap.data_api_status == "degraded"
    assert snap.data_api_latency_ms is not None  # latency capturée même sur 503


@pytest.mark.asyncio
async def test_head_405_falls_back_to_get(http_client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.head(GAMMA_URL).respond(405)
        router.get(GAMMA_URL).respond(200)
        router.head(DATA_API_URL).respond(200)
        checker = ExternalHealthChecker(http_client)
        snap = await checker.check()

    assert snap.gamma_status == "ok"


@pytest.mark.asyncio
async def test_cache_ttl_avoids_extra_calls(http_client: httpx.AsyncClient) -> None:
    """2 ``check()`` consécutifs en < TTL ne refresh qu'1 fois côté réseau."""
    with respx.mock(assert_all_called=False) as router:
        gamma_route = router.head(GAMMA_URL).respond(200)
        data_route = router.head(DATA_API_URL).respond(200)
        checker = ExternalHealthChecker(http_client, cache_ttl_seconds=60.0)

        await checker.check()
        await checker.check()
        await checker.check()

    assert gamma_route.call_count == 1
    assert data_route.call_count == 1


@pytest.mark.asyncio
async def test_cache_refresh_after_ttl(http_client: httpx.AsyncClient) -> None:
    """Avec TTL≈0 on refresh à chaque call."""
    with respx.mock(assert_all_called=False) as router:
        gamma_route = router.head(GAMMA_URL).respond(200)
        data_route = router.head(DATA_API_URL).respond(200)
        # TTL négatif : ``_is_fresh`` toujours False.
        checker = ExternalHealthChecker(http_client, cache_ttl_seconds=-1.0)

        await checker.check()
        await checker.check()

    assert gamma_route.call_count == 2
    assert data_route.call_count == 2


@pytest.mark.asyncio
async def test_lock_serializes_concurrent_refreshes(http_client: httpx.AsyncClient) -> None:
    """N coroutines parallèles → 1 seul refresh (les autres prennent le cache frais)."""
    import asyncio

    with respx.mock(assert_all_called=False) as router:
        gamma_route = router.head(GAMMA_URL).respond(200)
        data_route = router.head(DATA_API_URL).respond(200)
        checker = ExternalHealthChecker(http_client, cache_ttl_seconds=60.0)

        await asyncio.gather(*(checker.check() for _ in range(10)))

    assert gamma_route.call_count == 1
    assert data_route.call_count == 1
