"""DTOs Pydantic pour la couche storage."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


class DetectedTradeDTO(BaseModel):
    """Représentation d'un trade prête pour insertion en base.

    M11 : ``trade_id`` (uuid hex) ajouté pour l'instrumentation latence
    cross-queue (`structlog.contextvars.bind_contextvars`). Nullable pour
    backward-compat tests M1..M10 ; généré par le `WalletPoller` au moment
    de l'insertion quand l'instrumentation est active (cf. spec M11 §5.1).

    M15 MB.6 : ``is_source_probation`` propage le flag
    ``TargetTrader.is_probation`` au pipeline strategy. Quand ``True``,
    ``PositionSizer._check_buy`` multiplie ``my_size`` par
    ``probation_size_multiplier`` (default 0.25). Default ``False`` —
    propagation rétro-compatible avec les chemins M1..M14.
    """

    model_config = ConfigDict(frozen=True)

    tx_hash: str
    target_wallet: str
    condition_id: str
    asset_id: str
    side: Literal["BUY", "SELL"]
    size: float
    usdc_size: float
    price: float
    timestamp: datetime
    outcome: str | None = None
    slug: str | None = None
    raw_json: dict[str, Any]
    trade_id: str | None = None
    is_source_probation: bool = False


class StrategyDecisionDTO(BaseModel):
    """Décision du pipeline strategy, prête pour insertion en base."""

    model_config = ConfigDict(frozen=True)

    detected_trade_id: int
    tx_hash: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str | None = None
    my_size: float | None = None
    my_price: float | None = None
    slippage_pct: float | None = None
    pipeline_state: dict[str, Any]


class PnlSnapshotDTO(BaseModel):
    """Snapshot PnL prêt pour insertion en base. Écrit par le ``PnlSnapshotWriter`` (M4).

    M17 MD.3 : ``execution_mode`` ajouté (tri-state SIM/DRY/LIVE).
    Si non fourni explicitement, dérive de ``is_dry_run`` (rétrocompat 1
    version : ``True → "dry_run"``, ``False → "live"``). Drop ``is_dry_run``
    programmé M18+.
    """

    model_config = ConfigDict(frozen=True)

    total_usdc: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown_pct: float
    open_positions_count: int
    cash_pnl_total: float | None
    is_dry_run: bool
    execution_mode: Literal["simulation", "dry_run", "live"] | None = None

    @model_validator(mode="after")
    def _derive_execution_mode(self) -> "PnlSnapshotDTO":
        """M17 MD.3 — dérive ``execution_mode`` depuis ``is_dry_run`` si non set.

        Rétrocompat lecture pour les outils externes (tests M4..M16,
        scripts ad-hoc) qui passent uniquement ``is_dry_run``. Drop
        programmé M18+ — le DTO post-drop exigera ``execution_mode`` set.
        """
        if self.execution_mode is None:
            object.__setattr__(
                self,
                "execution_mode",
                "dry_run" if self.is_dry_run else "live",
            )
        return self


class MyOrderDTO(BaseModel):
    """Ordre prêt pour insertion en base (status initial `SIMULATED` ou `SENT`).

    Vit dans `storage/dtos.py` (non `executor/dtos.py` comme suggéré dans la spec
    M3 §4) pour cohérence avec `DetectedTradeDTO` et `StrategyDecisionDTO` :
    tous les DTOs d'input des repositories vivent dans la couche storage.
    """

    model_config = ConfigDict(frozen=True)

    source_tx_hash: str
    condition_id: str
    asset_id: str
    side: Literal["BUY", "SELL"]
    size: float
    price: float
    tick_size: float
    neg_risk: bool
    order_type: Literal["FOK", "FAK", "GTC"]
    status: Literal["SIMULATED", "SENT"]
    simulated: bool
    clob_order_id: str | None = None
    realistic_fill: bool = False


class RealisticSimulatedOrderDTO(BaseModel):
    """Ordre simulé M8 (dry-run + realistic_fill) prêt pour persistance.

    Diffère de ``MyOrderDTO`` par :
    - ``status`` peut valoir ``REJECTED`` (FOK strict, book insuffisant).
    - ``realistic_fill=True`` toujours.
    - ``simulated=True`` toujours.
    - ``error_msg`` optionnel (REJECTED → ``insufficient_liquidity``).
    """

    model_config = ConfigDict(frozen=True)

    source_tx_hash: str
    condition_id: str
    asset_id: str
    side: Literal["BUY", "SELL"]
    size: float
    price: float
    tick_size: float
    neg_risk: bool
    order_type: Literal["FOK", "FAK", "GTC"] = "FOK"
    status: Literal["SIMULATED", "REJECTED"]
    error_msg: str | None = None


# --- M5 discovery DTOs --------------------------------------------------------


TraderEventType = Literal[
    "discovered",
    "scored",
    "promoted_active",
    "demoted_paused",
    "kept",
    "skipped_blacklist",
    "skipped_cap",
    "manual_override",
    "revived_shadow",
    # M12 — scoring v2 gates (cf. spec M12 §4.3) : wallet rejeté avant scoring.
    "gate_rejected",
    # M5_bis — competitive eviction (cf. spec §11).
    "promoted_active_via_eviction",
    "demoted_to_sell_only",
    "eviction_aborted",
    "promoted_active_via_rebound",
    "eviction_completed_to_shadow",
    "eviction_deferred_one_per_cycle",
    "eviction_deferred_sell_only_cap",
    "blacklisted",
    "blacklist_removed",
    # M5_bis Phase C (prépare le rewrite DecisionEngine demote) :
    "demoted_to_shadow",
    # M15 MB.6 — probation auto-release.
    "probation_released",
    # M15 MB.8 — auto-blacklist sur seuil PnL ou WR observé.
    "auto_blacklisted",
    # M17 MD.7 — kill switch (system-level, wallet_address=None).
    "kill_switch",
]


class TraderScoreDTO(BaseModel):
    """DTO append pour `TraderScoreRepository.insert`."""

    model_config = ConfigDict(frozen=True)

    target_trader_id: int
    wallet_address: str
    score: float
    scoring_version: str
    low_confidence: bool
    metrics_snapshot: dict[str, Any]


class TraderEventDTO(BaseModel):
    """DTO append pour `TraderEventRepository.insert` (audit trail discovery).

    M17 MD.7 : ``wallet_address`` devient ``str | None``. Default ``None``
    réservé aux events système (``event_type="kill_switch"``) qui ne sont
    pas attachés à un wallet spécifique. Migration 0010 relâche la
    contrainte DB en parallèle.
    """

    model_config = ConfigDict(frozen=True)

    wallet_address: str | None = None
    event_type: TraderEventType
    from_status: str | None = None
    to_status: str | None = None
    score_at_event: float | None = None
    scoring_version: str | None = None
    reason: str | None = None
    event_metadata: dict[str, Any] | None = None


class TraderDailyPnlDTO(BaseModel):
    """DTO append pour ``TraderDailyPnlRepository.insert_if_new`` (M12).

    Snapshot quotidien de l'equity curve d'un wallet. Source de reconstruction
    Sortino / Calmar / consistency dans le scoring v2. Les nombres viennent
    exclusivement de `/positions` + `/value` public Data API — zéro PII, zéro
    secret.
    """

    model_config = ConfigDict(frozen=True)

    wallet_address: str
    date: date
    equity_usdc: float = 0.0
    realized_pnl_day: float = 0.0
    unrealized_pnl_day: float = 0.0
    positions_count: int = 0
