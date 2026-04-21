"""DTOs Pydantic v2 du package eviction M5_bis.

Tous frozen — les décisions d'eviction sont immuables par construction,
c'est un audit trail append-only (cf. spec §7.1, §8).

Pattern cohérent avec :class:`~polycopy.discovery.dtos.DiscoveryDecision`
M5 : chaque décision porte un ``transition`` (Literal), un couple
``from_status``/``to_status``, un ``score_at_event`` et un
``event_metadata`` riche — le caller (``DiscoveryOrchestrator``) mappe
vers :class:`~polycopy.storage.models.TraderEvent` et pousse l'alerte
Telegram correspondante.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

EvictionTransition = Literal[
    "promote_via_eviction",  # T3 : shadow → active (cascade avec T5)
    "demote_to_sell_only",  # T5 : active → sell_only (toujours causé par T3/T7)
    "abort_to_active",  # T6 : sell_only → active (delta repasse sous margin)
    "promote_via_rebound",  # T7 : sell_only → active (score rebondit)
    "complete_to_shadow",  # T8 : sell_only → shadow (positions_open == 0)
    "blacklist",  # T10 : any → blacklisted (user ajout env)
    "unblacklist",  # T11/T12 : blacklisted → shadow/pinned (user retrait)
    "defer_one_per_cycle",  # EC-2 : candidat en attente (1 swap/cycle strict)
    "defer_sell_only_cap",  # EC-6 : MAX_SELL_ONLY_WALLETS atteint
]

HysteresisDirection = Literal[
    "eviction",  # candidat shadow qui veut évincer un active (T3)
    "abort",  # sell_only dont le delta repasse sous margin (T6)
    "rebound",  # sell_only dont le score regrimpe (T7)
]


@dataclass(frozen=True)
class EvictionDecision:
    """Décision d'eviction produite par ``EvictionScheduler.run_cycle``.

    Écrite par le caller dans ``trader_events`` avec ``event_type`` dérivé
    de ``transition`` (mapping :data:`TRANSITION_TO_EVENT_TYPE`). Mapping
    également vers le template Telegram correspondant (cf. spec §11).
    """

    wallet_address: str
    transition: EvictionTransition
    from_status: str
    to_status: str
    score_at_event: float | None = None
    # Delta de score candidat vs worst_active (positif = candidat meilleur).
    # None pour les transitions sans comparaison (unblacklist, complete_to_shadow).
    delta_vs_worst_active: float | None = None
    # Wallet qui a causé la transition :
    #   - T5/T7 : candidat évinceur (le shadow/sell_only qui a grimpé).
    #   - T6    : candidat qui avait causé le sell_only (référence T5 antérieure).
    #   - T8/T10/T11 : None.
    triggering_wallet: str | None = None
    # Nombre de cycles consécutifs où la condition d'hystérésis a tenu.
    # None si la décision ne dépend pas d'hystérésis (blacklist, complete_to_shadow).
    cycles_observed: int | None = None
    # Reason code stable, machine-readable, en snake_case (ex: "delta_above_margin",
    # "positions_all_closed", "cap_reached", "user_env_added"). Pour audit grep-friendly.
    reason_code: str = ""


@dataclass(frozen=True)
class HysteresisState:
    """État in-memory d'une hystérésis en cours pour un wallet donné.

    Porté par le wallet **candidat** (celui qui veut bouger), pas par le
    couple (candidat, worst_active). Cf. spec §4.5 EC-3.

    Le ``target_wallet`` permet de détecter un changement de cible entre
    deux cycles — dans ce cas :class:`HysteresisTracker` reset le compteur
    (la condition observée n'est plus la même).
    """

    direction: HysteresisDirection
    # Pour direction="eviction" : le worst_active observé.
    # Pour direction="abort" : le triggering_wallet qui a causé le sell_only.
    # Pour direction="rebound" : le worst_active courant (même sémantique
    #   qu'"eviction", juste en partant d'un sell_only).
    target_wallet: str | None
    cycles_observed: int
    first_observed_at: datetime
    # Dernier delta observé — sert au debug + aux alertes Telegram (on
    # envoie le delta courant quand l'hystérésis se déclenche).
    last_delta: float = 0.0
    # Métadonnées libres transportées par :class:`EvictionScheduler` pour
    # reconstruire l'EvictionDecision au moment du déclenchement.
    metadata: dict[str, str] = field(default_factory=dict)


# Mapping Literal transition → event_type string persisté dans
# trader_events.event_type. Appliqué par DiscoveryOrchestrator Phase C.
TRANSITION_TO_EVENT_TYPE: dict[EvictionTransition, str] = {
    "promote_via_eviction": "promoted_active_via_eviction",
    "demote_to_sell_only": "demoted_to_sell_only",
    "abort_to_active": "eviction_aborted",
    "promote_via_rebound": "promoted_active_via_rebound",
    "complete_to_shadow": "eviction_completed_to_shadow",
    "blacklist": "blacklisted",
    "unblacklist": "blacklist_removed",
    "defer_one_per_cycle": "eviction_deferred_one_per_cycle",
    "defer_sell_only_cap": "eviction_deferred_sell_only_cap",
}
