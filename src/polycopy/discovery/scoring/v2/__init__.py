"""Sous-package scoring v2 (M12).

Formule hybride 6 facteurs + 6 gates durs + normalisation winsorisée pool-wide.
Cf. spec M12 §3 (factors) + §4 (gates).

API publique :

- :class:`TraderMetricsV2` — étend :class:`TraderMetrics` M5 avec les 12 nouvelles
  mesures nécessaires à la formule v2.
- :class:`ScoreV2Breakdown` — sortie détaillée du scoring (sous-scores bruts +
  normalisés + score final) pour audit et drill-down dashboard.
- :class:`PoolContext` — snapshot des valeurs pool-wide (winsorisation + Brier
  baseline). Rebuilt par :class:`DiscoveryOrchestrator` en début de chaque cycle.
- :data:`_CURRENT_POOL_CONTEXT` — contextvar posé par l'orchestrator avant
  d'appeler :func:`compute_score_v2` via le registry (wrapper signature v1).
- :func:`compute_score_v2` — agrégation pure des 6 facteurs + pondération fixe
  ``0.25/0.20/0.20/0.15/0.10/0.10``.
- :func:`check_all_gates` — validation pré-scoring fail-fast.
"""

from __future__ import annotations

from polycopy.discovery.scoring.v2.aggregator import (
    compute_score_v2,
    compute_score_v2_1_1,
)
from polycopy.discovery.scoring.v2.category_resolver import MarketCategoryResolver
from polycopy.discovery.scoring.v2.dtos import (
    AggregateGateResult,
    GateResult,
    PoolContext,
    RawSubscores,
    ScoreV2Breakdown,
    ScoringNormalizedSubscores,
    TraderMetricsV2,
)
from polycopy.discovery.scoring.v2.gates import check_all_gates
from polycopy.discovery.scoring.v2.pool_context import (
    _CURRENT_POOL_CONTEXT,
    bind_pool_context,
)

__all__ = [
    "_CURRENT_POOL_CONTEXT",
    "AggregateGateResult",
    "GateResult",
    "MarketCategoryResolver",
    "PoolContext",
    "RawSubscores",
    "ScoreV2Breakdown",
    "ScoringNormalizedSubscores",
    "TraderMetricsV2",
    "bind_pool_context",
    "check_all_gates",
    "compute_score_v2",
    "compute_score_v2_1_1",
]
