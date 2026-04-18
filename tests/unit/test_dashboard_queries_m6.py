"""Tests des nouvelles queries dashboard M6 (KPIs, Discovery, Milestones, version)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.dashboard import queries
from polycopy.storage.dtos import PnlSnapshotDTO
from polycopy.storage.models import MyOrder, TargetTrader, TraderEvent
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    PnlSnapshotRepository,
)
from tests.unit.test_dashboard_queries import _trade


def _pnl(total: float, *, dt: datetime, drawdown: float = 0.0) -> PnlSnapshotDTO:
    return PnlSnapshotDTO(
        total_usdc=total,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        drawdown_pct=drawdown,
        open_positions_count=0,
        cash_pnl_total=None,
        is_dry_run=False,
    )


@pytest.mark.asyncio
async def test_get_home_kpi_cards_empty_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cards = await queries.get_home_kpi_cards(session_factory)
    assert len(cards) == 4
    titles = [c.title for c in cards]
    assert titles == ["Total USDC", "Drawdown", "Positions ouvertes", "Trades détectés (24 h)"]
    # Empty DB → values defaulted
    assert cards[0].value == "—"
    assert cards[1].value == "—"
    assert cards[2].value == "0"
    assert cards[3].value == "0"
    assert all(c.sparkline_points == [] for c in cards)


@pytest.mark.asyncio
async def test_get_home_kpi_cards_with_snapshots(
    session_factory: async_sessionmaker[AsyncSession],
    pnl_snapshot_repo: PnlSnapshotRepository,
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    now = datetime.now(tz=UTC)
    # Snapshots sur la fenêtre 24h, croissants → delta positif
    for i, value in enumerate((100.0, 110.0, 121.0)):
        await pnl_snapshot_repo.insert(
            _pnl(value, dt=now - timedelta(hours=12 - i * 2), drawdown=2.5),
        )
    await detected_trade_repo.insert_if_new(_trade("0xtx1"))

    cards = await queries.get_home_kpi_cards(session_factory)
    total_card = cards[0]
    drawdown_card = cards[1]
    detections_card = cards[3]

    # Le format dépend de la dernière inséré (qui devient « latest »)
    assert "$" in total_card.value
    assert total_card.delta is not None
    assert total_card.delta_sign in {"positive", "negative"}
    assert len(total_card.sparkline_points) >= 2
    assert "%" in drawdown_card.value
    assert detections_card.value == "1"


@pytest.mark.asyncio
async def test_get_discovery_status_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    status = await queries.get_discovery_status(session_factory, enabled=False)
    assert status.enabled is False
    assert status.active_count == 0
    assert status.shadow_count == 0
    assert status.paused_count == 0
    assert status.pinned_count == 0
    assert status.last_cycle_at is None
    assert status.promotions_24h == 0


@pytest.mark.asyncio
async def test_get_discovery_status_with_traders(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(TargetTrader(wallet_address="0x1", status="active", pinned=False))
        session.add(TargetTrader(wallet_address="0x2", status="shadow", pinned=False))
        session.add(TargetTrader(wallet_address="0x3", status="paused", pinned=False))
        session.add(TargetTrader(wallet_address="0x4", status="pinned", pinned=True))
        session.add(
            TraderEvent(
                wallet_address="0x1",
                event_type="promoted_active",
                at=datetime.now(tz=UTC) - timedelta(hours=1),
            ),
        )
        session.add(
            TraderEvent(
                wallet_address="0x3",
                event_type="demoted_paused",
                at=datetime.now(tz=UTC) - timedelta(hours=2),
            ),
        )
        await session.commit()

    status = await queries.get_discovery_status(session_factory, enabled=True)
    assert status.enabled is True
    assert status.active_count == 1
    assert status.shadow_count == 1
    assert status.paused_count == 1
    assert status.pinned_count == 1
    assert status.promotions_24h == 1
    assert status.demotions_24h == 1


@pytest.mark.asyncio
async def test_get_pnl_milestones_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    out = await queries.get_pnl_milestones(session_factory, since=timedelta(days=7))
    assert out == []


@pytest.mark.asyncio
async def test_get_pnl_milestones_combines_fills_and_promotions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        session.add(
            MyOrder(
                source_tx_hash="0xa",
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
                filled_at=now - timedelta(hours=2),
            ),
        )
        session.add(
            TraderEvent(
                wallet_address="0xpromo",
                event_type="promoted_active",
                at=now - timedelta(hours=1),
            ),
        )
        session.add(
            TraderEvent(
                wallet_address="0xkill",
                event_type="kill_switch",
                at=now - timedelta(minutes=30),
            ),
        )
        await session.commit()

    out = await queries.get_pnl_milestones(session_factory, since=timedelta(days=1))
    assert len(out) == 3
    # Tri desc par at
    assert out[0].event_type == "kill_switch"
    types = {m.event_type for m in out}
    assert "trader_promoted" in types
    assert "first_fill" in types


@pytest.mark.asyncio
async def test_get_pnl_milestones_caps_at_8(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        for i in range(20):
            session.add(
                TraderEvent(
                    wallet_address=f"0xw{i}",
                    event_type="kill_switch",
                    at=now - timedelta(minutes=i),
                ),
            )
        await session.commit()
    out = await queries.get_pnl_milestones(session_factory, since=timedelta(days=1))
    assert len(out) == 8


@pytest.mark.asyncio
async def test_get_app_version_returns_string() -> None:
    # Reset cache pour ce test (le module garde un cache process-wide)
    queries._APP_VERSION_CACHE = None  # type: ignore[attr-defined]
    version = await queries.get_app_version()
    assert isinstance(version, str)
    assert version.startswith("0.6.0-")


@pytest.mark.asyncio
async def test_get_app_version_cached() -> None:
    queries._APP_VERSION_CACHE = "0.6.0-cached"  # type: ignore[attr-defined]
    assert await queries.get_app_version() == "0.6.0-cached"
    queries._APP_VERSION_CACHE = None  # type: ignore[attr-defined]
