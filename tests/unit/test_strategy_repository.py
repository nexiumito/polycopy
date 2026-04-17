"""Tests du `StrategyDecisionRepository`."""

from polycopy.storage.dtos import StrategyDecisionDTO
from polycopy.storage.repositories import StrategyDecisionRepository


def _decision(
    tx: str, decision: str = "APPROVED", reason: str | None = None
) -> StrategyDecisionDTO:
    return StrategyDecisionDTO(
        detected_trade_id=0,
        tx_hash=tx,
        decision=decision,  # type: ignore[arg-type]
        reason=reason,
        my_size=10.0 if decision == "APPROVED" else None,
        my_price=0.5 if decision == "APPROVED" else None,
        slippage_pct=1.2 if decision == "APPROVED" else None,
        pipeline_state={"tx_hash": tx, "trace": [{"filter": "MarketFilter", "passed": True}]},
    )


async def test_insert_persists_decision(
    strategy_decision_repo: StrategyDecisionRepository,
) -> None:
    saved = await strategy_decision_repo.insert(_decision("0xtx1"))
    assert saved.id is not None
    assert saved.tx_hash == "0xtx1"
    assert saved.decision == "APPROVED"
    assert saved.my_size == 10.0
    assert saved.pipeline_state["tx_hash"] == "0xtx1"


async def test_list_recent_orders_by_decided_at_desc(
    strategy_decision_repo: StrategyDecisionRepository,
) -> None:
    for i in range(3):
        await strategy_decision_repo.insert(_decision(f"0xtx{i}"))
    recent = await strategy_decision_repo.list_recent(limit=2)
    assert len(recent) == 2
    # Le plus récent insert a le tx_hash le plus haut.
    assert recent[0].tx_hash == "0xtx2"


async def test_count_by_decision(
    strategy_decision_repo: StrategyDecisionRepository,
) -> None:
    await strategy_decision_repo.insert(_decision("0xa", "APPROVED"))
    await strategy_decision_repo.insert(_decision("0xb", "APPROVED"))
    await strategy_decision_repo.insert(_decision("0xc", "REJECTED", "liquidity_too_low"))
    counts = await strategy_decision_repo.count_by_decision()
    assert counts == {"APPROVED": 2, "REJECTED": 1}
