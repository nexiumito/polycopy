"""Package M5_bis — compétition adaptative entre wallets (eviction).

Surface publique exposée :

- :class:`EvictionScheduler` — point d'entrée, appelé par
  :class:`~polycopy.discovery.orchestrator.DiscoveryOrchestrator` en aval
  du :class:`~polycopy.discovery.decision_engine.DecisionEngine` M5.
- :class:`EvictionDecision` — DTO gelé retourné par le scheduler ; mapping
  1:1 vers :class:`~polycopy.storage.models.TraderEvent`.
- :class:`EvictionTransition` — Literal des 7 transitions M5_bis (T3, T5,
  T6, T7, T8, blacklist, unblacklist).

Feature flag ``EVICTION_ENABLED=false`` par défaut : si off, aucun objet
de ce package n'est instancié par ``DiscoveryOrchestrator`` — zéro diff
runtime vs M5 strict (cf. spec §13 Phase B critère d'acceptation).

Cf. ``docs/specs/M5_bis_competitive_eviction_spec.md`` §4 (state machine)
et §7 (architecture).
"""

from polycopy.discovery.eviction.dtos import (
    EvictionDecision,
    EvictionTransition,
    HysteresisDirection,
    HysteresisState,
)

__all__ = [
    "EvictionDecision",
    "EvictionTransition",
    "HysteresisDirection",
    "HysteresisState",
]
