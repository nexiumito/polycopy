"""DTOs Pydantic v2 pour le scoring M12.

Tous frozen (immutabilité → facilite le debug + raisonnement concurrent sur les
tâches asyncio). Composition préférée à l'héritage quand ``frozen=True``
impose des contraintes incompatibles (ex: ajouter des champs par défaut sur un
parent frozen).

Design (spec M12 §3, §4) :

- :class:`TraderMetricsV2` — étend :class:`TraderMetrics` M5 par composition.
  Le champ ``base`` référence l'instance M5 existante (``win_rate``, ``ROI``,
  ``HHI markets``, ``volume`` — toujours consommés par la v1). Les 12 nouvelles
  mesures vivent à plat sur v2. Évite de dupliquer l'API consommateur.
- :class:`RawSubscores` / :class:`ScoringNormalizedSubscores` — 6 sous-scores
  (avant/après winsorisation + normalisation).
- :class:`ScoreV2Breakdown` — sortie complète du scoring pour audit /
  drill-down dashboard ``/traders/scoring``.
- :class:`PoolContext` — valeurs pool-wide utilisées par winsorisation p5-p95
  + baseline Brier. Rebuilt 1×/cycle par ``DiscoveryOrchestrator``.
- :class:`GateResult` / :class:`AggregateGateResult` — résultats des gates
  durs. ``passed=False`` court-circuite tout scoring (wallet rejeté).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from polycopy.discovery.dtos import TraderMetrics


class TraderMetricsV2(BaseModel):
    """Metrics agrégées pour le scoring v2 (M12 §3).

    Composition : ``base`` réfère les mesures M5 (v1 continue à les consommer
    inchangées). Les champs v2 sont additifs et documentés par facteur.
    """

    model_config = ConfigDict(frozen=True)

    base: TraderMetrics
    # --- risk_adjusted (§3.2) ----------------------------------------------
    sortino_90d: float = 0.0
    calmar_90d: float = 0.0
    # --- calibration (§3.3) ------------------------------------------------
    brier_90d: float | None = None  # None = pas assez de positions résolues
    # --- timing_alpha (§3.4) -----------------------------------------------
    timing_alpha_weighted: float = 0.5  # neutre par défaut (cf. §3.4 fallback)
    # --- specialization (§3.5) ---------------------------------------------
    hhi_categories: float = 1.0  # 1.0 = concentration max (1 catégorie)
    # --- consistency (§3.6) ------------------------------------------------
    monthly_pnl_positive_ratio: float = 0.0
    # --- discipline (§3.7) -------------------------------------------------
    zombie_ratio: float = 0.0
    sizing_cv: float = 1.0
    # --- gates durs (§4.1) -------------------------------------------------
    cash_pnl_90d: float = 0.0
    trade_count_90d: int = 0
    days_active: int = 0
    # --- equity curve raw (entrée Sortino/Calmar, consommée par le factor) -
    monthly_equity_curve: list[float] = Field(default_factory=list)

    @property
    def wallet_address(self) -> str:
        """Proxy convenience : wallet_address vit sur ``base``."""
        return self.base.wallet_address


class RawSubscores(BaseModel):
    """6 sous-scores bruts (avant winsorisation pool)."""

    model_config = ConfigDict(frozen=True)

    risk_adjusted: float
    calibration: float
    timing_alpha: float
    specialization: float
    consistency: float
    discipline: float


class ScoringNormalizedSubscores(BaseModel):
    """6 sous-scores normalisés ∈ [0, 1] post winsorisation p5-p95 pool."""

    model_config = ConfigDict(frozen=True)

    risk_adjusted: float = Field(ge=0.0, le=1.0)
    calibration: float = Field(ge=0.0, le=1.0)
    timing_alpha: float = Field(ge=0.0, le=1.0)
    specialization: float = Field(ge=0.0, le=1.0)
    consistency: float = Field(ge=0.0, le=1.0)
    discipline: float = Field(ge=0.0, le=1.0)


class ScoreV2Breakdown(BaseModel):
    """Sortie complète de :func:`compute_score_v2` pour audit + dashboard."""

    model_config = ConfigDict(frozen=True)

    wallet_address: str
    score: float = Field(ge=0.0, le=1.0)
    raw: RawSubscores
    normalized: ScoringNormalizedSubscores
    brier_baseline_pool: float
    scoring_version: Literal["v2"] = "v2"


class PoolContext(BaseModel):
    """Snapshot pool-wide pour normalisation + Brier baseline (§3.8).

    Rebuilt par :class:`DiscoveryOrchestrator._build_pool_context` au début de
    chaque cycle. Les 6 listes ``*_pool`` contiennent les valeurs brutes
    calculées pour chaque wallet du pool candidat ; winsorisation p5-p95
    appliquée dans :func:`apply_pool_normalization` à l'usage.
    """

    model_config = ConfigDict(frozen=True)

    risk_adjusted_pool: list[float] = Field(default_factory=list)
    calibration_pool: list[float] = Field(default_factory=list)
    timing_alpha_pool: list[float] = Field(default_factory=list)
    specialization_pool: list[float] = Field(default_factory=list)
    consistency_pool: list[float] = Field(default_factory=list)
    discipline_pool: list[float] = Field(default_factory=list)
    # Brier d'un wallet hypothétique qui achèterait toujours au midpoint pool
    # (ou fallback 0.25 = Brier random binaire).
    brier_baseline_pool: float = 0.25


_GateName = Literal[
    "cash_pnl_positive",
    "trade_count_min",
    "days_active_min",
    "zombie_ratio_max",
    "not_blacklisted",
    "not_wash_cluster",
]


class GateResult(BaseModel):
    """Résultat d'un gate dur (§4.1)."""

    model_config = ConfigDict(frozen=True)

    gate_name: _GateName
    passed: bool
    observed_value: float | int | str
    threshold: float | int | str
    reason: str


class AggregateGateResult(BaseModel):
    """Résultat agrégé : `passed` global + premier gate échoué (fail-fast)."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    failed_gate: GateResult | None = None
