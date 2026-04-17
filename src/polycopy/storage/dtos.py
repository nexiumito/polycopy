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
