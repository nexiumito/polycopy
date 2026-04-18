"""Moteur de scoring M5 — formule v1 déterministe, versionnée.

Design (spec §7) :

- Registry ``SCORING_VERSIONS_REGISTRY`` : version → fonction de scoring.
  Permet de coexister v1/v2 sans migration rétroactive.
- Cold start : `resolved_positions_count < SCORING_MIN_CLOSED_MARKETS` →
  retourne `(0.0, low_confidence=True)` pour empêcher toute promotion.
- Formule v1 : `0.30·win_rate + 0.30·roi_norm + 0.20·diversity + 0.20·volume_norm`.

Anti-gaming §2.2 :
- PnL farming sur marchés évidents : ROI minuscule → roi_norm stagne.
- Wash trading : win_rate=0.5, roi ≈ 0 → score plafonne ~0.5 (sous promotion).
- One-hit wonder : cold start gate + Herfindahl = 1 → diversité tombe.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING

from polycopy.discovery.dtos import TraderMetrics

if TYPE_CHECKING:
    from polycopy.config import Settings


def _compute_score_v1(metrics: TraderMetrics) -> float:
    """Formule v1 : pondération conservatrice 4-métriques.

    Retour ∈ [0, 1]. Clip systématique pour résilience aux valeurs extrêmes.
    """
    consistency = max(0.0, min(1.0, float(metrics.win_rate)))
    roi_clipped = max(-2.0, min(2.0, float(metrics.realized_roi)))
    roi_norm = (roi_clipped + 2.0) / 4.0
    diversity = max(0.0, min(1.0, 1.0 - float(metrics.herfindahl_index)))
    volume_norm = min(
        1.0,
        max(
            0.0,
            math.log10(max(1.0, float(metrics.total_volume_usd)) / 1000.0) / 3.0,
        ),
    )
    score = 0.30 * consistency + 0.30 * roi_norm + 0.20 * diversity + 0.20 * volume_norm
    return max(0.0, min(1.0, score))


SCORING_VERSIONS_REGISTRY: dict[str, Callable[[TraderMetrics], float]] = {
    "v1": _compute_score_v1,
}


def compute_score(
    metrics: TraderMetrics,
    *,
    settings: Settings,
) -> tuple[float, bool]:
    """Retourne ``(score, low_confidence)``.

    ``low_confidence=True`` si ``resolved_positions_count < scoring_min_closed_markets``
    (cold start). Dans ce cas, `score=0.0` par design — empêche promotion.

    Raise ``ValueError`` si ``settings.scoring_version`` n'est pas enregistré.
    """
    fn = SCORING_VERSIONS_REGISTRY.get(settings.scoring_version)
    if fn is None:
        raise ValueError(
            f"Unknown SCORING_VERSION: {settings.scoring_version!r}. "
            f"Registered: {list(SCORING_VERSIONS_REGISTRY.keys())}",
        )
    if metrics.resolved_positions_count < settings.scoring_min_closed_markets:
        return 0.0, True
    return fn(metrics), False
