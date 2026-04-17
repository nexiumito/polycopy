"""DTOs Pydantic pour la couche storage."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class DetectedTradeDTO(BaseModel):
    """Représentation d'un trade prête pour insertion en base."""

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
