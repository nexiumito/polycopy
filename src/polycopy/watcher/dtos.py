"""DTOs Pydantic pour les réponses Polymarket Data API.

Schéma extrait de https://docs.polymarket.com/api-reference/core/get-user-activity
et confirmé via le skill `/polymarket:polymarket`. `extra="allow"` pour encaisser
de futurs champs sans casser les DTOs.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ActivityType = Literal[
    "TRADE",
    "SPLIT",
    "MERGE",
    "REDEEM",
    "REWARD",
    "CONVERSION",
    "MAKER_REBATE",
    "REFERRAL_REWARD",
]


class TradeActivity(BaseModel):
    """Sous-ensemble utile d'une ligne `Activity` filtrée sur `type=TRADE`."""

    model_config = ConfigDict(populate_by_name=True, extra="allow", frozen=True)

    proxy_wallet: str = Field(alias="proxyWallet")
    timestamp: int  # unix seconds
    condition_id: str = Field(alias="conditionId")
    asset: str  # token_id ERC1155 CTF
    side: Literal["BUY", "SELL"]
    size: float
    usdc_size: float = Field(alias="usdcSize")
    price: float
    transaction_hash: str = Field(alias="transactionHash")
    outcome: str | None = None
    slug: str | None = None
    type: ActivityType
