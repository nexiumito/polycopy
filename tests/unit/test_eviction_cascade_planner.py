"""Tests CascadePlanner M5_bis Phase B.7.

Planner pur, sans DB ni async — testable en stateless.
"""

from __future__ import annotations

from polycopy.discovery.eviction import CascadePlanner, TraderSnapshot


def _snap(
    wallet: str,
    status: str,
    score: float | None,
    *,
    pinned: bool = False,
) -> TraderSnapshot:
    return TraderSnapshot(
        wallet_address=wallet,
        status=status,
        score=score,
        pinned=pinned,
    )


def test_plan_returns_top_delta_candidate() -> None:
    """Le candidat retenu est celui avec la plus grande delta."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan(
        [
            _snap("0xworst", "active", 0.55),
            _snap("0xok", "active", 0.80),
            _snap("0xcand_high", "shadow", 0.95),  # delta = 0.40
            _snap("0xcand_low", "shadow", 0.72),  # delta = 0.17
        ],
    )
    assert plan.promote_candidate is not None
    assert plan.promote_candidate.wallet_address == "0xcand_high"
    assert plan.demote_worst == "0xworst"
    # Le low candidat est deferred.
    assert len(plan.deferred_one_per_cycle) == 1
    assert plan.deferred_one_per_cycle[0].wallet_address == "0xcand_low"


def test_plan_returns_noop_when_margin_not_met() -> None:
    """Aucun candidat avec delta ≥ margin → no-op."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan(
        [
            _snap("0xworst", "active", 0.70),
            _snap("0xcand", "shadow", 0.80),  # delta 0.10 < 0.15
        ],
    )
    assert plan.promote_candidate is None
    assert plan.demote_worst is None


def test_plan_excludes_pinned_from_worst() -> None:
    """EC-7 : un pinned n'est jamais worst_active."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan(
        [
            _snap("0xpinned_low", "pinned", 0.40, pinned=True),  # score bas mais pinned
            _snap("0xworst", "active", 0.55),  # vrai worst
            _snap("0xcand", "shadow", 0.80),
        ],
    )
    assert plan.demote_worst == "0xworst"


def test_plan_empty_when_all_actives_pinned() -> None:
    """EC-7 extreme : tous les actives sont pinned → pas d'eviction possible."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan(
        [
            _snap("0xp1", "active", 0.70, pinned=True),
            _snap("0xcand", "shadow", 0.95),
        ],
    )
    # Même si delta 0.25 énorme, aucun active non-pinned → no-op.
    assert plan.promote_candidate is None


def test_plan_cap_deferred_when_sell_only_saturated() -> None:
    """EC-6 : MAX_SELL_ONLY_WALLETS atteint → deferred_sell_only_cap=True."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=2)
    plan = planner.plan(
        [
            _snap("0xworst", "active", 0.55),
            _snap("0xcand", "shadow", 0.95),
            _snap("0xs1", "sell_only", 0.40),
            _snap("0xs2", "sell_only", 0.40),  # cap atteint
        ],
    )
    assert plan.deferred_sell_only_cap is True
    assert plan.promote_candidate is None
    assert plan.demote_worst is None


def test_plan_sell_only_candidate_for_rebound() -> None:
    """Un sell_only avec score haut est candidat T7 (rebond)."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan(
        [
            _snap("0xworst", "active", 0.55),
            _snap("0xsell_high", "sell_only", 0.90),  # rebond candidat
        ],
    )
    assert plan.promote_candidate is not None
    assert plan.promote_candidate.wallet_address == "0xsell_high"
    assert plan.promote_candidate.from_status == "sell_only"
    assert plan.demote_worst == "0xworst"


def test_plan_excludes_wallets_without_score() -> None:
    """Un wallet sans score (None) n'est jamais candidat ni worst."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan(
        [
            _snap("0xworst", "active", 0.55),
            _snap("0xnoscore_active", "active", None),  # ignoré
            _snap("0xnoscore_shadow", "shadow", None),  # ignoré
            _snap("0xcand", "shadow", 0.95),
        ],
    )
    assert plan.promote_candidate is not None
    assert plan.promote_candidate.wallet_address == "0xcand"
    assert plan.demote_worst == "0xworst"  # pas 0xnoscore_active


def test_plan_tie_break_stable_by_wallet_address() -> None:
    """Égalité de delta : tri lexicographique stable sur wallet_address."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan(
        [
            _snap("0xworst", "active", 0.55),
            _snap("0xcand_b", "shadow", 0.80),  # delta 0.25
            _snap("0xcand_a", "shadow", 0.80),  # delta 0.25, gagnant (lexico)
        ],
    )
    assert plan.promote_candidate is not None
    assert plan.promote_candidate.wallet_address == "0xcand_a"


def test_plan_margin_boundary_strict_gte() -> None:
    """Delta == margin est déclencheur (≥, pas >)."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan(
        [
            _snap("0xworst", "active", 0.50),
            _snap("0xcand", "shadow", 0.65),  # delta exactement 0.15
        ],
    )
    assert plan.promote_candidate is not None
    assert plan.promote_candidate.wallet_address == "0xcand"


def test_plan_empty_pool_noop() -> None:
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan([])
    assert plan.promote_candidate is None
    assert plan.demote_worst is None


def test_plan_no_shadow_or_sell_only_candidates() -> None:
    """Pool ne contient que des actives/pinned → no-op."""
    planner = CascadePlanner(score_margin=0.15, max_sell_only_wallets=10)
    plan = planner.plan(
        [
            _snap("0xa", "active", 0.80),
            _snap("0xb", "pinned", 0.60, pinned=True),
        ],
    )
    assert plan.promote_candidate is None
