"""Tests du `MyOrderRepository`."""

from datetime import UTC, datetime

from polycopy.storage.dtos import MyOrderDTO
from polycopy.storage.repositories import MyOrderRepository


def _dto(
    tx: str = "0xtx1",
    *,
    status: str = "SIMULATED",
    simulated: bool = True,
) -> MyOrderDTO:
    return MyOrderDTO(
        source_tx_hash=tx,
        condition_id="0xcond",
        asset_id="123",
        side="BUY",
        size=10.0,
        price=0.5,
        tick_size=0.01,
        neg_risk=False,
        order_type="FOK",
        status=status,  # type: ignore[arg-type]
        simulated=simulated,
    )


async def test_insert_persists_simulated_order(
    my_order_repo: MyOrderRepository,
) -> None:
    saved = await my_order_repo.insert(_dto())
    assert saved.id is not None
    assert saved.source_tx_hash == "0xtx1"
    assert saved.status == "SIMULATED"
    assert saved.simulated is True
    assert saved.tick_size == 0.01


async def test_update_status_sent_to_filled(
    my_order_repo: MyOrderRepository,
) -> None:
    saved = await my_order_repo.insert(_dto(status="SENT", simulated=False))
    await my_order_repo.update_status(
        saved.id,
        "FILLED",
        clob_order_id="0xclob",
        taking_amount="200000000",
        making_amount="100000000",
        transaction_hashes=["0xtx_onchain"],
        filled_at=datetime.now(tz=UTC),
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "FILLED"
    assert recent[0].clob_order_id == "0xclob"
    assert recent[0].taking_amount == "200000000"
    assert recent[0].transaction_hashes == ["0xtx_onchain"]
    assert recent[0].filled_at is not None


async def test_update_status_unknown_id_raises(
    my_order_repo: MyOrderRepository,
) -> None:
    import pytest

    with pytest.raises(ValueError, match="not found"):
        await my_order_repo.update_status(99999, "FAILED", error_msg="never existed")


async def test_list_recent_orders_by_sent_at_desc(
    my_order_repo: MyOrderRepository,
) -> None:
    for i in range(3):
        await my_order_repo.insert(_dto(tx=f"0xtx{i}"))
    recent = await my_order_repo.list_recent(limit=2)
    assert len(recent) == 2
    # Le plus récent insert a le tx_hash le plus haut.
    assert recent[0].source_tx_hash == "0xtx2"
