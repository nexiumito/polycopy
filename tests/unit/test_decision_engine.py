"""Tests exhaustifs du DecisionEngine (couvre les 11 scénarios de la spec §9.6)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from polycopy.config import Settings
from polycopy.discovery.decision_engine import DecisionEngine
from polycopy.discovery.dtos import ScoringResult, TraderMetrics
from polycopy.monitoring.dtos import Alert
from polycopy.storage.repositories import TargetTraderRepository


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "target_wallets": "0xdummy",
        "scoring_version": "v1",
        "scoring_promotion_threshold": 0.65,
        "scoring_demotion_threshold": 0.40,
        "scoring_demotion_hysteresis_cycles": 3,
        "trader_shadow_days": 7,
        "max_active_traders": 10,
        "blacklisted_wallets": "",
        "discovery_shadow_bypass": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _scoring(wallet: str, score: float, version: str = "v1") -> ScoringResult:
    return ScoringResult(
        wallet_address=wallet,
        score=score,
        scoring_version=version,
        low_confidence=False,
        metrics=TraderMetrics(wallet_address=wallet, fetched_at=datetime.now(tz=UTC)),
        cycle_at=datetime.now(tz=UTC),
    )


@pytest.fixture
async def alerts_queue() -> asyncio.Queue[Alert]:
    return asyncio.Queue(maxsize=10)


# ---------------------------------------------------------------------
# Scénarios du §9.6 + coverage du skip_blacklist.
# ---------------------------------------------------------------------


async def test_absent_wallet_high_score_discovered_shadow(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    d = await engine.decide(_scoring("0xnew", 0.80), None, active_count=3)
    assert d.decision == "discovered_shadow"
    assert d.to_status == "shadow"
    # Wallet inséré en DB
    t = await target_trader_repo.get("0xnew")
    assert t is not None and t.status == "shadow"


async def test_absent_wallet_low_score_keeps_absent(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    d = await engine.decide(_scoring("0xghost", 0.10), None, active_count=3)
    assert d.decision == "keep"
    assert d.to_status == "absent"
    assert await target_trader_repo.get("0xghost") is None


async def test_absent_wallet_shadow_bypass_promotes_direct(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    s = _settings(trader_shadow_days=0, discovery_shadow_bypass=True)
    engine = DecisionEngine(target_trader_repo, s, alerts_queue)
    d = await engine.decide(_scoring("0xbypass", 0.90), None, active_count=0)
    assert d.decision == "promote_active"
    assert d.to_status == "active"
    assert d.event_metadata["bypass_shadow"] is True
    t = await target_trader_repo.get("0xbypass")
    assert t is not None and t.status == "active"


async def test_absent_wallet_high_score_cap_reached_skips(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    s = _settings(max_active_traders=2)
    engine = DecisionEngine(target_trader_repo, s, alerts_queue)
    d = await engine.decide(_scoring("0xcandidate", 0.90), None, active_count=2)
    assert d.decision == "skip_cap"
    assert await target_trader_repo.get("0xcandidate") is None
    # Alerte poussée
    alert = await asyncio.wait_for(alerts_queue.get(), timeout=0.1)
    assert alert.event == "discovery_cap_reached"


async def test_blacklist_returns_skip_no_write(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    s = _settings(blacklisted_wallets="0xBAD")
    engine = DecisionEngine(target_trader_repo, s, alerts_queue)
    d = await engine.decide(_scoring("0xbad", 0.99), None, active_count=0)
    assert d.decision == "skip_blacklist"
    assert await target_trader_repo.get("0xbad") is None
    assert alerts_queue.empty()


async def test_pinned_wallet_always_kept(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    await target_trader_repo.upsert("0xpin")  # upsert → pinned
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    current = await target_trader_repo.get("0xpin")
    d = await engine.decide(_scoring("0xpin", 0.01), current, active_count=5)
    assert d.decision == "keep"
    assert d.to_status == "pinned"
    # Status reste pinned
    after = await target_trader_repo.get("0xpin")
    assert after is not None and after.status == "pinned"


async def test_shadow_days_not_elapsed_kept(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    await target_trader_repo.insert_shadow(
        "0xshort",
        discovered_at=datetime.now(tz=UTC),  # ~0j
    )
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    current = await target_trader_repo.get("0xshort")
    d = await engine.decide(_scoring("0xshort", 0.90), current, active_count=0)
    assert d.decision == "keep"
    assert d.to_status == "shadow"


async def test_shadow_elapsed_and_high_score_promotes(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    await target_trader_repo.insert_shadow(
        "0xripe",
        discovered_at=datetime.now(tz=UTC) - timedelta(days=10),
    )
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    current = await target_trader_repo.get("0xripe")
    d = await engine.decide(_scoring("0xripe", 0.80), current, active_count=2)
    assert d.decision == "promote_active"
    assert d.to_status == "active"


async def test_active_score_above_threshold_resets_hysteresis(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    await target_trader_repo.insert_shadow("0xok")
    await target_trader_repo.transition_status("0xok", new_status="active")
    await target_trader_repo.increment_low_score("0xok")
    await target_trader_repo.increment_low_score("0xok")
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    current = await target_trader_repo.get("0xok")
    d = await engine.decide(_scoring("0xok", 0.75), current, active_count=3)
    assert d.decision == "keep"
    after = await target_trader_repo.get("0xok")
    assert after is not None and after.consecutive_low_score_cycles == 0


async def test_active_under_threshold_two_cycles_no_demote(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    await target_trader_repo.insert_shadow("0xunder")
    await target_trader_repo.transition_status("0xunder", new_status="active")
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    # Cycle 1
    cur = await target_trader_repo.get("0xunder")
    d1 = await engine.decide(_scoring("0xunder", 0.30), cur, active_count=3)
    assert d1.decision == "keep"
    # Cycle 2
    cur = await target_trader_repo.get("0xunder")
    d2 = await engine.decide(_scoring("0xunder", 0.25), cur, active_count=3)
    assert d2.decision == "keep"
    cur = await target_trader_repo.get("0xunder")
    assert cur is not None and cur.status == "active"
    assert cur.consecutive_low_score_cycles == 2


async def test_active_under_threshold_three_cycles_demotes_to_shadow(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """M5_bis Phase C : la demote active → shadow (ex-paused) + previously_demoted_at."""
    await target_trader_repo.insert_shadow("0xout")
    await target_trader_repo.transition_status("0xout", new_status="active")
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    decisions = []
    for _ in range(3):
        cur = await target_trader_repo.get("0xout")
        decisions.append(
            await engine.decide(_scoring("0xout", 0.20), cur, active_count=3),
        )
    assert decisions[0].decision == "keep"
    assert decisions[1].decision == "keep"
    assert decisions[2].decision == "demote_shadow"
    assert decisions[2].to_status == "shadow"
    after = await target_trader_repo.get("0xout")
    assert after is not None
    assert after.status == "shadow"
    assert after.consecutive_low_score_cycles == 0  # reset après demote
    assert after.previously_demoted_at is not None  # flag UX M5_bis


async def test_paused_legacy_defensive_keep(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """Compat : un wallet en status='paused' (legacy / downgrade) reste keep.

    La migration 0007 convertit les paused → shadow. Si un wallet se retrouve
    encore en paused (downgrade DB, seed manuel, race), le DecisionEngine le
    laisse tranquille avec un log WARNING.
    """
    await target_trader_repo.insert_shadow("0xlegacy")
    await target_trader_repo.transition_status("0xlegacy", new_status="active")
    await target_trader_repo.transition_status("0xlegacy", new_status="paused")
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    cur = await target_trader_repo.get("0xlegacy")
    d = await engine.decide(_scoring("0xlegacy", 0.85), cur, active_count=3)
    assert d.decision == "keep"
    assert d.from_status == "paused"
    assert d.to_status == "paused"


async def test_sell_only_returns_keep(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """M5_bis Phase C : un wallet sell_only reçoit keep de DecisionEngine.

    Le lifecycle sell_only (T6/T7/T8) est piloté par EvictionScheduler, pas
    par DecisionEngine. DecisionEngine se contente de keep pour ne pas
    interférer.
    """
    await target_trader_repo.insert_shadow("0xsell")
    await target_trader_repo.transition_status("0xsell", new_status="active")
    await target_trader_repo.transition_status("0xsell", new_status="sell_only")
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    cur = await target_trader_repo.get("0xsell")
    d = await engine.decide(_scoring("0xsell", 0.85), cur, active_count=3)
    assert d.decision == "keep"
    assert d.from_status == "sell_only"
    assert d.to_status == "sell_only"
    # Score quand même écrit (trader_scores par l'orchestrator).
    assert d.score_at_event == 0.85


async def test_blacklisted_status_returns_keep(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """M5_bis Phase C : un wallet déjà blacklisted (status='blacklisted') → keep.

    La transition T10/T11/T12 est pilotée par EvictionScheduler.reconcile_blacklist,
    pas par DecisionEngine. Distinct de skip_blacklist qui s'applique quand
    BLACKLISTED_WALLETS contient le wallet mais qu'il n'est pas encore en
    status='blacklisted'.
    """
    await target_trader_repo.insert_shadow("0xbl")
    await target_trader_repo.transition_status_unsafe(
        "0xbl",
        new_status="blacklisted",
    )
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    cur = await target_trader_repo.get("0xbl")
    d = await engine.decide(_scoring("0xbl", 0.99), cur, active_count=3)
    assert d.decision == "keep"
    assert d.to_status == "blacklisted"
