"""Aggregator : :func:`compute_score_v2` (M12 §3.1).

Pondération fixe ``0.25 / 0.20 / 0.20 / 0.15 / 0.10 / 0.10`` (somme = 1.0).
Changer une pondération = bumper ``SCORING_VERSION`` (append-only, versioning
sacré) — **jamais** rewrite les rows ``trader_scores`` historiques.

Pure function — accepte un :class:`TraderMetricsV2` + :class:`PoolContext`,
retourne un :class:`ScoreV2Breakdown` complet (6 raw + 6 normalisés + final)
pour audit / drill-down dashboard.

Le wrapper signature v1 (:func:`_compute_score_v2_wrapper`) permet d'enregistrer
v2 dans :data:`SCORING_VERSIONS_REGISTRY` sans modifier le contrat public
``(metrics) -> float``. Le :class:`PoolContext` est injecté via
:data:`_CURRENT_POOL_CONTEXT` (contextvar posé par l'orchestrator).
"""

from __future__ import annotations

import structlog

from polycopy.discovery.dtos import TraderMetrics
from polycopy.discovery.scoring.v2.dtos import (
    PoolContext,
    RawSubscores,
    ScoreV2Breakdown,
    ScoringNormalizedSubscores,
    TraderMetricsV2,
)
from polycopy.discovery.scoring.v2.factors import (
    compute_calibration,
    compute_consistency,
    compute_discipline,
    compute_risk_adjusted,
    compute_specialization,
    compute_timing_alpha,
)
from polycopy.discovery.scoring.v2.normalization import apply_pool_normalization
from polycopy.discovery.scoring.v2.pool_context import _CURRENT_POOL_CONTEXT

log = structlog.get_logger(__name__)

# Pondérations figées §3.1 — somme = 1.0. Vérifié par test.
_WEIGHT_RISK_ADJUSTED: float = 0.25
_WEIGHT_CALIBRATION: float = 0.20
_WEIGHT_TIMING_ALPHA: float = 0.20
_WEIGHT_SPECIALIZATION: float = 0.15
_WEIGHT_CONSISTENCY: float = 0.10
_WEIGHT_DISCIPLINE: float = 0.10


def compute_score_v2(
    metrics: TraderMetricsV2,
    pool_context: PoolContext,
) -> ScoreV2Breakdown:
    """Calcule le :class:`ScoreV2Breakdown` pour 1 wallet.

    Étapes :

    1. Calcul des 6 sous-scores bruts via les pure factors.
    2. Normalisation pool-wide via winsorisation p5-p95 (pool_context).
    3. Agrégation pondérée fixe.
    4. Clip final ``[0, 1]``.

    Pure function — déterministe, zéro I/O, zéro state externe (tout passé
    par argument).
    """
    raw = RawSubscores(
        risk_adjusted=compute_risk_adjusted(metrics),
        calibration=compute_calibration(metrics, pool_context.brier_baseline_pool),
        timing_alpha=compute_timing_alpha(metrics),
        specialization=compute_specialization(metrics),
        consistency=compute_consistency(metrics),
        discipline=compute_discipline(metrics),
    )
    normalized = ScoringNormalizedSubscores(
        risk_adjusted=apply_pool_normalization(
            raw.risk_adjusted,
            pool_context.risk_adjusted_pool,
        ),
        calibration=apply_pool_normalization(
            raw.calibration,
            pool_context.calibration_pool,
        ),
        timing_alpha=apply_pool_normalization(
            raw.timing_alpha,
            pool_context.timing_alpha_pool,
        ),
        specialization=apply_pool_normalization(
            raw.specialization,
            pool_context.specialization_pool,
        ),
        consistency=apply_pool_normalization(
            raw.consistency,
            pool_context.consistency_pool,
        ),
        discipline=apply_pool_normalization(
            raw.discipline,
            pool_context.discipline_pool,
        ),
    )
    final = (
        _WEIGHT_RISK_ADJUSTED * normalized.risk_adjusted
        + _WEIGHT_CALIBRATION * normalized.calibration
        + _WEIGHT_TIMING_ALPHA * normalized.timing_alpha
        + _WEIGHT_SPECIALIZATION * normalized.specialization
        + _WEIGHT_CONSISTENCY * normalized.consistency
        + _WEIGHT_DISCIPLINE * normalized.discipline
    )
    return ScoreV2Breakdown(
        wallet_address=metrics.wallet_address,
        score=max(0.0, min(1.0, final)),
        raw=raw,
        normalized=normalized,
        brier_baseline_pool=pool_context.brier_baseline_pool,
    )


def _compute_score_v2_wrapper(metrics: TraderMetrics) -> float:
    """Wrapper ``SCORING_VERSIONS_REGISTRY`` — signature ``(TraderMetrics) -> float``.

    La formule v2 exige un :class:`PoolContext` (pool-wide values + Brier
    baseline). Il est injecté via le contextvar
    :data:`_CURRENT_POOL_CONTEXT` que l'orchestrator pose via
    :func:`bind_pool_context` au début de chaque cycle.

    Cas particuliers :

    - :data:`_CURRENT_POOL_CONTEXT` non posé (appel hors orchestrator, ex:
      test unitaire v1 qui déclenche le registry par erreur) → retourne 0.0
      + log WARNING.
    - ``metrics`` n'est pas un :class:`TraderMetricsV2` (ex: legacy v1 passe
      par le registry) → retourne 0.0 + log WARNING. L'orchestrator doit
      passer un :class:`TraderMetricsV2` quand ``scoring_version="v2"``.
    """
    pool_ctx = _CURRENT_POOL_CONTEXT.get()
    if pool_ctx is None:
        log.warning("scoring_v2_no_pool_context", wallet=metrics.wallet_address)
        return 0.0
    if not isinstance(metrics, TraderMetricsV2):
        log.warning(
            "scoring_v2_wrong_metrics_type",
            wallet=metrics.wallet_address,
            type_received=type(metrics).__name__,
        )
        return 0.0
    return compute_score_v2(metrics, pool_ctx).score
