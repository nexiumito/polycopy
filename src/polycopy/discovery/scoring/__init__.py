"""Sous-package scoring discovery (M5 v1 + M14 v2.1-ROBUST).

La formule v1 vit dans :mod:`polycopy.discovery.scoring.v1` (intacte, identique
à M5). La formule v2.1-ROBUST vit dans :mod:`polycopy.discovery.scoring.v2`
(ajoutée M12, refactorée M14 — cf. spec M14 §5 + brief MA.md).

**M14 vs M12** : la formule M12 (``"v2"``) est jugée non-viable par l'audit
2026-04-24 (5 défauts structurels — H-008 timing_alpha, H-009 Sortino sentinel,
H-014 zombie filter, M-001 Brier P(YES), C-007 fixed-point trap winsorisation).
M14 livre v2.1-ROBUST qui corrige les 5 + flip HHI signal (Mitts-Ofir).
Le code v2 est modifié in-place et l'output `scoring_version` bumpé à
``"v2.1"``. La DB est reset post-M14 → pas de cohabitation v2/v2.1.

Ce ``__init__`` expose l'API publique consommée par
:class:`polycopy.discovery.orchestrator.DiscoveryOrchestrator` :

- :data:`SCORING_VERSIONS_REGISTRY` — registry ``{scoring_version: Callable}``
  contient ``"v1"`` (M5) et ``"v2.1"`` (M14).
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
    _compute_score_v2_1_1_wrapper,
    _compute_score_v2_wrapper,
)

# Enregistrement v2.1 + v2.1.1 dans le registry partagé avec v1. Effet de
# bord au chargement du module — intentionnel (l'orchestrator consomme le
# registry après import). Versioning sacré : ne JAMAIS réécrire une row
# historique d'une version donnée. Ici on n'enregistre **plus** "v2" car la
# DB est reset post-M14 (cf. décision utilisateur 2026-04-25). v2.1 reste
# accessible (audit trail M14).
SCORING_VERSIONS_REGISTRY["v2.1"] = _compute_score_v2_wrapper
# M15 MB.2 : v2.1.1 ajoute le facteur internal_pnl (poids 0.25) avec
# branche cold-start fallback v2.1 weights si <10 closed positions copiées.
SCORING_VERSIONS_REGISTRY["v2.1.1"] = _compute_score_v2_1_1_wrapper


__all__ = [
    "SCORING_VERSIONS_REGISTRY",
    "compute_score",
]
