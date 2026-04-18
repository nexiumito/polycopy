"""Facteur ``timing_alpha`` (M12 §3.4).

Fraction du PnL généré par des trades entrés **avant** un mouvement
significatif de prix (Mitts-Ofir 2026). Signal informationnel (smart money
follows news before the crowd).

**Implémentation v1** : l'aggregation pair-level → wallet-level est déléguée
à :class:`MetricsCollectorV2._compute_timing_alpha_wallet` (weighted mean par
``sqrt(n_trades_pair)``). Ce facteur ne fait que le wrapping / clipping final.

**Fallback** : si < 50 trades observés sur la fenêtre 10 min pour un pair →
``timing_alpha_pair = 0.5`` (neutre, décision D3 validée user 2026-04-18).
Pool normalization compressera les wallets avec beaucoup de pairs illiquides
vers le p50.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


def compute_timing_alpha(metrics: TraderMetricsV2) -> float:
    """Retourne ``metrics.timing_alpha_weighted`` clippé à ``[0, 1]``.

    L'aggregation pair → wallet est faite amont par le collector — ce facteur
    applique juste la borne.
    """
    return max(0.0, min(1.0, metrics.timing_alpha_weighted))
