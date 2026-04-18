"""Tests des routes FastAPI du dashboard (via ASGITransport, pas de socket)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard.routes import build_app
from polycopy.storage.dtos import (
    DetectedTradeDTO,
    PnlSnapshotDTO,
    StrategyDecisionDTO,
)
from polycopy.storage.models import MyOrder
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    PnlSnapshotRepository,
    StrategyDecisionRepository,
)


@pytest_asyncio.fixture
async def dashboard_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
    )
    app = build_app(session_factory, settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_healthz(dashboard_client: AsyncClient) -> None:
    res = await dashboard_client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_home_returns_html(dashboard_client: AsyncClient) -> None:
    res = await dashboard_client.get("/home")
    assert res.status_code == 200
    assert "<!doctype html>" in res.text.lower()
    assert "polycopy" in res.text


@pytest.mark.asyncio
async def test_root_redirects_to_home(dashboard_client: AsyncClient) -> None:
    res = await dashboard_client.get("/", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"].endswith("/home")


@pytest.mark.asyncio
async def test_partials_kpis_is_fragment(dashboard_client: AsyncClient) -> None:
    res = await dashboard_client.get("/partials/kpis")
    assert res.status_code == 200
    # Fragment — ne doit PAS contenir le doctype full-page.
    assert "<!doctype html>" not in res.text.lower()


@pytest.mark.asyncio
async def test_partials_pnl_data_json_shape(
    dashboard_client: AsyncClient,
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    await pnl_snapshot_repo.insert(
        PnlSnapshotDTO(
            total_usdc=42.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=1.5,
            open_positions_count=0,
            cash_pnl_total=None,
            is_dry_run=False,
        ),
    )
    res = await dashboard_client.get("/partials/pnl-data.json?since=24h")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"timestamps", "total_usdc", "drawdown_pct"}
    assert len(body["timestamps"]) == 1
    assert len(body["total_usdc"]) == 1
    assert len(body["drawdown_pct"]) == 1
    assert body["total_usdc"][0] == 42.0


@pytest.mark.asyncio
async def test_partials_pnl_data_invalid_since_fallback(
    dashboard_client: AsyncClient,
) -> None:
    res = await dashboard_client.get("/partials/pnl-data.json?since=foo")
    assert res.status_code == 200  # pas de 422
    body = res.json()
    assert body == {"timestamps": [], "total_usdc": [], "drawdown_pct": []}


@pytest.mark.asyncio
async def test_partials_orders_rows_filter(
    dashboard_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for tx, status in (("0xa", "FILLED"), ("0xb", "REJECTED"), ("0xc", "FILLED")):
        async with session_factory() as session:
            session.add(
                MyOrder(
                    source_tx_hash=tx,
                    condition_id="0xcond",
                    asset_id="1",
                    side="BUY",
                    size=1.0,
                    price=0.5,
                    tick_size=0.01,
                    neg_risk=False,
                    order_type="FOK",
                    status=status,
                    simulated=True,
                    transaction_hashes=[],
                ),
            )
            await session.commit()
    res = await dashboard_client.get("/partials/orders-rows?status=FILLED")
    assert res.status_code == 200
    # Les 2 fills apparaissent, pas le rejet.
    assert res.text.count("0xa") >= 1
    assert res.text.count("0xc") >= 1
    assert "0xb" not in res.text


@pytest.mark.asyncio
async def test_partials_limit_clamp(
    dashboard_client: AsyncClient,
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    for i in range(3):
        await detected_trade_repo.insert_if_new(
            DetectedTradeDTO(
                tx_hash=f"0xt{i}",
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
    # limit=500 doit être accepté (clampé en interne) — pas de 422.
    res = await dashboard_client.get("/partials/detections-rows?limit=500")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_partials_strategy_rows_filter(
    dashboard_client: AsyncClient,
    strategy_decision_repo: StrategyDecisionRepository,
) -> None:
    await strategy_decision_repo.insert(
        StrategyDecisionDTO(
            detected_trade_id=1,
            tx_hash="0xapproved",
            decision="APPROVED",
            my_size=1.0,
            my_price=0.5,
            pipeline_state={},
        ),
    )
    await strategy_decision_repo.insert(
        StrategyDecisionDTO(
            detected_trade_id=2,
            tx_hash="0xrejected",
            decision="REJECTED",
            reason="slippage_exceeded",
            pipeline_state={},
        ),
    )
    res = await dashboard_client.get("/partials/strategy-rows?decision=APPROVED")
    assert res.status_code == 200
    assert "0xapprov" in res.text
    assert "0xrejec" not in res.text


@pytest.mark.asyncio
async def test_static_css_served(dashboard_client: AsyncClient) -> None:
    res = await dashboard_client.get("/static/dashboard.css")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/css")


@pytest.mark.asyncio
async def test_docs_disabled(dashboard_client: AsyncClient) -> None:
    assert (await dashboard_client.get("/docs")).status_code == 404
    assert (await dashboard_client.get("/openapi.json")).status_code == 404
    assert (await dashboard_client.get("/redoc")).status_code == 404


@pytest.mark.asyncio
async def test_each_page_returns_200(dashboard_client: AsyncClient) -> None:
    for path in (
        "/home",
        "/detections",
        "/strategy",
        "/orders",
        "/positions",
        "/pnl",
        "/traders",
        "/backtest",
    ):
        res = await dashboard_client.get(path)
        assert res.status_code == 200, path
        assert "<!doctype html>" in res.text.lower(), path


@pytest.mark.asyncio
async def test_traders_page_filter_querystring(
    dashboard_client: AsyncClient,
) -> None:
    """Le filtre `?status=...` est passé au partial HTMX via l'URL hx-get."""
    res = await dashboard_client.get("/traders?status=shadow")
    assert res.status_code == 200
    # Le template inclut l'URL hx-get avec status.
    assert "/partials/traders-rows?status=shadow" in res.text


@pytest.mark.asyncio
async def test_partials_traders_rows_renders_counts(
    dashboard_client: AsyncClient,
    target_trader_repo,  # type: ignore[no-untyped-def]
) -> None:
    await target_trader_repo.upsert("0xpinn")  # pinned
    await target_trader_repo.insert_shadow("0xshad")
    res = await dashboard_client.get("/partials/traders-rows")
    assert res.status_code == 200
    # Le fragment contient les 2 wallets et au moins 1 status counts.
    assert "0xpinn" in res.text
    assert "0xshad" in res.text
    assert "<!doctype html>" not in res.text.lower()


@pytest.mark.asyncio
async def test_partials_traders_rows_invalid_status_ignored(
    dashboard_client: AsyncClient,
) -> None:
    """Filtre invalide → traité comme non-filtre (UX > strictness)."""
    res = await dashboard_client.get("/partials/traders-rows?status=FAKE")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_backtest_page_notes_when_no_report(
    dashboard_client: AsyncClient,
) -> None:
    res = await dashboard_client.get("/backtest")
    assert res.status_code == 200
    # Soit rapport existant, soit guide affiché : les 2 contiennent "Backtest".
    assert "Backtest" in res.text
