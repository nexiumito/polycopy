"""Tests route ``/activity`` + query ``list_activity_closed_positions`` (commit 6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard import queries
from polycopy.dashboard.routes import build_app
from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.models import MyOrder, MyPosition
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    MyPositionRepository,
    TargetTraderRepository,
)


@pytest_asyncio.fixture
async def activity_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    settings = Settings(_env_file=None, target_wallets=[])  # type: ignore[call-arg]
    app = build_app(session_factory, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


async def _insert_filled_order(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_tx: str,
    cond_id: str,
    asset_id: str,
    side: str,
    size: float,
    price: float,
) -> None:
    async with session_factory() as session:
        session.add(
            MyOrder(
                source_tx_hash=source_tx,
                condition_id=cond_id,
                asset_id=asset_id,
                side=side,
                size=size,
                price=price,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="FILLED",
                simulated=False,
                transaction_hashes=[],
            ),
        )
        await session.commit()


@pytest.mark.asyncio
async def test_activity_query_empty_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DB vide → liste vide, pas de crash."""
    rows = await queries.list_activity_closed_positions(session_factory)
    assert rows == []


@pytest.mark.asyncio
async def test_activity_query_with_live_closed_position(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    target_trader_repo: TargetTraderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    """Position live fermée via SELL : PnL calculé à partir des fills."""
    # 1. Seed trader source + DetectedTrade.
    await target_trader_repo.insert_shadow("0xsrc", label="alpha-trader")
    await detected_trade_repo.insert_if_new(
        DetectedTradeDTO(
            tx_hash="0xsrctx",
            target_wallet="0xsrc",
            condition_id="0xcondA",
            asset_id="asset-1",
            side="BUY",
            size=10.0,
            usdc_size=4.0,
            price=0.4,
            timestamp=datetime.now(tz=UTC),
            outcome="Yes",
            slug="market-slug",
            raw_json={"tx_hash": "0xsrctx"},
        ),
    )

    # 2. BUY fill (10 @ 0.40) + SELL fill (10 @ 0.55).
    await _insert_filled_order(
        session_factory,
        source_tx="0xsrctx",
        cond_id="0xcondA",
        asset_id="asset-1",
        side="BUY",
        size=10.0,
        price=0.40,
    )
    await _insert_filled_order(
        session_factory,
        source_tx="0xsrctx",
        cond_id="0xcondA",
        asset_id="asset-1",
        side="SELL",
        size=10.0,
        price=0.55,
    )

    # 3. Position fermée via le SELL (simulated=False).
    await my_position_repo.upsert_on_fill("0xcondA", "asset-1", "BUY", 10.0, 0.4)
    await my_position_repo.upsert_on_fill("0xcondA", "asset-1", "SELL", 10.0, 0.55)

    rows = await queries.list_activity_closed_positions(session_factory)
    assert len(rows) == 1
    r = rows[0]
    assert r.source_trader_wallet == "0xsrc"
    assert r.source_trader_label == "alpha-trader"
    assert r.outcome_label == "Yes"
    assert r.avg_buy_price == pytest.approx(0.40)
    assert r.avg_sell_price == pytest.approx(0.55)
    # PnL live = 10*0.55 - 10*0.40 = 1.5.
    assert r.realized_pnl == pytest.approx(1.5)
    assert r.simulated is False


@pytest.mark.asyncio
async def test_activity_query_with_dry_run_resolved_position(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Position dry-run résolue par M8 : avg_sell=None, realized_pnl depuis MyPosition."""
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xcondB",
                asset_id="asset-2",
                size=5.0,
                avg_price=0.30,
                opened_at=datetime.now(tz=UTC) - timedelta(days=2),
                closed_at=datetime.now(tz=UTC),
                simulated=True,
                realized_pnl=3.5,  # dénormalisé par DryRunResolutionWatcher.
            ),
        )
        await session.commit()

    rows = await queries.list_activity_closed_positions(session_factory)
    assert len(rows) == 1
    r = rows[0]
    assert r.simulated is True
    assert r.avg_sell_price is None  # aucun SELL fill en dry-run M8.
    assert r.realized_pnl == pytest.approx(3.5)
    assert r.source_trader_wallet is None  # pas de trade source joignable.


@pytest.mark.asyncio
async def test_activity_query_orders_by_closed_desc(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Résultats triés ``closed_at DESC`` (plus récent en premier)."""
    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xold",
                asset_id="a",
                size=1.0,
                avg_price=0.5,
                opened_at=now - timedelta(days=3),
                closed_at=now - timedelta(days=2),
                simulated=True,
                realized_pnl=0.1,
            ),
        )
        session.add(
            MyPosition(
                condition_id="0xnew",
                asset_id="b",
                size=1.0,
                avg_price=0.5,
                opened_at=now - timedelta(days=1),
                closed_at=now,
                simulated=True,
                realized_pnl=0.2,
            ),
        )
        await session.commit()

    rows = await queries.list_activity_closed_positions(session_factory)
    assert [r.condition_id for r in rows] == ["0xnew", "0xold"]


@pytest.mark.asyncio
async def test_activity_page_renders(activity_client: AsyncClient) -> None:
    res = await activity_client.get("/activity")
    assert res.status_code == 200
    assert "Historique des positions fermées" in res.text
    assert "Légende des colonnes" in res.text


@pytest.mark.asyncio
async def test_activity_partial_renders_rows(
    activity_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            MyPosition(
                condition_id="0xshow",
                asset_id="z",
                size=1.0,
                avg_price=0.5,
                opened_at=datetime.now(tz=UTC) - timedelta(hours=2),
                closed_at=datetime.now(tz=UTC),
                simulated=True,
                realized_pnl=0.4,
            ),
        )
        await session.commit()
    res = await activity_client.get("/partials/activity-rows")
    assert res.status_code == 200
    body = res.text
    assert "0xshow" in body
    # PnL positif → couleur profit.
    assert "var(--color-profit)" in body


@pytest.mark.asyncio
async def test_sidebar_has_activity_link(activity_client: AsyncClient) -> None:
    res = await activity_client.get("/home")
    assert res.status_code == 200
    assert "/activity" in res.text
    assert "Activité" in res.text
