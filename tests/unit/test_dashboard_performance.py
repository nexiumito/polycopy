"""Tests route ``/performance`` + query ``list_trader_performance`` (commit 7)."""

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
async def performance_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    settings = Settings(_env_file=None, target_wallets=[])  # type: ignore[call-arg]
    app = build_app(session_factory, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


async def _seed_trader_with_position(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    target_trader_repo: TargetTraderRepository,
    my_position_repo: MyPositionRepository,
    *,
    wallet: str,
    label: str | None,
    pnl: float,
    closed: bool,
) -> None:
    """Seed : TargetTrader + DetectedTrade + MyOrder BUY + MyPosition."""
    await target_trader_repo.insert_shadow(wallet, label=label)
    tx = "0xtx-" + wallet[-4:]
    cond = "0xcond-" + wallet[-4:]
    asset = "asset-" + wallet[-4:]
    await detected_trade_repo.insert_if_new(
        DetectedTradeDTO(
            tx_hash=tx,
            target_wallet=wallet,
            condition_id=cond,
            asset_id=asset,
            side="BUY",
            size=10.0,
            usdc_size=4.0,
            price=0.4,
            timestamp=datetime.now(tz=UTC),
            outcome="Yes",
            slug="slug",
            raw_json={"tx_hash": tx},
        ),
    )
    # BUY order source.
    async with session_factory() as session:
        session.add(
            MyOrder(
                source_tx_hash=tx,
                condition_id=cond,
                asset_id=asset,
                side="BUY",
                size=10.0,
                price=0.4,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="FILLED",
                simulated=False,
                transaction_hashes=[],
            ),
        )
        await session.commit()
    await my_position_repo.upsert_on_fill(cond, asset, "BUY", 10.0, 0.4)
    if closed:
        # Clôture via realized_pnl dénormalisé (cas dry-run résolu).
        async with session_factory() as session:
            stmt_pos = await session.execute(
                __import__("sqlalchemy")
                .select(MyPosition)
                .where(
                    MyPosition.condition_id == cond,
                ),
            )
            pos = stmt_pos.scalar_one()
            pos.closed_at = datetime.now(tz=UTC)
            pos.realized_pnl = pnl
            pos.simulated = True
            await session.commit()


@pytest.mark.asyncio
async def test_performance_query_empty_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = await queries.list_trader_performance(session_factory)
    assert rows == []


@pytest.mark.asyncio
async def test_performance_query_trader_without_position_excluded(
    session_factory: async_sessionmaker[AsyncSession],
    target_trader_repo: TargetTraderRepository,
) -> None:
    """Un trader sans position tracée ne doit PAS apparaître dans le leaderboard."""
    await target_trader_repo.insert_shadow("0xghost", label="sans position")
    rows = await queries.list_trader_performance(session_factory)
    assert rows == []


@pytest.mark.asyncio
async def test_performance_query_sorts_desc_by_pnl_total(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    target_trader_repo: TargetTraderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    """3 traders : positif, négatif, neutre (no closed) → ordre desc PnL."""
    await _seed_trader_with_position(
        session_factory,
        detected_trade_repo,
        target_trader_repo,
        my_position_repo,
        wallet="0xaaa1",
        label="winner",
        pnl=5.0,
        closed=True,
    )
    await _seed_trader_with_position(
        session_factory,
        detected_trade_repo,
        target_trader_repo,
        my_position_repo,
        wallet="0xbbb2",
        label="loser",
        pnl=-2.0,
        closed=True,
    )
    await _seed_trader_with_position(
        session_factory,
        detected_trade_repo,
        target_trader_repo,
        my_position_repo,
        wallet="0xccc3",
        label="open-only",
        pnl=0.0,
        closed=False,
    )

    rows = await queries.list_trader_performance(session_factory)
    assert len(rows) == 3
    assert [r.label for r in rows] == ["winner", "open-only", "loser"]

    # Win rate : winner 1W/0L → 100%, loser 0W/1L → 0%, open-only → None.
    winner = next(r for r in rows if r.label == "winner")
    assert winner.positions_closed_count == 1
    assert winner.positions_open_count == 0
    assert winner.win_count == 1
    assert winner.loss_count == 0
    assert winner.win_rate_pct == pytest.approx(100.0)
    assert winner.realized_pnl_total == pytest.approx(5.0)

    open_only = next(r for r in rows if r.label == "open-only")
    assert open_only.positions_closed_count == 0
    assert open_only.positions_open_count == 1
    assert open_only.win_rate_pct is None
    assert open_only.realized_pnl_total == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_performance_query_respects_status_filter(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    target_trader_repo: TargetTraderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    await _seed_trader_with_position(
        session_factory,
        detected_trade_repo,
        target_trader_repo,
        my_position_repo,
        wallet="0xact1",
        label="is-active",
        pnl=3.0,
        closed=True,
    )
    await target_trader_repo.transition_status("0xact1", new_status="active")
    await _seed_trader_with_position(
        session_factory,
        detected_trade_repo,
        target_trader_repo,
        my_position_repo,
        wallet="0xshd2",
        label="is-shadow",
        pnl=1.0,
        closed=True,
    )
    # 0xshd2 reste en shadow (default).

    active_only = await queries.list_trader_performance(session_factory, status="active")
    assert [r.label for r in active_only] == ["is-active"]

    shadow_only = await queries.list_trader_performance(session_factory, status="shadow")
    assert [r.label for r in shadow_only] == ["is-shadow"]

    unknown_status_passthrough = await queries.list_trader_performance(
        session_factory,
        status="bogus",
    )
    assert len(unknown_status_passthrough) == 2


@pytest.mark.asyncio
async def test_performance_page_renders(
    performance_client: AsyncClient,
) -> None:
    res = await performance_client.get("/performance")
    assert res.status_code == 200
    assert "leaderboard traders" in res.text.lower()
    assert "/partials/performance-rows" in res.text
    # Filter-chips présents.
    for label in ("Toutes", "Active", "Shadow", "Sell-only", "Blacklisted", "Pinned"):
        assert label in res.text


@pytest.mark.asyncio
async def test_performance_partial_renders_row(
    performance_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    target_trader_repo: TargetTraderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    await _seed_trader_with_position(
        session_factory,
        detected_trade_repo,
        target_trader_repo,
        my_position_repo,
        wallet="0xperf1",
        label="perf-subject",
        pnl=7.5,
        closed=True,
    )
    res = await performance_client.get("/partials/performance-rows")
    assert res.status_code == 200
    body = res.text
    assert "perf-subject" in body
    # PnL positif → couleur profit.
    assert "var(--color-profit)" in body


@pytest.mark.asyncio
async def test_sidebar_has_performance_link(
    performance_client: AsyncClient,
) -> None:
    res = await performance_client.get("/home")
    assert res.status_code == 200
    assert "/performance" in res.text
    assert "Performance" in res.text


@pytest.mark.asyncio
async def test_performance_last_trade_at_reflects_latest_detected(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    target_trader_repo: TargetTraderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    """``last_trade_at`` est le max des ``DetectedTrade.timestamp`` du trader."""
    wallet = "0xtime1"
    await target_trader_repo.insert_shadow(wallet)
    oldest = datetime.now(tz=UTC) - timedelta(days=5)
    newest = datetime.now(tz=UTC) - timedelta(hours=3)
    for i, ts in enumerate([oldest, newest]):
        tx = f"0xtx{i}"
        await detected_trade_repo.insert_if_new(
            DetectedTradeDTO(
                tx_hash=tx,
                target_wallet=wallet,
                condition_id=f"0xcond{i}",
                asset_id=f"asset{i}",
                side="BUY",
                size=1.0,
                usdc_size=0.5,
                price=0.5,
                timestamp=ts,
                outcome=None,
                slug=None,
                raw_json={"tx_hash": tx},
            ),
        )
    await my_position_repo.upsert_on_fill("0xcond0", "asset0", "BUY", 1.0, 0.5)
    async with session_factory() as session:
        session.add(
            MyOrder(
                source_tx_hash="0xtx0",
                condition_id="0xcond0",
                asset_id="asset0",
                side="BUY",
                size=1.0,
                price=0.5,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="FILLED",
                simulated=False,
                transaction_hashes=[],
            ),
        )
        await session.commit()

    rows = await queries.list_trader_performance(session_factory)
    assert len(rows) == 1
    assert rows[0].last_trade_at is not None
    # Doit pointer sur le plus récent (newest), pas sur oldest.
    assert rows[0].last_trade_at >= newest - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_performance_leaderboard_dry_run_winrate(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    target_trader_repo: TargetTraderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    """Bug 3 : un scénario BUY+SELL SIMULATED qui ferme une position virtuelle
    doit remonter avec un winrate et un PnL réel, pas des zéros ni "—".
    """
    wallet = "0xdryrun1"
    tx = "0xtx-dry"
    cond = "0xcond-dry"
    asset = "asset-dry"
    await target_trader_repo.insert_shadow(wallet, label="dry-subject")
    await target_trader_repo.transition_status(wallet, new_status="active")
    await detected_trade_repo.insert_if_new(
        DetectedTradeDTO(
            tx_hash=tx,
            target_wallet=wallet,
            condition_id=cond,
            asset_id=asset,
            side="BUY",
            size=10.0,
            usdc_size=4.0,
            price=0.4,
            timestamp=datetime.now(tz=UTC),
            outcome="Yes",
            slug="slug",
            raw_json={"tx_hash": tx},
        ),
    )

    # BUY 10 @ 0.40 SIMULATED (dry-run via M8 realistic_fill).
    async with session_factory() as session:
        session.add(
            MyOrder(
                source_tx_hash=tx,
                condition_id=cond,
                asset_id=asset,
                side="BUY",
                size=10.0,
                price=0.40,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="SIMULATED",
                simulated=True,
                realistic_fill=True,
                transaction_hashes=[],
            ),
        )
        # SELL 10 @ 0.60 SIMULATED (la copie du SELL source wallet).
        session.add(
            MyOrder(
                source_tx_hash=tx,
                condition_id=cond,
                asset_id=asset,
                side="SELL",
                size=10.0,
                price=0.60,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="SIMULATED",
                simulated=True,
                realistic_fill=True,
                transaction_hashes=[],
            ),
        )
        await session.commit()
    # Position virtuelle créée puis close via upsert_virtual (chaîne réelle
    # bug 1 + bug 3) — realized_pnl doit être +2.0 dénormalisé.
    await my_position_repo.upsert_virtual(
        condition_id=cond,
        asset_id=asset,
        side="BUY",
        size_filled=10.0,
        fill_price=0.40,
    )
    closed = await my_position_repo.upsert_virtual(
        condition_id=cond,
        asset_id=asset,
        side="SELL",
        size_filled=10.0,
        fill_price=0.60,
    )
    assert closed is not None
    assert closed.realized_pnl == pytest.approx(2.0, abs=1e-9)

    rows = await queries.list_trader_performance(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.wallet_address == wallet
    assert row.positions_closed_count == 1
    assert row.positions_open_count == 0
    assert row.win_count == 1
    assert row.loss_count == 0
    assert row.win_rate_pct == pytest.approx(100.0, abs=0.01)
    assert row.realized_pnl_total == pytest.approx(2.0, abs=1e-9)
