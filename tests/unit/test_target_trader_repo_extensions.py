"""Tests des extensions M5 de `TargetTraderRepository`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polycopy.storage.repositories import TargetTraderRepository


async def test_upsert_sets_pinned_status(
    target_trader_repo: TargetTraderRepository,
) -> None:
    """`upsert` (seed TARGET_WALLETS) doit marquer le wallet pinned."""
    trader = await target_trader_repo.upsert("0xABC")
    assert trader.status == "pinned"
    assert trader.pinned is True
    assert trader.active is True


async def test_insert_shadow_creates_observing_wallet(
    target_trader_repo: TargetTraderRepository,
) -> None:
    now = datetime.now(tz=UTC)
    trader = await target_trader_repo.insert_shadow(
        "0xDISCOVERED",
        label="auto:holders",
        discovered_at=now,
    )
    assert trader.status == "shadow"
    assert trader.active is False
    assert trader.pinned is False
    assert trader.discovered_at is not None


async def test_insert_shadow_idempotent(
    target_trader_repo: TargetTraderRepository,
) -> None:
    t1 = await target_trader_repo.insert_shadow("0xdupe")
    t2 = await target_trader_repo.insert_shadow("0xdupe")
    assert t1.id == t2.id


async def test_transition_status_shadow_to_active(
    target_trader_repo: TargetTraderRepository,
) -> None:
    await target_trader_repo.insert_shadow("0xshadow")
    result = await target_trader_repo.transition_status("0xshadow", new_status="active")
    assert result.status == "active"
    assert result.active is True
    assert result.promoted_at is not None


async def test_transition_status_active_to_paused_resets_hysteresis(
    target_trader_repo: TargetTraderRepository,
) -> None:
    await target_trader_repo.insert_shadow("0xw")
    await target_trader_repo.transition_status("0xw", new_status="active")
    # Simule 3 cycles low
    for _ in range(3):
        await target_trader_repo.increment_low_score("0xw")
    trader = await target_trader_repo.get("0xw")
    assert trader is not None
    assert trader.consecutive_low_score_cycles == 3

    result = await target_trader_repo.transition_status(
        "0xw",
        new_status="paused",
        reset_hysteresis=True,
    )
    assert result.status == "paused"
    assert result.active is False
    assert result.consecutive_low_score_cycles == 0


async def test_transition_status_raises_on_pinned(
    target_trader_repo: TargetTraderRepository,
) -> None:
    await target_trader_repo.upsert("0xpinned_seed")
    with pytest.raises(ValueError, match="pinned"):
        await target_trader_repo.transition_status("0xpinned_seed", new_status="paused")


async def test_transition_status_raises_on_unknown_wallet(
    target_trader_repo: TargetTraderRepository,
) -> None:
    with pytest.raises(ValueError, match="not found"):
        await target_trader_repo.transition_status("0xghost", new_status="paused")


async def test_update_score_sets_version_and_timestamp(
    target_trader_repo: TargetTraderRepository,
) -> None:
    await target_trader_repo.insert_shadow("0xscored")
    await target_trader_repo.update_score(
        "0xscored",
        score=0.72,
        scoring_version="v1",
    )
    trader = await target_trader_repo.get("0xscored")
    assert trader is not None
    assert trader.score == pytest.approx(0.72)
    assert trader.scoring_version == "v1"
    assert trader.last_scored_at is not None


async def test_list_active_excludes_shadow_and_paused(
    target_trader_repo: TargetTraderRepository,
) -> None:
    """Non-régression M1 : `list_active` ne retourne que les wallets effectivement pollés."""
    await target_trader_repo.upsert("0xpinned_seed")  # pinned → retourné
    await target_trader_repo.insert_shadow("0xshadow")  # shadow → exclu
    await target_trader_repo.insert_shadow("0xpromoted")
    await target_trader_repo.transition_status("0xpromoted", new_status="active")
    await target_trader_repo.insert_shadow("0xpaused")
    await target_trader_repo.transition_status("0xpaused", new_status="active")
    await target_trader_repo.transition_status("0xpaused", new_status="paused")

    actives = await target_trader_repo.list_active()
    addresses = {t.wallet_address for t in actives}
    assert addresses == {"0xpinned_seed", "0xpromoted"}


async def test_list_all_and_list_by_status(
    target_trader_repo: TargetTraderRepository,
) -> None:
    await target_trader_repo.upsert("0xa")
    await target_trader_repo.insert_shadow("0xb")
    await target_trader_repo.insert_shadow("0xc")

    all_ = await target_trader_repo.list_all()
    assert len(all_) == 3

    shadows = await target_trader_repo.list_by_status("shadow")
    assert {t.wallet_address for t in shadows} == {"0xb", "0xc"}

    assert await target_trader_repo.count_by_status("shadow") == 2
    assert await target_trader_repo.count_by_status("pinned") == 1


async def test_increment_and_reset_low_score(
    target_trader_repo: TargetTraderRepository,
) -> None:
    await target_trader_repo.insert_shadow("0xhyst")
    await target_trader_repo.transition_status("0xhyst", new_status="active")

    assert await target_trader_repo.increment_low_score("0xhyst") == 1
    assert await target_trader_repo.increment_low_score("0xhyst") == 2
    assert await target_trader_repo.increment_low_score("0xhyst") == 3

    await target_trader_repo.reset_low_score("0xhyst")
    trader = await target_trader_repo.get("0xhyst")
    assert trader is not None
    assert trader.consecutive_low_score_cycles == 0
