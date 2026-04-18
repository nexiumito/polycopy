"""Sous-package scoring discovery (M5 v1 + M12 v2 coexistants).

La formule v1 vit dans :mod:`polycopy.discovery.scoring.v1` (intacte, identique
à M5). La formule v2 vit dans :mod:`polycopy.discovery.scoring.v2` (ajoutée
M12, cf. spec M12 §3).

Ce ``__init__`` expose l'API publique consommée par
:class:`polycopy.discovery.orchestrator.DiscoveryOrchestrator` :

- :data:`SCORING_VERSIONS_REGISTRY` — registry ``{scoring_version: Callable}``
  étendu M12 pour inclure ``"v2"`` via :func:`compute_score_v2_registry`.
- :func:`compute_score` — wrapper qui lookup le registry selon
  ``settings.scoring_version`` + gère le cold start v1 et retourne
  ``(score, low_confidence)``. Logique M5 inchangée pour v1.

API strictement backward-compatible avec M5. Les imports existants
``from polycopy.discovery.scoring import compute_score, SCORING_VERSIONS_REGISTRY``
continuent à fonctionner sans modification.
"""

from __future__ import annotations

from polycopy.discovery.scoring.v1 import (
    SCORING_VERSIONS_REGISTRY,
    compute_score,
)
from polycopy.discovery.scoring.v2.aggregator import (
    _compute_score_v2_wrapper,
)

# Enregistrement v2 dans le registry partagé avec v1. Effet de bord au
# chargement du module — intentionnel (l'orchestrator consomme le registry
# après import). Versioning sacré : ne JAMAIS supprimer une entrée historique.
SCORING_VERSIONS_REGISTRY["v2"] = _compute_score_v2_wrapper


__all__ = [
    "SCORING_VERSIONS_REGISTRY",
    "compute_score",
]
