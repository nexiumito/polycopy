"""Tests M11 §9.3.D — route ``/latency`` + query ``compute_latency_percentiles``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard import queries
from polycopy.dashboard.queries import _percentile, compute_latency_percentiles
from polycopy.dashboard.routes import build_app
from polycopy.storage.models import TradeLatencySample


@pytest_asyncio.fixture
async def dashboard_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    settings = Settings(_env_file=None, target_wallets=[])  # type: ignore[call-arg]
    app = build_app(session_factory, settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    samples: list[tuple[str, float, datetime]],
) -> None:
    async with session_factory() as session:
        for stage, ms, ts in samples:
            session.add(
                TradeLatencySample(
                    trade_id="tid",
                    stage_name=stage,
                    duration_ms=ms,
                    timestamp=ts,
                ),
            )
        await session.commit()


def test_percentile_helper_basic() -> None:
    assert _percentile([], 0.5) == 0.0
    assert _percentile([10.0], 0.5) == 10.0
    # nearest-rank (simple)
    samples = sorted([1.0, 2.0, 3.0, 4.0, 10.0])
    assert _percentile(samples, 0.50) == 3.0
    assert _percentile(samples, 0.95) == 10.0


async def test_compute_latency_percentiles_on_empty_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await compute_latency_percentiles(
        session_factory,
        since=timedelta(hours=24),
    )
    for stats in result.values():
        assert stats["p50"] == 0.0
        assert stats["count"] == 0.0


async def test_compute_latency_percentiles_with_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(tz=UTC)
    samples: list[tuple[str, float, datetime]] = [
        ("watcher_detected_ms", float(v), now) for v in range(1, 11)
    ]
    await _seed(session_factory, samples)
    result = await compute_latency_percentiles(session_factory, since=timedelta(hours=1))
    stats = result["watcher_detected_ms"]
    assert stats["count"] == 10.0
    assert stats["p50"] == 6.0  # index int(0.5*10)=5 → sorted[5]=6
    assert stats["p95"] == 10.0
    assert stats["p99"] == 10.0


@pytest.mark.asyncio
async def test_latency_page_renders(
    dashboard_client: AsyncClient,
) -> None:
    res = await dashboard_client.get("/latency")
    assert res.status_code == 200
    body = res.text
    assert "Latence pipeline" in body or "latency-chart" in body
    assert "watcher_detected_ms" in body


@pytest.mark.asyncio
async def test_latency_chart_init_is_deferred(
    dashboard_client: AsyncClient,
) -> None:
    """Garde-fou régression : l'init Chart.js doit attendre DOMContentLoaded.

    Sinon l'IIFE inline s'exécute avant que le script `defer` Chart.js soit
    chargé → Chart is undefined → canvas vide (bug observé en dry-run v0.6.0).
    """
    res = await dashboard_client.get("/latency")
    assert res.status_code == 200
    assert "DOMContentLoaded" in res.text


@pytest.mark.asyncio
async def test_latency_page_respects_since_filter(
    dashboard_client: AsyncClient,
) -> None:
    res_1h = await dashboard_client.get("/latency?since=1h")
    res_24h = await dashboard_client.get("/latency?since=24h")
    assert res_1h.status_code == 200
    assert res_24h.status_code == 200
    # Both render; the filter chip marker indicates active selection.
    assert "filter-chip" in res_1h.text
    assert "filter-chip" in res_24h.text


def test_compute_latency_percentiles_preserves_stage_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Même DB vide, l'ordre des 6 stages standards est garanti."""
    # noqa — test synchrone utilitaire (pas besoin de fixture session)
    expected = [
        "watcher_detected_ms",
        "strategy_enriched_ms",
        "strategy_filtered_ms",
        "strategy_sized_ms",
        "strategy_risk_checked_ms",
        "executor_submitted_ms",
    ]
    assert list(queries._LATENCY_STAGES_ORDER) == expected
