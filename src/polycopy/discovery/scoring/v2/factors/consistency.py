"""Facteur ``consistency`` (M12 §3.6).

``fraction de mois avec PnL > 0 sur 3 mois glissants``.

Filtre les one-shots. Poids faible (0.10) dans la formule finale car
partiellement corrélé à ``risk_adjusted``.

Calcul upstream dans :class:`MetricsCollectorV2._compute_monthly_ratio` depuis
``trader_daily_pnl``. Ce facteur est un simple pass-through + clip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


def compute_consistency(metrics: TraderMetricsV2) -> float:
    """Retourne ``monthly_pnl_positive_ratio`` clippé ``[0, 1]``."""
    return max(0.0, min(1.0, metrics.monthly_pnl_positive_ratio))
