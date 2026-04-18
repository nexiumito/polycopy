"""Tests d'invariants structurels des templates M6 (pas de pixel-snapshot)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard.routes import build_app
from polycopy.storage.models import MyOrder, TargetTrader, TraderEvent


@pytest_asyncio.fixture
async def m6_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        dry_run=True,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_home_renders_kpis_and_lucide_icons(m6_client: AsyncClient) -> None:
    res = await m6_client.get("/home")
    assert res.status_code == 200
    body = res.text
    # KPI cards : 4 titres bien présents
    for title in ("Total USDC", "Drawdown", "Positions ouvertes", "Trades détectés (24 h)"):
        assert title in body, f"KPI '{title}' absent du rendering /home"
    # Au moins 1 icône Lucide
    assert "data-lucide=" in body
    # Sidebar
    assert 'id="sidebar"' in body
    # DRY-RUN badge (settings.dry_run=true)
    assert "DRY-RUN" in body


@pytest.mark.asyncio
async def test_home_loads_tailwind_and_inter(m6_client: AsyncClient) -> None:
    res = await m6_client.get("/home")
    body = res.text
    assert "cdn.tailwindcss.com" in body
    assert "fonts.googleapis.com/css2" in body
    assert "Inter" in body


@pytest.mark.asyncio
async def test_detections_has_infinite_scroll_pattern(
    m6_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Insère >= limit pour déclencher la sentinelle
    from polycopy.storage.dtos import DetectedTradeDTO
    from polycopy.storage.repositories import DetectedTradeRepository

    repo = DetectedTradeRepository(session_factory)
    for i in range(60):
        await repo.insert_if_new(
            DetectedTradeDTO(
                tx_hash=f"0xtx{i}",
                target_wallet="0xwallet",
                condition_id="0xcond",
                asset_id="1",
                side="BUY",
                size=1.0,
                usdc_size=1.0,
                price=0.5,
                timestamp=datetime.now(tz=UTC),
                raw_json={},
            ),
        )
    res = await m6_client.get("/partials/detections-rows?limit=50")
    assert res.status_code == 200
    assert 'hx-trigger="revealed' in res.text


@pytest.mark.asyncio
async def test_traders_renders_score_gauge(
    m6_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            TargetTrader(
                wallet_address="0xtraderscore",
                status="active",
                pinned=False,
                score=0.7,
                scoring_version="v1",
            ),
        )
        await session.commit()
    res = await m6_client.get("/partials/traders-rows")
    assert res.status_code == 200
    body = res.text
    assert "<svg" in body
    assert "stroke-dasharray" in body
    assert 'data-lucide="pin"' not in body or "0xtraderscore" in body  # juste que les rows rendent


@pytest.mark.asyncio
async def test_pnl_renders_chart_canvas(m6_client: AsyncClient) -> None:
    res = await m6_client.get("/pnl")
    assert res.status_code == 200
    body = res.text
    assert 'id="pnl-chart"' in body
    # Toggle stub réel/dry-run préparé
    assert "dry-run (M8)" in body


@pytest.mark.asyncio
async def test_pnl_milestones_rendered_when_present(
    m6_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            TraderEvent(
                wallet_address="0xkill",
                event_type="kill_switch",
                at=datetime.now(tz=UTC),
            ),
        )
        await session.commit()
    res = await m6_client.get("/pnl?since=24h")
    body = res.text
    assert "Kill switch déclenché" in body


@pytest.mark.asyncio
async def test_logs_stub_renders_m9_message(m6_client: AsyncClient) -> None:
    res = await m6_client.get("/logs")
    assert res.status_code == 200
    assert "M9" in res.text
    assert "polycopy" in res.text.lower()


@pytest.mark.asyncio
async def test_external_health_partial_renders_with_snapshot_or_loading(
    m6_client: AsyncClient,
) -> None:
    res = await m6_client.get("/api/health-external")
    assert res.status_code == 200
    body = res.text
    assert "Gamma" in body
    assert "Data API" in body


@pytest.mark.asyncio
async def test_pages_include_sidebar_and_footer(m6_client: AsyncClient) -> None:
    for path in ("/home", "/detections", "/strategy", "/orders", "/positions", "/pnl", "/traders"):
        res = await m6_client.get(path)
        assert res.status_code == 200, path
        body = res.text
        assert 'id="sidebar"' in body, f"sidebar absente sur {path}"
        assert "polycopy" in body, f"version footer absente sur {path}"


@pytest.mark.asyncio
async def test_orders_page_filters_chips(m6_client: AsyncClient) -> None:
    res = await m6_client.get("/orders?status=FILLED")
    body = res.text
    assert "filter-chip-active" in body
    # FILLED chip est active
    assert 'href="/orders?status=FILLED"' in body


@pytest.mark.asyncio
async def test_orders_rows_renders_with_my_order(
    m6_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            MyOrder(
                source_tx_hash="0xfilled",
                condition_id="0xcond",
                asset_id="1",
                side="BUY",
                size=2.0,
                price=0.5,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="FILLED",
                simulated=True,
                transaction_hashes=[],
            ),
        )
        await session.commit()
    res = await m6_client.get("/partials/orders-rows")
    assert res.status_code == 200
    body = res.text
    assert "FILLED" in body
    assert "badge" in body


@pytest.mark.asyncio
async def test_data_theme_attribute_uses_settings_value(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        dashboard_theme="light",
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/home")
    assert 'data-theme="light"' in res.text


@pytest.mark.asyncio
async def test_poll_interval_used_in_hx_trigger(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        dashboard_poll_interval_seconds=8,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/home")
    assert "every 8s" in res.text
