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
    compute_internal_pnl,
    compute_risk_adjusted,
    compute_specialization,
    compute_timing_alpha,
)
from polycopy.discovery.scoring.v2.normalization import rank_normalize_one
from polycopy.discovery.scoring.v2.pool_context import _CURRENT_POOL_CONTEXT

log = structlog.get_logger(__name__)

# Pondérations v2.1 (M14 MA.1 — renormalisées proportionnellement après drop
# de ``timing_alpha`` à 0). Somme = 1.0, vérifiée par assert au load + test.
#
# Justification : le placeholder ``timing_alpha=0.5`` documenté en M12 décision
# D3 produisait p5==p95==0.5 → pool normalization renvoyait 0.5 sentinel pour
# tous → contribution 0.20×0.5 = +0.10 uniforme sur chaque score (audit H-008,
# synthèse F01 3/3 sources). Drop strictement préférable per Daniele et al.
# adaptive lasso : "uninformative factors with non-zero weight monotonically
# degrade out-of-sample estimation error".
#
# Renormalisation proportionnelle (décision D7 M14 §14.2) : chaque facteur
# garde son ratio relatif M12 (Sortino+Calmar > Brier > HHI > consistency =
# discipline). On divise par 0.80 (somme post-drop) pour ramener à 1.0.
#
#   risk_adjusted   : 0.25 / 0.80 = 0.3125
#   calibration     : 0.20 / 0.80 = 0.2500
#   specialization  : 0.15 / 0.80 = 0.1875
#   consistency     : 0.10 / 0.80 = 0.1250
#   discipline      : 0.10 / 0.80 = 0.1250
#   timing_alpha    : 0.0       (conservé pour re-enable v2.2 / via RTDS, MG)
_WEIGHT_RISK_ADJUSTED: float = 0.3125
_WEIGHT_CALIBRATION: float = 0.2500
_WEIGHT_TIMING_ALPHA: float = 0.0
_WEIGHT_SPECIALIZATION: float = 0.1875
_WEIGHT_CONSISTENCY: float = 0.1250
_WEIGHT_DISCIPLINE: float = 0.1250

# M15 MB.2 — pondérations v2.1.1.
# Drop 0.25 sur les 5 facteurs hérités au profit de `internal_pnl=0.25`.
# Pondérations post spec M15 §5.2 (somme = 1.0, vérifiée au load) :
#
#   risk_adjusted   : 0.3125 → 0.25      (-0.0625)
#   calibration     : 0.2500 → 0.20      (-0.05)
#   specialization  : 0.1875 → 0.15      (-0.0375)
#   consistency     : 0.1250 → 0.075     (-0.05)
#   discipline      : 0.1250 → 0.075     (-0.05)
#   timing_alpha    : 0.0   (inchangé)
#   internal_pnl    : 0.0   → 0.25       (NEW)
#   ────────────────────────────────────────
#   sum             : 1.0    1.0
#
# Note : ces poids ne sont pas une renormalisation proportionnelle stricte
# (qui donnerait 0.10/0.10 sur consistency/discipline) — ils suivent le
# tableau M15 §5.2 qui réduit consistency + discipline plus agressivement
# que risk_adjusted/calibration (cohérent avec Claude §4.2 v2.2 ranking
# attendu : signal financier > régularité comportementale).
_WEIGHT_RISK_ADJUSTED_V2_1_1: float = 0.25
_WEIGHT_CALIBRATION_V2_1_1: float = 0.20
_WEIGHT_TIMING_ALPHA_V2_1_1: float = 0.0
_WEIGHT_SPECIALIZATION_V2_1_1: float = 0.15
_WEIGHT_CONSISTENCY_V2_1_1: float = 0.075
_WEIGHT_DISCIPLINE_V2_1_1: float = 0.075
_WEIGHT_INTERNAL_PNL_V2_1_1: float = 0.25

# Garde-fou : la somme doit être exactement 1.0 ± epsilon. Vérifié au load
# (raise ImportError si jamais une futur modif casse l'invariant) + par
# `test_aggregator_weights_sum_to_one`.
_WEIGHTS_SUM_TOLERANCE: float = 1e-6
_WEIGHTS_SUM: float = (
    _WEIGHT_RISK_ADJUSTED
    + _WEIGHT_CALIBRATION
    + _WEIGHT_TIMING_ALPHA
    + _WEIGHT_SPECIALIZATION
    + _WEIGHT_CONSISTENCY
    + _WEIGHT_DISCIPLINE
)
_WEIGHTS_SUM_V2_1_1: float = (
    _WEIGHT_RISK_ADJUSTED_V2_1_1
    + _WEIGHT_CALIBRATION_V2_1_1
    + _WEIGHT_TIMING_ALPHA_V2_1_1
    + _WEIGHT_SPECIALIZATION_V2_1_1
    + _WEIGHT_CONSISTENCY_V2_1_1
    + _WEIGHT_DISCIPLINE_V2_1_1
    + _WEIGHT_INTERNAL_PNL_V2_1_1
)
if abs(_WEIGHTS_SUM - 1.0) > _WEIGHTS_SUM_TOLERANCE:  # pragma: no cover
    raise ImportError(
        f"Pondérations scoring v2.1 ne somment pas à 1.0 : {_WEIGHTS_SUM} "
        "(check src/polycopy/discovery/scoring/v2/aggregator.py)"
    )
if abs(_WEIGHTS_SUM_V2_1_1 - 1.0) > _WEIGHTS_SUM_TOLERANCE:  # pragma: no cover
    raise ImportError(
        f"Pondérations scoring v2.1.1 ne somment pas à 1.0 : "
        f"{_WEIGHTS_SUM_V2_1_1} "
        "(check src/polycopy/discovery/scoring/v2/aggregator.py)"
    )


def compute_score_v2(
    metrics: TraderMetricsV2,
    pool_context: PoolContext,
) -> ScoreV2Breakdown:
    """Calcule le :class:`ScoreV2Breakdown` v2.1 pour 1 wallet.

    Étapes :

    1. Calcul des 6 sous-scores bruts via les pure factors.
    2. Normalisation pool-wide via winsorisation p5-p95 (pool_context).
    3. Agrégation pondérée fixe.
    4. Clip final ``[0, 1]``.

    Pure function — déterministe, zéro I/O, zéro state externe (tout passé
    par argument).

    M15 MB.2 : conservée intacte pour préserver l'audit trail v2.1 (registry
    SCORING_VERSIONS_REGISTRY["v2.1"] continue à pointer ici via wrapper).
    Les nouvelles rows v2.1.1 passent par :func:`compute_score_v2_1_1`.
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
        risk_adjusted=rank_normalize_one(
            raw.risk_adjusted,
            pool_context.risk_adjusted_pool,
        ),
        calibration=rank_normalize_one(
            raw.calibration,
            pool_context.calibration_pool,
        ),
        timing_alpha=rank_normalize_one(
            raw.timing_alpha,
            pool_context.timing_alpha_pool,
        ),
        specialization=rank_normalize_one(
            raw.specialization,
            pool_context.specialization_pool,
        ),
        consistency=rank_normalize_one(
            raw.consistency,
            pool_context.consistency_pool,
        ),
        discipline=rank_normalize_one(
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
        scoring_version="v2.1",
    )


def compute_score_v2_1_1(
    metrics: TraderMetricsV2,
    pool_context: PoolContext,
) -> ScoreV2Breakdown:
    """Calcule le :class:`ScoreV2Breakdown` v2.1.1 (M15 MB.2).

    Diff vs ``compute_score_v2`` :

    1. **+1 facteur** ``internal_pnl`` (sigmoid sur PnL réalisée par polycopy
       depuis qu'il copie le wallet, MB.1).
    2. **Pondérations renormalisées** : 0.25 / 0.20 / 0.15 / 0.10 / 0.10 +
       0.25 internal_pnl (somme = 1.0).
    3. **Branche cold-start** : si ``metrics.internal_pnl_score is None``
       (count<10 closed positions copiées), on calcule sur **5 facteurs
       hérités** avec les poids v2.1 restaurés (somme=1.0). Aucun biais
       0.5 neutre — le facteur est strictement absent du calcul.
    4. ``scoring_version="v2.1.1"`` + ``cold_start_internal_pnl`` flag set
       sur le breakdown.

    Pure function — déterministe, zéro I/O, zéro state externe.

    Cf. spec M15 §5.2 + §9.2 + §14.1 D3.
    """
    raw_internal_pnl = compute_internal_pnl(metrics)
    cold_start = raw_internal_pnl is None

    raw = RawSubscores(
        risk_adjusted=compute_risk_adjusted(metrics),
        calibration=compute_calibration(metrics, pool_context.brier_baseline_pool),
        timing_alpha=compute_timing_alpha(metrics),
        specialization=compute_specialization(metrics),
        consistency=compute_consistency(metrics),
        discipline=compute_discipline(metrics),
        # 0.0 placeholder en cold-start (pas pondéré dans le scoring final).
        internal_pnl=raw_internal_pnl if not cold_start else 0.0,
    )
    normalized = ScoringNormalizedSubscores(
        risk_adjusted=rank_normalize_one(
            raw.risk_adjusted,
            pool_context.risk_adjusted_pool,
        ),
        calibration=rank_normalize_one(
            raw.calibration,
            pool_context.calibration_pool,
        ),
        timing_alpha=rank_normalize_one(
            raw.timing_alpha,
            pool_context.timing_alpha_pool,
        ),
        specialization=rank_normalize_one(
            raw.specialization,
            pool_context.specialization_pool,
        ),
        consistency=rank_normalize_one(
            raw.consistency,
            pool_context.consistency_pool,
        ),
        discipline=rank_normalize_one(
            raw.discipline,
            pool_context.discipline_pool,
        ),
        # MB.2 : rank uniquement si on a un score réel (cold-start → 0.0
        # placeholder, ignoré par la branche pondération).
        internal_pnl=(
            rank_normalize_one(raw_internal_pnl, pool_context.internal_pnl_pool)
            if not cold_start and raw_internal_pnl is not None
            else 0.0
        ),
    )

    if cold_start:
        # Branche cold-start (MB.2 §5.2 D3) — wallet n'a pas encore ≥10
        # closed positions copiées. Score calculé sur les 5 facteurs hérités
        # avec les poids v2.1 (somme=1.0). Plus honnête statistiquement
        # qu'un biais 0.5 neutre qui pousserait tous les nouveaux wallets
        # vers la médiane.
        final = (
            _WEIGHT_RISK_ADJUSTED * normalized.risk_adjusted
            + _WEIGHT_CALIBRATION * normalized.calibration
            + _WEIGHT_TIMING_ALPHA * normalized.timing_alpha
            + _WEIGHT_SPECIALIZATION * normalized.specialization
            + _WEIGHT_CONSISTENCY * normalized.consistency
            + _WEIGHT_DISCIPLINE * normalized.discipline
        )
    else:
        # Branche full v2.1.1 — wallet a ≥10 closed positions copiées.
        # Score calculé sur les 7 facteurs (6 hérités + internal_pnl) avec
        # les poids renormalisés (somme=1.0).
        final = (
            _WEIGHT_RISK_ADJUSTED_V2_1_1 * normalized.risk_adjusted
            + _WEIGHT_CALIBRATION_V2_1_1 * normalized.calibration
            + _WEIGHT_TIMING_ALPHA_V2_1_1 * normalized.timing_alpha
            + _WEIGHT_SPECIALIZATION_V2_1_1 * normalized.specialization
            + _WEIGHT_CONSISTENCY_V2_1_1 * normalized.consistency
            + _WEIGHT_DISCIPLINE_V2_1_1 * normalized.discipline
            + _WEIGHT_INTERNAL_PNL_V2_1_1 * normalized.internal_pnl
        )

    return ScoreV2Breakdown(
        wallet_address=metrics.wallet_address,
        score=max(0.0, min(1.0, final)),
        raw=raw,
        normalized=normalized,
        brier_baseline_pool=pool_context.brier_baseline_pool,
        scoring_version="v2.1.1",
        cold_start_internal_pnl=cold_start,
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


def _compute_score_v2_1_1_wrapper(metrics: TraderMetrics) -> float:
    """Wrapper SCORING_VERSIONS_REGISTRY — signature ``(TraderMetrics) -> float``.

    Strictement parallèle à :func:`_compute_score_v2_wrapper` mais pointe sur
    :func:`compute_score_v2_1_1` (formule M15 MB.2 avec facteur internal_pnl
    + branche cold-start). Le contextvar :data:`_CURRENT_POOL_CONTEXT` est
    partagé — l'orchestrator pose le pool ctx 1 fois/cycle, les deux
    wrappers le consomment.
    """
    pool_ctx = _CURRENT_POOL_CONTEXT.get()
    if pool_ctx is None:
        log.warning(
            "scoring_v2_1_1_no_pool_context",
            wallet=metrics.wallet_address,
        )
        return 0.0
    if not isinstance(metrics, TraderMetricsV2):
        log.warning(
            "scoring_v2_1_1_wrong_metrics_type",
            wallet=metrics.wallet_address,
            type_received=type(metrics).__name__,
        )
        return 0.0
    return compute_score_v2_1_1(metrics, pool_ctx).score
