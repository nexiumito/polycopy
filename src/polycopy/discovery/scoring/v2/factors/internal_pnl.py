"""Factor ``internal_pnl`` (M15 MB.2) — feedback PnL réalisé par polycopy.

Lit ``metrics.internal_pnl_score`` calculé par
:meth:`polycopy.discovery.metrics_collector_v2.MetricsCollectorV2._compute_internal_pnl_score`.

Score sigmoid sur la PnL réalisée par polycopy depuis qu'il copie le wallet
(``signed_pnl_30d / SCORING_INTERNAL_PNL_SCALE_USD``). Le calcul lourd
(query SQL + sigmoid) vit côté collector pour éviter le coupling de ce
module au repository — ici, simple lecture + clip défensif.

Cold-start : si ``internal_pnl_score is None`` (count<10 closed positions
copiées), retourne ``None``. L'aggregator (MB.2) traite ce cas via la
branche cold-start (renormalisation locale aux poids v2.1 sur 5 facteurs
hérités, somme = 1.0).

Pure function. Pas de pool context — score déjà clipped ``[0, 1]`` par
le sigmoid en amont (clip défensif supplémentaire ici pour les cas où
le DTO serait set manuellement hors borne dans un test).

Cf. spec M15 §5.2 + §9.2.
"""

from __future__ import annotations

from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


def compute_internal_pnl(metrics: TraderMetricsV2) -> float | None:
    """Lit ``metrics.internal_pnl_score`` (déjà sigmoid).

    Returns:
      ``None`` si cold-start (collector a retourné ``None``, count<10 closed
      positions copiées sur 30j).
      ``float ∈ [0, 1]`` sinon (sigmoid déjà clippé, défensive clip).
    """
    score = metrics.internal_pnl_score
    if score is None:
        return None
    return max(0.0, min(1.0, float(score)))
