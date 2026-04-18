"""Tests de `TraderScoreRepository` + `TraderEventRepository` (M5 append-only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polycopy.storage.dtos import TraderEventDTO, TraderScoreDTO
from polycopy.storage.repositories import (
    TargetTraderRepository,
    TraderEventRepository,
    TraderScoreRepository,
)


def _score_dto(
    trader_id: int,
    wallet: str,
    score: float,
    *,
    version: str = "v1",
    low_conf: bool = False,
) -> TraderScoreDTO:
    return TraderScoreDTO(
        target_trader_id=trader_id,
        wallet_address=wallet,
        score=score,
        scoring_version=version,
        low_confidence=low_conf,
        metrics_snapshot={"win_rate": 0.6},
    )


async def test_trader_score_insert_and_latest(
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    t = await target_trader_repo.insert_shadow("0xscored")
    await trader_score_repo.insert(_score_dto(t.id, "0xscored", 0.5))
    await trader_score_repo.insert(_score_dto(t.id, "0xscored", 0.7))

    latest = await trader_score_repo.latest_for_wallet("0xscored")
    assert latest is not None
    assert latest.score == 0.7


async def test_trader_score_list_for_wallet_ordered(
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    t = await target_trader_repo.insert_shadow("0xhist")
    for s in (0.2, 0.4, 0.6):
        await trader_score_repo.insert(_score_dto(t.id, "0xhist", s))
    history = await trader_score_repo.list_for_wallet("0xhist")
    # Ordre décroissant par cycle_at → dernière entrée d'abord.
    assert [h.score for h in history] == [0.6, 0.4, 0.2]


async def test_trader_score_latest_per_wallet(
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    a = await target_trader_repo.insert_shadow("0xa")
    b = await target_trader_repo.insert_shadow("0xb")
    await trader_score_repo.insert(_score_dto(a.id, "0xa", 0.3))
    await trader_score_repo.insert(_score_dto(a.id, "0xa", 0.8))  # latest A
    await trader_score_repo.insert(_score_dto(b.id, "0xb", 0.9))  # latest B
    await trader_score_repo.insert(_score_dto(b.id, "0xb", 0.4))

    result = await trader_score_repo.latest_per_wallet()
    by_wallet = {r.wallet_address: r.score for r in result}
    # La sous-requête retient le max(cycle_at) donc la DERNIÈRE ligne insérée par wallet.
    assert by_wallet["0xa"] == 0.8
    assert by_wallet["0xb"] == 0.4


async def test_trader_event_insert_and_list_recent(
    trader_event_repo: TraderEventRepository,
) -> None:
    await trader_event_repo.insert(
        TraderEventDTO(
            wallet_address="0xevent",
            event_type="discovered",
            to_status="shadow",
            reason="via /holders bootstrap",
        ),
    )
    await trader_event_repo.insert(
        TraderEventDTO(
            wallet_address="0xevent",
            event_type="promoted_active",
            from_status="shadow",
            to_status="active",
            score_at_event=0.72,
            scoring_version="v1",
        ),
    )
    events = await trader_event_repo.list_recent()
    assert len(events) == 2
    # Ordre décroissant : promoted_active en premier.
    assert events[0].event_type == "promoted_active"
    assert events[0].score_at_event == 0.72


async def test_trader_event_count_by_type_since(
    trader_event_repo: TraderEventRepository,
) -> None:
    for et in ("promoted_active", "promoted_active", "demoted_paused"):
        await trader_event_repo.insert(
            TraderEventDTO(wallet_address="0xw", event_type=et),
        )
    since = datetime.now(tz=UTC) - timedelta(hours=1)
    counts = await trader_event_repo.count_by_event_type_since(since)
    assert counts == {"promoted_active": 2, "demoted_paused": 1}


async def test_trader_event_list_since_filters(
    trader_event_repo: TraderEventRepository,
) -> None:
    await trader_event_repo.insert(
        TraderEventDTO(wallet_address="0xw", event_type="discovered"),
    )
    # Since in the future → 0 résultats.
    future = datetime.now(tz=UTC) + timedelta(hours=1)
    assert await trader_event_repo.list_recent(since=future) == []
