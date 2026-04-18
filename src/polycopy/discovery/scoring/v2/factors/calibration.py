"""Facteur ``calibration`` (M12 §3.3).

Brier-skill score : ``1 - brier_wallet / brier_baseline_pool``.

- Brier d'un wallet = ``mean((outcome - predicted_prob)^2)`` sur positions
  résolues. ``outcome ∈ {0, 1}`` (YES won / NO won), ``predicted_prob =
  avg_price``.
- Brier baseline pool = Brier d'un wallet "moyen" (achète au midpoint) calculé
  1×/cycle par :class:`DiscoveryOrchestrator._build_pool_context`, fallback à
  ``0.25`` (Brier random binaire équilibré).

Score ≈ 1 → bien calibré, ≈ 0 → autant bruité que le pool, < 0 → pire que pool
(rare, pool normalization clippera à 0).

Seuils académiques (Gneiting-Raftery) : Brier < 0.22 = skill-level, Brier <
0.15 = expert-level.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


# Fallback Brier baseline (random binaire équilibré).
_FALLBACK_BRIER_BASELINE: float = 0.25


def compute_calibration(
    metrics: TraderMetricsV2,
    brier_baseline_pool: float,
) -> float:
    """Retourne la valeur brute Brier-skill ∈ ``(-inf, 1]``.

    Pool normalization compressera les valeurs négatives vers ``p5 → 0.0``.

    Cas particuliers :

    - ``brier_90d is None`` (aucune position résolue ou données insuffisantes)
      → retourne 0.0.
    - Baseline pool ≤ 0 (dégénéré) → fallback ``_FALLBACK_BRIER_BASELINE``.
    """
    if metrics.brier_90d is None:
        return 0.0
    baseline = brier_baseline_pool if brier_baseline_pool > 0 else _FALLBACK_BRIER_BASELINE
    return 1.0 - (metrics.brier_90d / baseline)
