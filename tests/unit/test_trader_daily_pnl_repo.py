"""Tests `TraderDailyPnlRepository` M12.

Contrat critique :

- ``insert_if_new`` dédup sur la contrainte unique ``(wallet_address, date)``.
- ``get_curve`` retourne les rows ordonnées ``date`` ascendant, borné par
  ``days``.
- ``get_curves_batch`` regroupe par wallet, préserve l'ordre des dates.

Ces invariants sont consommés par :
- ``TraderDailyPnlWriter`` (writer quotidien, dédup idempotent).
- ``MetricsCollectorV2._compute_equity_curve`` (Sortino / Calmar).
- ``DiscoveryOrchestrator._build_pool_context`` (batch pool-wide).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polycopy.storage.dtos import TraderDailyPnlDTO
from polycopy.storage.repositories import TraderDailyPnlRepository


def _dto(
    wallet: str,
    d: datetime,
    *,
    equity: float = 100.0,
    realized: float = 0.0,
    unrealized: float = 0.0,
    positions: int = 0,
) -> TraderDailyPnlDTO:
    return TraderDailyPnlDTO(
        wallet_address=wallet,
        date=d.date(),
        equity_usdc=equity,
        realized_pnl_day=realized,
        unrealized_pnl_day=unrealized,
        positions_count=positions,
    )


@pytest.mark.asyncio
async def test_insert_if_new_inserts_first_time(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    today = datetime.now(tz=UTC)
    inserted = await trader_daily_pnl_repo.insert_if_new(_dto("0xabc", today))
    assert inserted is True


@pytest.mark.asyncio
async def test_insert_if_new_dedup_same_wallet_same_date(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    """Deux inserts même (wallet, date) → 2ᵉ retourne False (contrainte unique)."""
    today = datetime.now(tz=UTC)
    first = await trader_daily_pnl_repo.insert_if_new(_dto("0xabc", today, equity=100.0))
    second = await trader_daily_pnl_repo.insert_if_new(
        _dto("0xabc", today, equity=200.0),
    )
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_insert_if_new_allows_same_wallet_different_date(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    today = datetime.now(tz=UTC)
    yesterday = today - timedelta(days=1)
    assert await trader_daily_pnl_repo.insert_if_new(_dto("0xabc", today)) is True
    assert await trader_daily_pnl_repo.insert_if_new(_dto("0xabc", yesterday)) is True


@pytest.mark.asyncio
async def test_insert_if_new_allows_different_wallets_same_date(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    today = datetime.now(tz=UTC)
    assert await trader_daily_pnl_repo.insert_if_new(_dto("0xabc", today)) is True
    assert await trader_daily_pnl_repo.insert_if_new(_dto("0xdef", today)) is True


@pytest.mark.asyncio
async def test_wallet_address_is_lowercased(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    today = datetime.now(tz=UTC)
    await trader_daily_pnl_repo.insert_if_new(_dto("0xABC", today))
    curve = await trader_daily_pnl_repo.get_curve("0xabc", days=30)
    assert len(curve) == 1
    assert curve[0].wallet_address == "0xabc"


@pytest.mark.asyncio
async def test_get_curve_ordered_by_date_ascending(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    now = datetime.now(tz=UTC)
    for i, equity in zip([3, 1, 2], [103.0, 101.0, 102.0], strict=True):
        await trader_daily_pnl_repo.insert_if_new(
            _dto("0xabc", now - timedelta(days=i), equity=equity),
        )
    curve = await trader_daily_pnl_repo.get_curve("0xabc", days=30)
    assert [float(r.equity_usdc) for r in curve] == [103.0, 102.0, 101.0]


@pytest.mark.asyncio
async def test_get_curve_respects_days_cutoff(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    now = datetime.now(tz=UTC)
    # Row d'il y a 10 jours (dehors) + row d'il y a 2 jours (dans).
    await trader_daily_pnl_repo.insert_if_new(_dto("0xabc", now - timedelta(days=10)))
    await trader_daily_pnl_repo.insert_if_new(_dto("0xabc", now - timedelta(days=2)))
    curve = await trader_daily_pnl_repo.get_curve("0xabc", days=5)
    assert len(curve) == 1


@pytest.mark.asyncio
async def test_get_curves_batch_groups_by_wallet(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    now = datetime.now(tz=UTC)
    await trader_daily_pnl_repo.insert_if_new(_dto("0xaaa", now, equity=10.0))
    await trader_daily_pnl_repo.insert_if_new(_dto("0xaaa", now - timedelta(days=1), equity=9.0))
    await trader_daily_pnl_repo.insert_if_new(_dto("0xbbb", now, equity=20.0))
    grouped = await trader_daily_pnl_repo.get_curves_batch(
        ["0xaaa", "0xbbb", "0xccc"],
        days=30,
    )
    assert set(grouped.keys()) == {"0xaaa", "0xbbb", "0xccc"}
    assert len(grouped["0xaaa"]) == 2
    assert len(grouped["0xbbb"]) == 1
    assert grouped["0xccc"] == []


@pytest.mark.asyncio
async def test_get_curves_batch_empty_wallet_list(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    assert await trader_daily_pnl_repo.get_curves_batch([], days=30) == {}
