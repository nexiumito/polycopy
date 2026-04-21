"""Pure planner : choisit qui éviction quoi ce cycle.

Stateless : ne dépend ni de la DB, ni de structlog, ni d'asyncio. Prend
un snapshot immutable du pool de traders + scores du cycle courant, et
retourne **un seul** ``CascadePlan`` (1 swap max par cycle, cf. spec §4.5
EC-2 + §2.1 "1 swap max par cycle").

Entrée / sortie typées — pas de stockage interne. Tous les choix
(selection, tie-break, exclusion pinned) sont déterministes ; ré-appel
avec les mêmes inputs = même sortie.

Exclusions strictes :
- Un ``pinned`` n'est **jamais** worst_active (spec §4.5 EC-7).
- Un ``blacklisted`` n'est **jamais** candidat, jamais worst.
- Un wallet qui n'a pas encore de score (``score is None``) n'est ni
  candidat ni worst (cold start M5, pas encore scoré ce cycle).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TraderSnapshot:
    """Snapshot minimal d'un wallet pour le cycle courant — input pur du planner.

    Pas de référence au modèle SQLA : le scheduler fait la conversion
    ``TargetTrader → TraderSnapshot`` une fois au début du cycle, puis
    passe uniquement ces snapshots au planner (testable sans DB).
    """

    wallet_address: str
    status: str  # shadow | active | sell_only | pinned | blacklisted | paused (legacy)
    score: float | None
    pinned: bool
    # Wallet qui a causé le sell_only courant, ou None si status != sell_only.
    eviction_triggering_wallet: str | None = None
    # Nombre de positions ouvertes (simulated OR réelles). Utilisé par T8
    # (complete_to_shadow si == 0). Rempli par le scheduler via un query
    # MyPosition repository — le planner lui-même n'accède pas à la DB.
    open_positions_count: int = 0


@dataclass(frozen=True)
class EvictionCandidate:
    """Candidat à l'eviction (shadow ou sell_only en rebond) avec son delta."""

    wallet_address: str
    from_status: str  # "shadow" | "sell_only"
    score: float
    # Delta = score_candidat - score_worst_active. >= margin requis pour déclencher.
    delta_vs_worst_active: float


@dataclass(frozen=True)
class CascadePlan:
    """Plan cascade pour le cycle courant.

    ``None`` pour ``promote_candidate`` + ``demote_worst`` = rien à faire
    (cap OK, pas de delta suffisant, ou autre safeguard). Les candidats
    éligibles non retenus (séquentiel 1/cycle) sont listés dans
    ``deferred_one_per_cycle`` — audité par le scheduler via un event
    ``eviction_deferred_one_per_cycle``.
    """

    promote_candidate: EvictionCandidate | None
    demote_worst: str | None  # wallet_address du worst_active à cascader en sell_only
    deferred_one_per_cycle: tuple[EvictionCandidate, ...] = ()
    # True si la cascade a été skip parce que MAX_SELL_ONLY_WALLETS atteint.
    deferred_sell_only_cap: bool = False


class CascadePlanner:
    """Pure planner — 1 swap max par cycle.

    Algorithme (cf. spec §4.3 T3 + §4.5 EC-2) :

    1. Filtrer le pool ``active`` en excluant les pinned → liste
       ``active_candidates_for_eviction``.
    2. Si vide → pas d'eviction possible (tous pinned — spec §4.5 EC-7).
    3. Calculer ``worst_active = min(by score)``.
    4. Filtrer le pool (shadow ∪ sell_only) par score défini + score >
       worst.score + margin. Trier par delta décroissant.
    5. Top candidat = sélection retenue ; les autres éligibles partent
       en ``deferred_one_per_cycle``.
    6. Si ``sell_only_count >= max_sell_only_wallets`` → retour
       ``deferred_sell_only_cap=True``, pas de cascade ce cycle.
    """

    def __init__(
        self,
        *,
        score_margin: float,
        max_sell_only_wallets: int,
    ) -> None:
        self._margin = score_margin
        self._max_sell_only = max_sell_only_wallets

    def plan(
        self,
        traders: list[TraderSnapshot],
    ) -> CascadePlan:
        active_non_pinned = [
            t
            for t in traders
            if t.status == "active" and not t.pinned and t.score is not None
        ]
        if not active_non_pinned:
            return CascadePlan(promote_candidate=None, demote_worst=None)

        # Tous les actives sont-ils pinned ? (EC-7)
        if all(t.pinned for t in traders if t.status == "active"):
            return CascadePlan(promote_candidate=None, demote_worst=None)

        worst = min(active_non_pinned, key=lambda t: (t.score or 0.0, t.wallet_address))
        worst_score = worst.score
        assert worst_score is not None

        # Candidats = shadow + sell_only avec score strictement > worst + margin.
        potential = [
            t
            for t in traders
            if t.status in ("shadow", "sell_only")
            and t.score is not None
            and (t.score - worst_score) >= self._margin
        ]
        if not potential:
            return CascadePlan(promote_candidate=None, demote_worst=None)

        # Cap sell_only ?
        sell_only_count = sum(1 for t in traders if t.status == "sell_only")
        if sell_only_count >= self._max_sell_only:
            # Convertir en candidats pour audit (defer_sell_only_cap).
            return CascadePlan(
                promote_candidate=None,
                demote_worst=None,
                deferred_sell_only_cap=True,
            )

        # Tri par delta décroissant, tie-break stable par wallet_address.
        sorted_candidates = sorted(
            potential,
            key=lambda t: (-(t.score or 0.0) + worst_score, t.wallet_address),
        )
        top = sorted_candidates[0]
        assert top.score is not None
        top_candidate = EvictionCandidate(
            wallet_address=top.wallet_address,
            from_status=top.status,
            score=top.score,
            delta_vs_worst_active=top.score - worst_score,
        )
        deferred = tuple(
            EvictionCandidate(
                wallet_address=t.wallet_address,
                from_status=t.status,
                score=t.score or 0.0,
                delta_vs_worst_active=(t.score or 0.0) - worst_score,
            )
            for t in sorted_candidates[1:]
        )
        return CascadePlan(
            promote_candidate=top_candidate,
            demote_worst=worst.wallet_address,
            deferred_one_per_cycle=deferred,
        )
