"""Tests du ``PnlSnapshotRepository`` (M4)."""

from datetime import UTC, datetime, timedelta

import pytest

from polycopy.storage.dtos import PnlSnapshotDTO
from polycopy.storage.repositories import PnlSnapshotRepository


def _dto(total: float, *, is_dry_run: bool = False, drawdown_pct: float = 0.0) -> PnlSnapshotDTO:
    return PnlSnapshotDTO(
        total_usdc=total,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        drawdown_pct=drawdown_pct,
        open_positions_count=0,
        cash_pnl_total=None,
        is_dry_run=is_dry_run,
    )


@pytest.mark.asyncio
async def test_insert_persists_and_returns_with_id(
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    rec = await pnl_snapshot_repo.insert(_dto(100.0))
    assert rec.id is not None
    assert rec.total_usdc == 100.0
    assert rec.is_dry_run is False


@pytest.mark.asyncio
async def test_get_max_total_usdc_ignores_dry_run(
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    await pnl_snapshot_repo.insert(_dto(50.0, is_dry_run=False))
    await pnl_snapshot_repo.insert(_dto(200.0, is_dry_run=True))  # doit être ignoré
    await pnl_snapshot_repo.insert(_dto(80.0, is_dry_run=False))
    assert await pnl_snapshot_repo.get_max_total_usdc(only_real=True) == 80.0
    # only_real=False → max absolu.
    assert await pnl_snapshot_repo.get_max_total_usdc(only_real=False) == 200.0


@pytest.mark.asyncio
async def test_get_max_total_usdc_empty_returns_none(
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    assert await pnl_snapshot_repo.get_max_total_usdc() is None


@pytest.mark.asyncio
async def test_get_latest_returns_most_recent(
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    assert await pnl_snapshot_repo.get_latest() is None
    await pnl_snapshot_repo.insert(_dto(10.0, is_dry_run=False))
    await pnl_snapshot_repo.insert(_dto(20.0, is_dry_run=False))
    latest = await pnl_snapshot_repo.get_latest()
    assert latest is not None
    assert latest.total_usdc == 20.0


@pytest.mark.asyncio
async def test_list_since_filters_by_timestamp(
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> None:
    for i in range(3):
        await pnl_snapshot_repo.insert(_dto(float(i), is_dry_run=False))
    now = datetime.now(tz=UTC)
    rows = await pnl_snapshot_repo.list_since(now - timedelta(minutes=1))
    assert len(rows) == 3
    future = await pnl_snapshot_repo.list_since(now + timedelta(minutes=5))
    assert future == []
