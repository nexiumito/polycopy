"""DTOs Pydantic et structures internes du Strategy Engine."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from polycopy.storage.dtos import DetectedTradeDTO


class MarketMetadata(BaseModel):
    """Sous-ensemble Gamma /markets utile au pipeline. `extra="allow"` pour absorber
    les champs additionnels (ex: `negRisk`, `feeType`) sans casser les DTOs."""

    model_config = ConfigDict(populate_by_name=True, extra="allow", frozen=True)

    market_id: str = Field(alias="id")
    condition_id: str = Field(alias="conditionId")
    question: str | None = None
    slug: str | None = None
    active: bool | None = None
    closed: bool | None = None
    archived: bool | None = None
    accepting_orders: bool | None = Field(default=None, alias="acceptingOrders")
    enable_order_book: bool | None = Field(default=None, alias="enableOrderBook")
    liquidity_clob: float | None = Field(default=None, alias="liquidityClob")
    end_date: datetime | None = Field(default=None, alias="endDate")
    end_date_iso: str | None = Field(default=None, alias="endDateIso")
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[str] = Field(default_factory=list, alias="outcomePrices")
    neg_risk: bool = Field(default=False, alias="negRisk")
    fee_type: str | None = Field(default=None, alias="feeType")
    """M16 : type de fee Polymarket (ex: 'crypto_fees_v2', 'sports_fees_v2').
    None / null pour les markets fee-free (vaste majorité Politics/Tech/Finance
    pré-rollout). Mapping → params formule cf. PositionSizer._compute_effective_fee_rate."""
    fees_enabled: bool | None = Field(default=None, alias="feesEnabled")
    """M16 : flag Gamma indiquant si le marché a des fees actives. Optionnel
    (null pour les markets pre-rollout March 30 2026). Présent en plus du fee_type
    pour cohérence avec l'API Gamma — non utilisé par le PositionSizer (le filter
    se base uniquement sur fee_type via la formule officielle)."""

    @field_validator("clob_token_ids", "outcomes", "outcome_prices", mode="before")
    @classmethod
    def _parse_json_string(cls, v: object) -> object:
        """Gamma renvoie ces champs en strings JSON-stringifiées (`'["a","b"]'`)."""
        if v is None:
            return []
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            return json.loads(stripped)
        return v


class OrderApproved(BaseModel):
    """Event poussé sur `approved_orders_queue` (consommé par l'Executor à M3).

    M11 : ``trade_id`` (uuid hex) propagé depuis le ``DetectedTradeDTO`` pour
    rebinder le contextvar structlog côté executor et compléter la chaîne
    d'instrumentation (stage 6 = ``executor_submitted_ms``). Nullable pour
    backward-compat tests M2..M10.

    M15 MB.1 : ``source_wallet_address`` propagé depuis
    ``DetectedTradeDTO.target_wallet`` pour permettre à l'Executor de
    persister ``MyPosition.source_wallet_address`` à la création — alimente
    le collecteur ``_compute_internal_pnl_score``. Nullable + lower-cased
    par convention. Cf. spec M15 §14.5.
    """

    model_config = ConfigDict(frozen=True)

    detected_trade_id: int
    tx_hash: str
    condition_id: str
    asset_id: str
    side: Literal["BUY", "SELL"]
    my_size: float
    my_price: float
    trade_id: str | None = None
    source_wallet_address: str | None = None


@dataclass
class FilterResult:
    """Résultat d'un filtre du pipeline : passé ou rejeté avec une raison courte."""

    passed: bool
    reason: str | None = None


@dataclass
class PipelineContext:
    """État partagé entre filtres dans un même run de pipeline.

    Mutable par construction (chaque filtre enrichit le contexte). Sérialisé
    pour audit via `to_audit_dict` au moment de persister la décision.
    """

    trade: DetectedTradeDTO
    market: MarketMetadata | None = None
    midpoint: float | None = None
    my_size: float | None = None
    slippage_pct: float | None = None
    # --- M16 : fee adjustment (defaults None pour backward-compat M2..M15) ---
    fee_rate: float | None = None
    """Effective fee rate appliqué (Decimal converti, range [0, 0.018])."""
    fee_cost_usd: float | None = None
    """Fee cost USD calculé (notional × fee_rate)."""
    ev_after_fee_usd: float | None = None
    """EV USD post-fee approximé (max_gain - fee_cost)."""
    filter_trace: list[dict[str, Any]] = field(default_factory=list)

    def record_filter(self, name: str, result: FilterResult) -> None:
        """Trace l'exécution d'un filtre pour audit."""
        self.filter_trace.append(
            {
                "filter": name,
                "passed": result.passed,
                "reason": result.reason,
            },
        )

    def to_audit_dict(self) -> dict[str, Any]:
        """Snapshot sérialisable JSON pour la colonne `pipeline_state`."""
        return {
            "tx_hash": self.trade.tx_hash,
            "condition_id": self.trade.condition_id,
            "asset_id": self.trade.asset_id,
            "source_size": self.trade.size,
            "source_price": self.trade.price,
            "midpoint": self.midpoint,
            "my_size": self.my_size,
            "slippage_pct": self.slippage_pct,
            "fee_rate": self.fee_rate,
            "fee_cost_usd": self.fee_cost_usd,
            "ev_after_fee_usd": self.ev_after_fee_usd,
            "market": (
                self.market.model_dump(mode="json", by_alias=True)
                if self.market is not None
                else None
            ),
            "filter_trace": self.filter_trace,
        }
