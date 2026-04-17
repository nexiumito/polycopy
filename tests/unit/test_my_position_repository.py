"""Tests du `MyPositionRepository.upsert_on_fill`."""

import pytest

from polycopy.storage.repositories import MyPositionRepository

_COND = "0xcond"
_ASSET = "123"


async def test_first_buy_creates_position(
    my_position_repo: MyPositionRepository,
) -> None:
    pos = await my_position_repo.upsert_on_fill(
        condition_id=_COND,
        asset_id=_ASSET,
        side="BUY",
        size_filled=10.0,
        fill_price=0.5,
    )
    assert pos.size == 10.0
    assert pos.avg_price == 0.5
    assert pos.closed_at is None


async def test_second_buy_cumulates_with_weighted_avg_price(
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_on_fill(
        condition_id=_COND,
        asset_id=_ASSET,
        side="BUY",
        size_filled=10.0,
        fill_price=0.5,
    )
    pos = await my_position_repo.upsert_on_fill(
        condition_id=_COND,
        asset_id=_ASSET,
        side="BUY",
        size_filled=10.0,
        fill_price=0.7,
    )
    # avg = (10*0.5 + 10*0.7) / 20 = 0.6
    assert pos.size == 20.0
    assert pos.avg_price == pytest.approx(0.6)


async def test_partial_sell_decrements_size_keeps_avg(
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_on_fill(
        condition_id=_COND,
        asset_id=_ASSET,
        side="BUY",
        size_filled=10.0,
        fill_price=0.5,
    )
    pos = await my_position_repo.upsert_on_fill(
        condition_id=_COND,
        asset_id=_ASSET,
        side="SELL",
        size_filled=4.0,
        fill_price=0.55,
    )
    assert pos.size == 6.0
    assert pos.avg_price == 0.5  # inchangé sur SELL
    assert pos.closed_at is None


async def test_full_sell_closes_position(
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_on_fill(
        condition_id=_COND,
        asset_id=_ASSET,
        side="BUY",
        size_filled=10.0,
        fill_price=0.5,
    )
    pos = await my_position_repo.upsert_on_fill(
        condition_id=_COND,
        asset_id=_ASSET,
        side="SELL",
        size_filled=10.0,
        fill_price=0.55,
    )
    assert pos.size == 0.0
    assert pos.closed_at is not None
    open_positions = await my_position_repo.list_open()
    assert open_positions == []


async def test_sell_without_open_raises(
    my_position_repo: MyPositionRepository,
) -> None:
    with pytest.raises(ValueError, match="non-existent position"):
        await my_position_repo.upsert_on_fill(
            condition_id=_COND,
            asset_id=_ASSET,
            side="SELL",
            size_filled=1.0,
            fill_price=0.5,
        )


async def test_get_open_returns_position_or_none(
    my_position_repo: MyPositionRepository,
) -> None:
    assert await my_position_repo.get_open(_COND) is None
    await my_position_repo.upsert_on_fill(
        condition_id=_COND,
        asset_id=_ASSET,
        side="BUY",
        size_filled=1.0,
        fill_price=0.5,
    )
    pos = await my_position_repo.get_open(_COND)
    assert pos is not None
    assert pos.size == 1.0
