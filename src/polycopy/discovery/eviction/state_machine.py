"""Pure state machine : classifies transitions per wallet per cycle.

Responsabilité : pour un wallet donné + son contexte (pool snapshot +
hystérésis + settings), dire "voici la transition applicable ce cycle"
— sans DB, sans async, sans alert push. Testable en table-driven isolé.

Le :class:`EvictionScheduler` (commit suivant) fait ensuite la
matérialisation : applique le plan cascade, persiste DB, push alert
Telegram.

Sémantique exacte des transitions — cf. spec §4.3 table T1..T12 :

- T5 (active → sell_only) n'est jamais retournée seule par cette machine
  : elle est **toujours** cascadée par un T3 ou T7 via
  :class:`~polycopy.discovery.eviction.cascade_planner.CascadePlanner`.
- T3 / T7 sont aussi produites via le planner (pas cette state machine).
- **Cette state machine produit uniquement** T6 (abort) et T8 (complete)
  — les transitions pilotées par l'état interne d'un sell_only, sans
  cascade. EC-1 (priorité T6 > T8 si conditions simultanées) est encodée
  dans le if/elif.

Les transitions T10 / T11 / T12 (blacklist reconciliation) vivent dans
une méthode séparée :func:`reconcile_blacklist_decisions` appelée
indépendamment par le scheduler (cycle + boot).
"""

from __future__ import annotations

from dataclasses import dataclass

from polycopy.discovery.eviction.cascade_planner import TraderSnapshot
from polycopy.discovery.eviction.dtos import EvictionDecision
from polycopy.discovery.eviction.hysteresis_tracker import HysteresisTracker


@dataclass(frozen=True)
class StateMachineInputs:
    """Entrées pures de la state machine. Pas de ref DB ni async."""

    traders: list[TraderSnapshot]
    # Scores frais du cycle par wallet. Peut contenir des wallets absents
    # de ``traders`` (ex: candidats pool scorés mais pas encore en DB — M5
    # les écrit en shadow plus tard). La state machine ignore ces cas.
    scores: dict[str, float]
    # Settings projetés (évite le couplage à ``Settings`` Pydantic).
    score_margin: float
    hysteresis_cycles: int


def classify_sell_only_transitions(
    inputs: StateMachineInputs,
    tracker: HysteresisTracker,
    *,
    blacklist: set[str],
) -> list[EvictionDecision]:
    """Parcourt les wallets en ``sell_only`` et classifie T6 (abort) | T8 (complete).

    Règle EC-1 — priorité T6 > T8 : si un wallet est éligible aux deux
    simultanément (delta d'abort atteint ET positions_open == 0 au même
    cycle), on déclenche l'abort. La spec §4.5 tranche ainsi pour
    préserver le slot quand le wallet a regagné la confiance relative.

    Un wallet blacklisté (via ``blacklist``) est ignoré — sa transition
    passe par :func:`reconcile_blacklist_decisions`.

    Le retour contient uniquement les décisions **actionnables** — les
    no-ops (ni abort, ni complete) n'apparaissent pas.
    """
    decisions: list[EvictionDecision] = []
    traders_by_wallet = {t.wallet_address.lower(): t for t in inputs.traders}
    # Calcul du worst_active courant (utile pour le rebond T7, mais le
    # rebond se résout via le CascadePlanner — ici on ne classifie que
    # T6 + T8). On a quand même besoin du worst pour savoir si la
    # condition d'abort (delta < margin) tient.
    active_non_pinned = [
        t
        for t in inputs.traders
        if t.status == "active" and not t.pinned and t.score is not None
    ]

    for sell_only in [t for t in inputs.traders if t.status == "sell_only"]:
        wallet = sell_only.wallet_address.lower()
        if wallet in blacklist:
            continue

        # T6 abort : la condition d'abort est que le delta(triggering, self)
        # repasse sous margin pendant N cycles. On interprète "triggering"
        # comme le wallet qui a causé le sell_only (stocké dans
        # eviction_triggering_wallet sur le modèle — porté par le snapshot).
        triggering = (
            sell_only.eviction_triggering_wallet.lower()
            if sell_only.eviction_triggering_wallet
            else None
        )
        self_score = inputs.scores.get(wallet, sell_only.score or 0.0)
        triggering_score: float | None = None
        if triggering is not None:
            t_snap = traders_by_wallet.get(triggering)
            triggering_score = (
                inputs.scores.get(triggering, t_snap.score or 0.0)
                if t_snap is not None
                else inputs.scores.get(triggering)
            )

        abort_triggered = False
        if triggering is not None and triggering_score is not None:
            delta = triggering_score - self_score
            if delta < inputs.score_margin:
                cycles = tracker.tick(
                    wallet,
                    direction="abort",
                    target_wallet=triggering,
                    current_delta=delta,
                )
                if cycles >= inputs.hysteresis_cycles:
                    decisions.append(
                        EvictionDecision(
                            wallet_address=wallet,
                            transition="abort_to_active",
                            from_status="sell_only",
                            to_status="active",
                            score_at_event=self_score,
                            delta_vs_worst_active=_delta_vs_worst(
                                self_score, active_non_pinned,
                            ),
                            triggering_wallet=triggering,
                            cycles_observed=cycles,
                            reason_code="abort_delta_below_margin",
                        ),
                    )
                    tracker.reset(wallet)
                    abort_triggered = True
            else:
                # Delta ≥ margin : reset compteur abort (la condition
                # n'est plus remplie ce cycle).
                existing = tracker.get(wallet)
                if existing is not None and existing.direction == "abort":
                    tracker.reset(wallet)

        if abort_triggered:
            continue

        # T8 complete_to_shadow : positions_open == 0 et pas d'abort
        # déclenché. On ne tick pas d'hystérésis ici — la condition est
        # atomique (soit 0 positions, soit non).
        if sell_only.open_positions_count == 0:
            decisions.append(
                EvictionDecision(
                    wallet_address=wallet,
                    transition="complete_to_shadow",
                    from_status="sell_only",
                    to_status="shadow",
                    score_at_event=self_score,
                    reason_code="positions_all_closed",
                ),
            )
            tracker.reset(wallet)

    return decisions


def _delta_vs_worst(
    self_score: float,
    active_non_pinned: list[TraderSnapshot],
) -> float | None:
    if not active_non_pinned:
        return None
    worst = min(active_non_pinned, key=lambda t: (t.score or 0.0, t.wallet_address))
    worst_score = worst.score
    if worst_score is None:
        return None
    return self_score - worst_score


def reconcile_blacklist_decisions(
    traders: list[TraderSnapshot],
    *,
    blacklist: set[str],
    target_wallets: set[str],
) -> list[EvictionDecision]:
    """Calcule les transitions T10/T11/T12 à appliquer pour aligner avec ``blacklist``.

    - Wallet dans blacklist avec status != blacklisted → transition T10.
    - Wallet avec status == blacklisted non plus dans blacklist :
      - Si wallet ∈ target_wallets → T12 (retour pinned).
      - Sinon → T11 (retour shadow).

    Pas d'appel DB, pas d'effet de bord — décisions pures à appliquer par
    le scheduler. Idempotent : second appel sans changement ``blacklist``
    retourne liste vide.
    """
    decisions: list[EvictionDecision] = []
    bl = {w.lower() for w in blacklist}
    tw = {w.lower() for w in target_wallets}
    for t in traders:
        wallet = t.wallet_address.lower()
        in_blacklist = wallet in bl
        if in_blacklist and t.status != "blacklisted":
            decisions.append(
                EvictionDecision(
                    wallet_address=wallet,
                    transition="blacklist",
                    from_status=t.status,
                    to_status="blacklisted",
                    score_at_event=t.score,
                    reason_code="user_env_added",
                ),
            )
        elif not in_blacklist and t.status == "blacklisted":
            restore_to = "pinned" if wallet in tw else "shadow"
            decisions.append(
                EvictionDecision(
                    wallet_address=wallet,
                    transition="unblacklist",
                    from_status="blacklisted",
                    to_status=restore_to,
                    score_at_event=t.score,
                    reason_code="user_env_removed",
                ),
            )
    return decisions
