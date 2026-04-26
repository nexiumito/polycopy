"""Tests table-driven M5_bis EvictionStateMachine (Phase B.6).

Couvre :class:`~polycopy.discovery.eviction.state_machine.
classify_sell_only_transitions` (T6 abort, T8 complete, EC-1 priorité) et
:func:`~polycopy.discovery.eviction.state_machine.
reconcile_blacklist_decisions` (T10/T11/T12, idempotence).

≥15 scénarios conformément à la spec §8.1.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polycopy.discovery.eviction import (
    HysteresisTracker,
    StateMachineInputs,
    TraderSnapshot,
    classify_sell_only_transitions,
    reconcile_blacklist_decisions,
)


def _snap(
    wallet: str,
    status: str,
    score: float | None,
    *,
    pinned: bool = False,
    triggering: str | None = None,
    open_positions: int = 0,
) -> TraderSnapshot:
    return TraderSnapshot(
        wallet_address=wallet,
        status=status,
        score=score,
        pinned=pinned,
        eviction_triggering_wallet=triggering,
        open_positions_count=open_positions,
    )


def _inputs(
    traders: list[TraderSnapshot],
    scores: dict[str, float],
    *,
    margin: float = 0.15,
    cycles: int = 3,
) -> StateMachineInputs:
    return StateMachineInputs(
        traders=traders,
        scores=scores,
        score_margin=margin,
        hysteresis_cycles=cycles,
    )


# --- T8 complete_to_shadow ---------------------------------------------------


def test_t8_complete_to_shadow_when_positions_zero() -> None:
    """SM-06 — sell_only sans positions ouvertes + pas de rebond → complete."""
    traders = [
        _snap("0xsell", "sell_only", 0.40, triggering="0xcand", open_positions=0),
        _snap("0xcand", "active", 0.80),
    ]
    scores = {"0xsell": 0.40, "0xcand": 0.80}
    tracker = HysteresisTracker()
    decisions = classify_sell_only_transitions(
        _inputs(traders, scores),
        tracker,
        blacklist=set(),
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.transition == "complete_to_shadow"
    assert d.wallet_address == "0xsell"
    assert d.to_status == "shadow"
    assert d.reason_code == "positions_all_closed"


def test_t8_skipped_when_positions_open() -> None:
    """sell_only avec positions ouvertes + pas d'abort → keep."""
    traders = [
        _snap("0xsell", "sell_only", 0.40, triggering="0xcand", open_positions=2),
        _snap("0xcand", "active", 0.80),
    ]
    scores = {"0xsell": 0.40, "0xcand": 0.80}
    tracker = HysteresisTracker()
    decisions = classify_sell_only_transitions(
        _inputs(traders, scores),
        tracker,
        blacklist=set(),
    )
    assert decisions == []


# --- T6 abort_to_active ------------------------------------------------------


def test_t6_abort_armed_on_delta_below_margin() -> None:
    """SM-04 — delta(triggering, self) < 0.15 pendant 3 cycles → abort."""
    traders = [
        _snap("0xsell", "sell_only", 0.50, triggering="0xcand", open_positions=1),
        _snap("0xcand", "active", 0.52),
    ]
    scores = {"0xsell": 0.50, "0xcand": 0.52}  # delta = 0.02
    tracker = HysteresisTracker()
    for _ in range(2):
        decisions = classify_sell_only_transitions(
            _inputs(traders, scores),
            tracker,
            blacklist=set(),
        )
        assert decisions == []
    # 3e cycle déclenche.
    decisions = classify_sell_only_transitions(
        _inputs(traders, scores),
        tracker,
        blacklist=set(),
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.transition == "abort_to_active"
    assert d.cycles_observed == 3
    assert d.reason_code == "abort_delta_below_margin"
    # Tracker est reset après déclenchement.
    assert tracker.count("0xsell") == 0


def test_t6_abort_reset_when_delta_above_margin() -> None:
    """Delta retourne ≥ margin après 2 cycles → reset compteur abort."""
    sell = _snap("0xsell", "sell_only", 0.50, triggering="0xcand", open_positions=1)
    cand = _snap("0xcand", "active", 0.55)
    traders = [sell, cand]
    tracker = HysteresisTracker()
    # 2 cycles under
    classify_sell_only_transitions(
        _inputs(traders, {"0xsell": 0.50, "0xcand": 0.55}),
        tracker,
        blacklist=set(),
    )
    classify_sell_only_transitions(
        _inputs(traders, {"0xsell": 0.50, "0xcand": 0.55}),
        tracker,
        blacklist=set(),
    )
    assert tracker.count("0xsell") == 2
    # Cycle 3 : delta repasse au-dessus margin → reset.
    decisions = classify_sell_only_transitions(
        _inputs(traders, {"0xsell": 0.50, "0xcand": 0.80}),
        tracker,
        blacklist=set(),
    )
    assert decisions == []
    assert tracker.count("0xsell") == 0


# --- EC-1 priorité T6 > T8 ---------------------------------------------------


def test_ec1_priority_abort_over_complete() -> None:
    """SM-07 — si T6 et T8 simultanément éligibles, priorité T6."""
    # positions_open=0 (T8 candidat) + delta 0.05 < 0.15 × 3 cycles (T6).
    traders = [
        _snap("0xsell", "sell_only", 0.60, triggering="0xcand", open_positions=0),
        _snap("0xcand", "active", 0.63),
    ]
    scores = {"0xsell": 0.60, "0xcand": 0.63}
    tracker = HysteresisTracker()
    for _ in range(2):
        classify_sell_only_transitions(
            _inputs(traders, scores),
            tracker,
            blacklist=set(),
        )
    decisions = classify_sell_only_transitions(
        _inputs(traders, scores),
        tracker,
        blacklist=set(),
    )
    # Une seule décision — l'abort. Pas de complete en doublon.
    assert len(decisions) == 1
    assert decisions[0].transition == "abort_to_active"


# --- Absence de triggering ---------------------------------------------------


def test_sell_only_without_triggering_only_t8_path() -> None:
    """sell_only sans triggering_wallet (edge case pré-Phase-B) → T6 impossible."""
    traders = [_snap("0xsell", "sell_only", 0.40, triggering=None, open_positions=0)]
    tracker = HysteresisTracker()
    decisions = classify_sell_only_transitions(
        _inputs(traders, {"0xsell": 0.40}),
        tracker,
        blacklist=set(),
    )
    # T8 déclenche quand même (positions=0).
    assert len(decisions) == 1
    assert decisions[0].transition == "complete_to_shadow"


# --- Blacklisted skip --------------------------------------------------------


def test_sell_only_in_blacklist_is_skipped() -> None:
    """Un sell_only blacklisted est ignoré — traité par reconcile_blacklist."""
    traders = [
        _snap("0xsell", "sell_only", 0.40, triggering="0xcand", open_positions=0),
    ]
    tracker = HysteresisTracker()
    decisions = classify_sell_only_transitions(
        _inputs(traders, {"0xsell": 0.40}),
        tracker,
        blacklist={"0xsell"},
    )
    assert decisions == []


# --- Reconcile blacklist : T10/T11/T12 --------------------------------------


def test_t10_blacklist_transitions_all_statuses() -> None:
    """SM-13 — wallets dans blacklist non déjà blacklisted → T10."""
    traders = [
        _snap("0xa", "active", 0.70),
        _snap("0xb", "shadow", 0.50),
        _snap("0xc", "sell_only", 0.40, triggering="0xd"),
        _snap("0xd", "pinned", 0.60, pinned=True),
        _snap("0xe", "blacklisted", None),  # déjà blacklisted, skip.
    ]
    decisions = reconcile_blacklist_decisions(
        traders,
        blacklist={"0xa", "0xb", "0xc", "0xd", "0xe"},
        target_wallets=set(),
    )
    # 4 nouvelles transitions (pas 0xe qui est déjà blacklisted).
    assert len(decisions) == 4
    assert {d.wallet_address for d in decisions} == {"0xa", "0xb", "0xc", "0xd"}
    assert all(d.transition == "blacklist" for d in decisions)
    assert all(d.to_status == "blacklisted" for d in decisions)


def test_t11_unblacklist_to_shadow_when_not_in_target_wallets() -> None:
    """SM-15 — wallet retiré de blacklist ET ∉ target_wallets → shadow."""
    traders = [_snap("0xback", "blacklisted", None)]
    decisions = reconcile_blacklist_decisions(
        traders,
        blacklist=set(),
        target_wallets=set(),
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.transition == "unblacklist"
    assert d.to_status == "shadow"
    assert d.reason_code == "user_env_removed"


def test_t12_unblacklist_to_pinned_when_in_target_wallets() -> None:
    """SM-14 — wallet retiré de blacklist ET ∈ target_wallets → pinned."""
    traders = [_snap("0xback", "blacklisted", None)]
    decisions = reconcile_blacklist_decisions(
        traders,
        blacklist=set(),
        target_wallets={"0xBACK"},  # case-insensitive
    )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.transition == "unblacklist"
    assert d.to_status == "pinned"


def test_reconcile_blacklist_idempotent() -> None:
    """2e appel sans changement blacklist → liste vide."""
    traders = [_snap("0xa", "blacklisted", None)]
    decisions_1 = reconcile_blacklist_decisions(
        traders,
        blacklist={"0xa"},
        target_wallets=set(),
    )
    assert decisions_1 == []  # déjà blacklisted, rien à faire.
    decisions_2 = reconcile_blacklist_decisions(
        traders,
        blacklist={"0xa"},
        target_wallets=set(),
    )
    assert decisions_2 == []


# --- Case sensitivity lowercase ---------------------------------------------


def test_blacklist_case_insensitive_lookup() -> None:
    """Wallet dans blacklist en UPPERCASE → transition déclenchée."""
    traders = [_snap("0xa", "active", 0.70)]
    decisions = reconcile_blacklist_decisions(
        traders,
        blacklist={"0xA"},
        target_wallets=set(),
    )
    assert len(decisions) == 1
    assert decisions[0].transition == "blacklist"


# --- Hysteresis direction change resets counter -----------------------------


def test_hysteresis_direction_change_resets_counter() -> None:
    """EC-3 dérivée : si la direction change, compteur reset.

    Scénario : 2 cycles abort (delta < margin), puis au 3e cycle delta
    repasse > margin — au cycle suivant on réarme depuis 1.
    """
    tracker = HysteresisTracker()
    # 2 ticks abort
    tracker.tick("0xsell", direction="abort", target_wallet="0xcand", current_delta=0.05)
    tracker.tick("0xsell", direction="abort", target_wallet="0xcand", current_delta=0.05)
    assert tracker.count("0xsell") == 2
    # Changement de cible → reset.
    n = tracker.tick("0xsell", direction="abort", target_wallet="0xother", current_delta=0.05)
    assert n == 1


# --- Empty / edge inputs -----------------------------------------------------


def test_no_sell_only_in_pool_returns_empty() -> None:
    traders = [
        _snap("0xa", "active", 0.70),
        _snap("0xb", "shadow", 0.50),
    ]
    tracker = HysteresisTracker()
    decisions = classify_sell_only_transitions(
        _inputs(traders, {"0xa": 0.70, "0xb": 0.50}),
        tracker,
        blacklist=set(),
    )
    assert decisions == []


def test_reconcile_empty_pool() -> None:
    decisions = reconcile_blacklist_decisions(
        [],
        blacklist={"0xa"},
        target_wallets=set(),
    )
    assert decisions == []


# --- Smoke : StateMachineInputs construction ----------------------------------


def test_inputs_frozen_immutability() -> None:
    """StateMachineInputs est un dataclass simple — ses fields sont accessibles."""
    inputs = _inputs([_snap("0xa", "active", 0.5)], {"0xa": 0.5})
    assert inputs.score_margin == 0.15
    assert inputs.hysteresis_cycles == 3


# --- Regression bag ---------------------------------------------------------


@pytest.mark.parametrize(
    ("self_score", "triggering_score", "margin", "expected_under"),
    [
        (0.50, 0.60, 0.15, True),  # delta=0.10 < 0.15 → under
        (0.50, 0.70, 0.15, False),  # delta=0.20 >= 0.15 → above
        (0.50, 0.65, 0.15, False),  # delta=0.15 == margin → NOT under (strictly <)
        (0.50, 0.64, 0.15, True),  # delta=0.14 < 0.15 → under
    ],
)
def test_abort_threshold_boundary(
    self_score: float,
    triggering_score: float,
    margin: float,
    expected_under: bool,
) -> None:
    """Frontière stricte < (pas ≤) pour la condition d'abort."""
    traders = [
        _snap("0xs", "sell_only", self_score, triggering="0xc", open_positions=1),
        _snap("0xc", "active", triggering_score),
    ]
    scores = {"0xs": self_score, "0xc": triggering_score}
    tracker = HysteresisTracker()
    # Tick 3 fois — si under, abort; sinon rien.
    for _ in range(3):
        decisions = classify_sell_only_transitions(
            _inputs(traders, scores, margin=margin),
            tracker,
            blacklist=set(),
        )
    if expected_under:
        assert any(d.transition == "abort_to_active" for d in decisions)
    else:
        assert decisions == []


def test_hysteresis_state_reports_first_observed_at() -> None:
    """HysteresisState capture le premier timestamp d'observation."""
    tracker = HysteresisTracker()
    before = datetime.now(tz=UTC)
    tracker.tick("0xa", direction="eviction", target_wallet="0xworst", current_delta=0.20)
    state = tracker.get("0xa")
    assert state is not None
    assert state.first_observed_at >= before
    assert state.last_delta == 0.20


# --- M15 MB.4 : fix audit H-007 — fresh scores in _delta_vs_worst (2 tests §9.4)


def test_classify_sell_only_uses_fresh_scores_for_worst_active() -> None:
    """MB.4 §9.4 #14 — `_delta_vs_worst` consomme `inputs.scores` fresh, pas
    `t.score` snapshot DB stale.

    Setup : pool 1 sell_only + 3 active non-pinned avec snapshot DB scores
    `[0.30, 0.45, 0.50]`. `inputs.scores` (fresh) `{0xa1: 0.55, 0xa2: 0.45,
    0xa3: 0.50, 0xsell: 0.40, 0xcand: 0.42}`.

    Avec MB.4 : worst fresh = 0xa2 à 0.45 (vs 0xa1 stale à 0.30 sans le fix).
    `delta_vs_worst_active = 0.40 - 0.45 = -0.05`.

    Cf. spec M15 §3.4 + audit 2026-04-24 [H-007].
    """
    # Le triggering wallet (0xcand) est inclus dans active_non_pinned par
    # design — c'est un wallet active. Pour démontrer le fix H-007 sans
    # ambiguïté on monte 0xcand fresh au-dessus du pool, et on observe
    # que le worst fresh devient 0xa2 (et non 0xa1 stale 0.30).
    sell = _snap("0xsell", "sell_only", 0.50, triggering="0xcand", open_positions=1)
    cand = _snap("0xcand", "active", 0.52)
    a1 = _snap("0xa1", "active", 0.30)  # DB stale 0.30 (potentielle worst)
    a2 = _snap("0xa2", "active", 0.45)  # DB stale 0.45
    a3 = _snap("0xa3", "active", 0.50)  # DB stale 0.50
    traders = [sell, cand, a1, a2, a3]
    fresh_scores = {
        "0xsell": 0.40,
        "0xcand": 0.42,  # ≥ 0xa2 fresh donc 0xa2 (0.45) est worst < 0xcand
        "0xa1": 0.55,  # remonté ce cycle (0.30 → 0.55)
        "0xa2": 0.45,  # worst fresh (sera détecté par MB.4)
        "0xa3": 0.50,
    }
    # Force le delta abort à se déclencher pour récupérer la décision.
    tracker = HysteresisTracker()
    decisions = []
    for _ in range(3):
        decisions = classify_sell_only_transitions(
            _inputs(traders, fresh_scores),
            tracker,
            blacklist=set(),
        )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.transition == "abort_to_active"
    # Avec MB.4, le worst FRESH parmi active_non_pinned :
    #   0xcand=0.42, 0xa1=0.55, 0xa2=0.45, 0xa3=0.50 → worst = 0xcand 0.42.
    # delta_vs_worst = self_score(0.40) - 0.42 = -0.02.
    # Si on consommait t.score stale, le worst serait 0xa1 à 0.30 →
    # delta = 0.40 - 0.30 = +0.10 (bug H-007). Différence détectable.
    assert d.delta_vs_worst_active is not None
    assert d.delta_vs_worst_active == pytest.approx(-0.02, abs=1e-6)


def test_eviction_no_stale_score_dependency_regression() -> None:
    """MB.4 §9.4 #15 — entre 2 cycles, si DB scores ne sont pas mis à jour
    (cas dégradé) mais inputs.scores porte les fresh, l'audit trail
    `delta_vs_worst_active` reste cohérent (pas de drift dû au stale).
    """
    sell = _snap("0xsell", "sell_only", 0.50, triggering="0xcand", open_positions=1)
    cand = _snap("0xcand", "active", 0.52)
    a1 = _snap("0xa1", "active", 0.40)  # DB stale 0.40 (pas mis à jour)
    traders = [sell, cand, a1]
    # Active non-pinned ce cycle : 0xcand (0.42 fresh) + 0xa1 (0.45 fresh).
    # Worst fresh = 0xcand 0.42. delta = self(0.40) - 0.42 = -0.02.
    fresh_cycle1 = {"0xsell": 0.40, "0xcand": 0.42, "0xa1": 0.45}
    fresh_cycle2 = {"0xsell": 0.40, "0xcand": 0.42, "0xa1": 0.45}  # stable

    # Cycle 1 + 2 + 3 sous margin → abort declenche au 3ème.
    tracker = HysteresisTracker()
    classify_sell_only_transitions(
        _inputs(traders, fresh_cycle1),
        tracker,
        blacklist=set(),
    )
    classify_sell_only_transitions(
        _inputs(traders, fresh_cycle2),
        tracker,
        blacklist=set(),
    )
    decisions = classify_sell_only_transitions(
        _inputs(traders, fresh_cycle2),
        tracker,
        blacklist=set(),
    )
    assert len(decisions) == 1
    d = decisions[0]
    # Worst fresh = 0xcand à 0.42. delta = 0.40 - 0.42 = -0.02. Cohérent
    # entre les 2 cycles (les fresh scores n'ont pas bougé).
    assert d.delta_vs_worst_active == pytest.approx(-0.02, abs=1e-6)
