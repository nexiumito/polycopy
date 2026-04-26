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


async def test_active_under_hard_floor_two_cycles_no_demote(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """M15 MB.3 : sous absolute_hard_floor (<0.30), hystérésis incrémentée
    mais pas encore demote (3 cycles requis).

    Adapté du test M5 ``test_active_under_threshold_two_cycles_no_demote``.
    Sémantique changée : la branche absolute_hard_floor de MB.3 remplace le
    threshold M5 ``score < SCORING_DEMOTION_THRESHOLD=0.40``. Pour tester
    l'hystérésis, on utilise des scores < 0.30 (default
    ``SCORING_ABSOLUTE_HARD_FLOOR``) — plus exigeant qu'auparavant mais le
    chemin code testé est strictement le même (incrément + check seuil).
    """
    await target_trader_repo.insert_shadow("0xunder")
    await target_trader_repo.transition_status("0xunder", new_status="active")
    engine = DecisionEngine(target_trader_repo, _settings(), alerts_queue)
    # Cycle 1 — score sous hard floor 0.30
    cur = await target_trader_repo.get("0xunder")
    d1 = await engine.decide(_scoring("0xunder", 0.20), cur, active_count=3)
    assert d1.decision == "keep"
    # Cycle 2 — toujours sous hard floor
    cur = await target_trader_repo.get("0xunder")
    d2 = await engine.decide(_scoring("0xunder", 0.15), cur, active_count=3)
    assert d2.decision == "keep"
    cur = await target_trader_repo.get("0xunder")
    assert cur is not None and cur.status == "active"
    assert cur.consecutive_low_score_cycles == 2


async def test_active_under_hard_floor_three_cycles_demotes_to_shadow(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """M15 MB.3 : 3 cycles sous absolute_hard_floor déclenchent demote shadow.

    Adapté du test M5_bis Phase C
    ``test_active_under_threshold_three_cycles_demotes_to_shadow``. Le
    chemin code (transition active→shadow + previously_demoted_at) est
    strictement préservé — c'est le critère d'entrée qui change : 3 cycles
    sous ``SCORING_ABSOLUTE_HARD_FLOOR=0.30`` (vs 3 cycles sous
    ``SCORING_DEMOTION_THRESHOLD=0.40`` en M5).
    """
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


# --- M15 MB.3 : ranking-based _decide_active (5 tests §9.3) -----------------


async def _seed_active_with_score(
    repo: TargetTraderRepository,
    wallet: str,
    score: float,
    *,
    pinned: bool = False,
) -> None:
    """Helper : insère un wallet en active avec un score posé via update_score."""
    await repo.insert_shadow(wallet)
    if pinned:
        await repo.transition_status_unsafe(wallet, new_status="pinned")
    else:
        await repo.transition_status(wallet, new_status="active")
    await repo.update_score(wallet, score=score, scoring_version="v2.1.1")


async def test_decide_active_ranking_based_demotes_out_of_top_n(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """MB.3 §9.3 #9 — wallet rank 11/10 (out of top-N) demote après 3 cycles."""
    settings = _settings(max_active_traders=10)
    # Seed pool : 10 wallets actifs avec scores 0.40..0.85, +1 wallet courant
    # à 0.31 → rank 11/11 (out-of-top-10).
    for i in range(10):
        await _seed_active_with_score(
            target_trader_repo,
            f"0xa{i:02d}",
            0.40 + i * 0.05,
        )
    await _seed_active_with_score(target_trader_repo, "0xouter", 0.31)

    engine = DecisionEngine(target_trader_repo, settings, alerts_queue)
    decisions = []
    for _ in range(3):
        cur = await target_trader_repo.get("0xouter")
        decisions.append(
            await engine.decide(_scoring("0xouter", 0.31), cur, active_count=11),
        )
    assert decisions[0].decision == "keep"
    assert decisions[0].event_metadata["ranking_basis"] == "top_n"
    assert decisions[2].decision == "demote_shadow"
    assert decisions[2].to_status == "shadow"
    assert decisions[2].event_metadata["ranking_basis"] == "top_n"


async def test_decide_active_hysteresis_resets_when_back_in_top_n(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """MB.3 §9.3 #10 — out-of-top-N puis back-in → hystérésis reset."""
    settings = _settings(max_active_traders=3)
    # Pool : 3 actifs au-dessus du wallet test.
    for w, s in (("0xtop1", 0.80), ("0xtop2", 0.75), ("0xtop3", 0.70)):
        await _seed_active_with_score(target_trader_repo, w, s)
    await _seed_active_with_score(target_trader_repo, "0xtest", 0.50)

    engine = DecisionEngine(target_trader_repo, settings, alerts_queue)
    # Cycle T+0 : rank 4/4 (out-of-top-3) → +1 hystérésis.
    cur = await target_trader_repo.get("0xtest")
    await engine.decide(_scoring("0xtest", 0.50), cur, active_count=4)
    cur = await target_trader_repo.get("0xtest")
    assert cur is not None and cur.consecutive_low_score_cycles == 1

    # Cycle T+1 : on remonte le score à 0.78 → rank 1/4 (in top-3).
    await target_trader_repo.update_score(
        "0xtest",
        score=0.78,
        scoring_version="v2.1.1",
    )
    cur = await target_trader_repo.get("0xtest")
    decision = await engine.decide(_scoring("0xtest", 0.78), cur, active_count=4)
    assert decision.decision == "keep"
    cur = await target_trader_repo.get("0xtest")
    assert cur is not None and cur.consecutive_low_score_cycles == 0


async def test_decide_active_absolute_hard_floor_safeguard_still_fires(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """MB.3 §9.3 #11 — pool entièrement bas, wallet dans top-N mais score
    sous absolute_hard_floor → force-demote après hystérésis.
    """
    settings = _settings(
        max_active_traders=10,
        scoring_absolute_hard_floor=0.30,
    )
    # Seed 10 wallets tous avec scores bas (≤ 0.30). Le wallet test à 0.20
    # est dans le top-10 (pool sub-cap aussi possible) mais sous hard floor.
    for i in range(9):
        await _seed_active_with_score(
            target_trader_repo,
            f"0xb{i:02d}",
            0.05 + i * 0.02,
        )
    await _seed_active_with_score(target_trader_repo, "0xfloor", 0.20)

    engine = DecisionEngine(target_trader_repo, settings, alerts_queue)
    decisions = []
    for _ in range(3):
        cur = await target_trader_repo.get("0xfloor")
        decisions.append(
            await engine.decide(_scoring("0xfloor", 0.20), cur, active_count=10),
        )
    assert decisions[2].decision == "demote_shadow"
    assert decisions[2].event_metadata["ranking_basis"] == "absolute_floor"


async def test_decide_active_pinned_never_demoted(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """MB.3 §9.3 #12 — pinned wallet jamais demote (safeguard M5)."""
    settings = _settings(max_active_traders=3)
    await _seed_active_with_score(
        target_trader_repo,
        "0xpin",
        0.05,
        pinned=True,
    )
    engine = DecisionEngine(target_trader_repo, settings, alerts_queue)
    for _ in range(10):
        cur = await target_trader_repo.get("0xpin")
        d = await engine.decide(_scoring("0xpin", 0.05), cur, active_count=1)
        assert d.decision == "keep"
        assert d.from_status == "pinned"
    cur = await target_trader_repo.get("0xpin")
    assert cur is not None
    # Aucun incrément hystérésis sur pinned (le path court-circuite avant
    # _decide_active).
    assert cur.consecutive_low_score_cycles == 0


async def test_decide_active_pool_sub_cap_no_demote(
    target_trader_repo: TargetTraderRepository,
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """MB.3 §9.3 #13 — pool sub-cap (active_count < cap), personne hors
    top-N → personne demote (régression test contre churn artificiel).
    """
    settings = _settings(max_active_traders=10)
    # Pool 5 wallets seulement (sub-cap 10).
    for w, s in (
        ("0xa1", 0.65),
        ("0xa2", 0.60),
        ("0xa3", 0.55),
        ("0xa4", 0.50),
        ("0xa5", 0.40),  # rank 5/5, mais 5 < 10 (cap)
    ):
        await _seed_active_with_score(target_trader_repo, w, s)

    engine = DecisionEngine(target_trader_repo, settings, alerts_queue)
    cur = await target_trader_repo.get("0xa5")
    decision = await engine.decide(_scoring("0xa5", 0.40), cur, active_count=5)
    assert decision.decision == "keep"
    assert decision.event_metadata["ranking_basis"] == "top_n"
    cur = await target_trader_repo.get("0xa5")
    assert cur is not None and cur.consecutive_low_score_cycles == 0


# --- M15 MB.8 : auto-blacklist sur PnL/WR observé (3 tests §9.8) -----------


async def _seed_my_position(
    my_position_repo,  # type: ignore[no-untyped-def]
    *,
    source_wallet: str,
    realized_pnl: float,
    closed_at: datetime,
    asset_id: str,
    condition_id: str,
    simulated: bool = True,
) -> None:
    """Insert direct d'une MyPosition closed via la session pour contrôle
    précis de closed_at + realized_pnl + source_wallet_address.
    """
    from polycopy.storage.models import MyPosition

    async with my_position_repo._session_factory() as session:  # noqa: SLF001
        position = MyPosition(
            condition_id=condition_id,
            asset_id=asset_id,
            size=0.0,
            avg_price=0.5,
            simulated=simulated,
            closed_at=closed_at,
            realized_pnl=realized_pnl,
            source_wallet_address=source_wallet.lower(),
        )
        session.add(position)
        await session.commit()


async def test_auto_blacklist_fires_on_pnl_threshold(
    target_trader_repo: TargetTraderRepository,
    my_position_repo,  # type: ignore[no-untyped-def]
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """MB.8 §9.8 #25 — wallet ACTIVE avec PnL cumulé < -$5 → auto-blacklist."""
    wallet = "0xtoxic"
    await _seed_active_with_score(target_trader_repo, wallet, 0.55)
    # 15 closed positions cumulant -$8.50 sur 30j (>>−$5 threshold).
    now = datetime.now(tz=UTC)
    for i in range(15):
        await _seed_my_position(
            my_position_repo,
            source_wallet=wallet,
            realized_pnl=-0.567,
            closed_at=now - timedelta(days=i),
            asset_id=f"0xa{i}",
            condition_id=f"0xc{i}",
        )

    settings = _settings(
        execution_mode="dry_run",
        auto_blacklist_pnl_threshold_usd="-5.0",
        auto_blacklist_min_positions_for_wr=30,
    )
    engine = DecisionEngine(
        target_trader_repo,
        settings,
        alerts_queue,
        my_positions_repo=my_position_repo,
    )
    cur = await target_trader_repo.get(wallet)
    decision = await engine.decide(_scoring(wallet, 0.55), cur, active_count=1)

    assert decision.event_metadata.get("auto_blacklist") is True
    assert decision.event_metadata["reason_code"] == "pnl_threshold"
    assert decision.to_status == "blacklisted"

    after = await target_trader_repo.get(wallet)
    assert after is not None and after.status == "blacklisted"

    # Alerte Telegram émise.
    alert = alerts_queue.get_nowait()
    assert alert.event == "trader_auto_blacklisted"
    assert alert.level == "WARNING"


async def test_auto_blacklist_fires_on_win_rate_floor(
    target_trader_repo: TargetTraderRepository,
    my_position_repo,  # type: ignore[no-untyped-def]
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """MB.8 §9.8 #26 — wallet avec 30+ positions et WR<25% → auto-blacklist
    par critère win_rate_floor (PnL non sous threshold).
    """
    wallet = "0xpoorwr"
    await _seed_active_with_score(target_trader_repo, wallet, 0.55)
    now = datetime.now(tz=UTC)
    # 6 wins (PnL +0.10) + 29 losses (PnL -0.05) → WR=6/35=0.171 < 0.25.
    # PnL total = 6*0.10 + 29*(-0.05) = 0.60 - 1.45 = -0.85 (au-dessus de -$5).
    for i in range(6):
        await _seed_my_position(
            my_position_repo,
            source_wallet=wallet,
            realized_pnl=0.10,
            closed_at=now - timedelta(days=i),
            asset_id=f"0xwin{i}",
            condition_id=f"0xcwin{i}",
        )
    for i in range(29):
        await _seed_my_position(
            my_position_repo,
            source_wallet=wallet,
            realized_pnl=-0.05,
            closed_at=now - timedelta(days=i),
            asset_id=f"0xlos{i}",
            condition_id=f"0xclos{i}",
        )

    settings = _settings(
        execution_mode="dry_run",
        auto_blacklist_pnl_threshold_usd="-5.0",
        auto_blacklist_min_positions_for_wr=30,
    )
    engine = DecisionEngine(
        target_trader_repo,
        settings,
        alerts_queue,
        my_positions_repo=my_position_repo,
    )
    cur = await target_trader_repo.get(wallet)
    decision = await engine.decide(_scoring(wallet, 0.55), cur, active_count=1)

    assert decision.event_metadata.get("auto_blacklist") is True
    assert decision.event_metadata["reason_code"] == "win_rate_floor"
    assert decision.to_status == "blacklisted"


async def test_auto_blacklist_idempotent_when_already_blacklisted(
    target_trader_repo: TargetTraderRepository,
    my_position_repo,  # type: ignore[no-untyped-def]
    alerts_queue: asyncio.Queue[Alert],
) -> None:
    """MB.8 §9.8 #27 — wallet déjà blacklisted → no path through MB.8.

    Le path `_decide_active` n'est pas atteint pour un wallet déjà
    blacklisted (filtré en amont par `decide()` via la branche
    `current.status == "blacklisted"`).
    """
    wallet = "0xalreadybl"
    await target_trader_repo.insert_shadow(wallet)
    await target_trader_repo.transition_status_unsafe(
        wallet,
        new_status="blacklisted",
    )

    settings = _settings(execution_mode="dry_run")
    engine = DecisionEngine(
        target_trader_repo,
        settings,
        alerts_queue,
        my_positions_repo=my_position_repo,
    )
    cur = await target_trader_repo.get(wallet)
    decision = await engine.decide(_scoring(wallet, 0.99), cur, active_count=1)

    # Branche blacklisted — keep, pas auto_blacklist.
    assert decision.decision == "keep"
    assert decision.from_status == "blacklisted"
    assert decision.to_status == "blacklisted"
    assert "auto_blacklist" not in decision.event_metadata
    # Pas d'alerte Telegram.
    assert alerts_queue.empty()
