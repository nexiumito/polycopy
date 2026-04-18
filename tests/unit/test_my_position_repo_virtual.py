"""Tests M8 §9.7 — extensions ``MyPositionRepository`` virtuelles."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from polycopy.storage.repositories import MyPositionRepository


async def test_upsert_virtual_creates_new_buy(
    my_position_repo: MyPositionRepository,
) -> None:
    pos = await my_position_repo.upsert_virtual(
        condition_id="0xc",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.08,
    )
    assert pos is not None
    assert pos.simulated is True
    assert pos.size == 10.0
    assert pos.avg_price == 0.08


async def test_upsert_virtual_buy_then_buy_weighted_avg(
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_virtual(
        condition_id="0xc",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.08,
    )
    pos = await my_position_repo.upsert_virtual(
        condition_id="0xc",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.10,
    )
    assert pos is not None
    assert pos.size == 20.0
    assert pos.avg_price == pytest.approx(0.09, abs=1e-9)


async def test_upsert_virtual_sell_without_position_returns_none(
    my_position_repo: MyPositionRepository,
) -> None:
    result = await my_position_repo.upsert_virtual(
        condition_id="0xc",
        asset_id="A",
        side="SELL",
        size_filled=5.0,
        fill_price=0.07,
    )
    assert result is None


async def test_list_open_virtual_filters_segregation(
    my_position_repo: MyPositionRepository,
) -> None:
    # Réelle ouverte
    await my_position_repo.upsert_on_fill(
        condition_id="0xR",
        asset_id="A",
        side="BUY",
        size_filled=5.0,
        fill_price=0.5,
    )
    # Virtuelle ouverte
    await my_position_repo.upsert_virtual(
        condition_id="0xV",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.3,
    )
    # Virtuelle close
    closed = await my_position_repo.upsert_virtual(
        condition_id="0xVC",
        asset_id="B",
        side="BUY",
        size_filled=2.0,
        fill_price=0.4,
    )
    assert closed is not None
    await my_position_repo.close_virtual(
        closed.id,
        closed_at=datetime.now(tz=UTC),
        realized_pnl=1.0,
    )

    open_virtual = await my_position_repo.list_open_virtual()
    assert {p.condition_id for p in open_virtual} == {"0xV"}
    open_real = await my_position_repo.list_open()
    assert {p.condition_id for p in open_real} == {"0xR"}


async def test_close_virtual_sets_pnl_and_closed_at(
    my_position_repo: MyPositionRepository,
) -> None:
    pos = await my_position_repo.upsert_virtual(
        condition_id="0xc",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    assert pos is not None
    when = datetime(2026, 4, 18, 10, 30, tzinfo=UTC)
    await my_position_repo.close_virtual(pos.id, closed_at=when, realized_pnl=5.0)
    open_virtual = await my_position_repo.list_open_virtual()
    assert open_virtual == []


async def test_close_virtual_refuses_real_position(
    my_position_repo: MyPositionRepository,
) -> None:
    real = await my_position_repo.upsert_on_fill(
        condition_id="0xR",
        asset_id="A",
        side="BUY",
        size_filled=5.0,
        fill_price=0.5,
    )
    with pytest.raises(ValueError, match="not virtual"):
        await my_position_repo.close_virtual(
            real.id,
            closed_at=datetime.now(tz=UTC),
            realized_pnl=1.0,
        )


async def test_sum_realized_pnl_virtual(
    my_position_repo: MyPositionRepository,
) -> None:
    p1 = await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    p2 = await my_position_repo.upsert_virtual(
        condition_id="0xb",
        asset_id="B",
        side="BUY",
        size_filled=10.0,
        fill_price=0.6,
    )
    assert p1 is not None and p2 is not None
    await my_position_repo.close_virtual(p1.id, closed_at=datetime.now(tz=UTC), realized_pnl=10.0)
    await my_position_repo.close_virtual(p2.id, closed_at=datetime.now(tz=UTC), realized_pnl=-3.0)
    total = await my_position_repo.sum_realized_pnl_virtual()
    assert total == pytest.approx(7.0, abs=1e-9)


async def test_sum_realized_pnl_virtual_empty_returns_zero(
    my_position_repo: MyPositionRepository,
) -> None:
    assert await my_position_repo.sum_realized_pnl_virtual() == 0.0


async def test_unique_constraint_triple_allows_real_and_virtual_coexistence(
    my_position_repo: MyPositionRepository,
) -> None:
    # Réelle ouverte sur (cond, asset)
    await my_position_repo.upsert_on_fill(
        condition_id="0xC",
        asset_id="A",
        side="BUY",
        size_filled=5.0,
        fill_price=0.5,
    )
    # Virtuelle ouverte sur les mêmes (cond, asset, simulated=True) — doit passer
    pos = await my_position_repo.upsert_virtual(
        condition_id="0xC",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.3,
    )
    assert pos is not None
    open_virtual = await my_position_repo.list_open_virtual()
    open_real = await my_position_repo.list_open()
    assert len(open_virtual) == 1
    assert len(open_real) == 1


async def test_unique_constraint_triple_rejects_duplicate_real(
    my_position_repo: MyPositionRepository,
) -> None:
    """2 inserts directs réels (simulated=False) → IntegrityError sur la 2ᵉ.

    `upsert_on_fill` masque ce comportement par sa logique d'upsert ; on teste
    la contrainte au niveau du modèle.
    """
    from polycopy.storage.models import MyPosition

    factory = my_position_repo._session_factory  # type: ignore[attr-defined]
    async with factory() as session:
        session.add(
            MyPosition(
                condition_id="0xX",
                asset_id="A",
                size=1.0,
                avg_price=0.5,
                simulated=False,
            ),
        )
        await session.commit()
    async with factory() as session:
        session.add(
            MyPosition(
                condition_id="0xX",
                asset_id="A",
                size=1.0,
                avg_price=0.5,
                simulated=False,
            ),
        )
        with pytest.raises(IntegrityError):
            await session.commit()
