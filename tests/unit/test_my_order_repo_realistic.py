"""Tests M8 — ``MyOrderRepository.insert_realistic_simulated``."""

from __future__ import annotations

from polycopy.storage.dtos import RealisticSimulatedOrderDTO
from polycopy.storage.repositories import MyOrderRepository


def _dto(
    *,
    status: str = "SIMULATED",
    error_msg: str | None = None,
    side: str = "BUY",
) -> RealisticSimulatedOrderDTO:
    return RealisticSimulatedOrderDTO(
        source_tx_hash="0xtx",
        condition_id="0xc",
        asset_id="A",
        side=side,  # type: ignore[arg-type]
        size=10.0,
        price=0.085,
        tick_size=0.001,
        neg_risk=False,
        status=status,  # type: ignore[arg-type]
        error_msg=error_msg,
    )


async def test_insert_realistic_simulated_sets_flags(
    my_order_repo: MyOrderRepository,
) -> None:
    order = await my_order_repo.insert_realistic_simulated(_dto())
    assert order.simulated is True
    assert order.realistic_fill is True
    assert order.status == "SIMULATED"
    assert order.clob_order_id is None


async def test_insert_realistic_simulated_rejected_persists_error_msg(
    my_order_repo: MyOrderRepository,
) -> None:
    order = await my_order_repo.insert_realistic_simulated(
        _dto(status="REJECTED", error_msg="insufficient_liquidity"),
    )
    assert order.status == "REJECTED"
    assert order.error_msg == "insufficient_liquidity"
    assert order.realistic_fill is True
