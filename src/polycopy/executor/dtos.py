"""DTOs internes du module Executor.

`MyOrderDTO` (input pour `MyOrderRepository.insert`) vit dans
`storage/dtos.py` pour cohérence avec les autres DTOs de repos.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BuiltOrder(BaseModel):
    """Snapshot d'un ordre prêt à signer (consommé par `ClobWriteClient.post_order`)."""

    model_config = ConfigDict(frozen=True)

    token_id: str
    side: Literal["BUY", "SELL"]
    # Sémantique: pour FOK BUY = USD à dépenser, pour FOK SELL = shares à vendre.
    # Cf. spec §6.4. À confirmer empiriquement au 1er run réel.
    size: float
    price: float  # déjà arrondi à `tick_size`
    tick_size: float
    neg_risk: bool
    order_type: Literal["FOK", "FAK", "GTC"]


class OrderResult(BaseModel):
    """Réponse CLOB normalisée (`POST /order`).

    `extra="allow"` pour absorber des champs futurs sans casser.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow", frozen=True)

    success: bool
    clob_order_id: str | None = Field(default=None, alias="orderID")
    status: Literal["matched", "live", "delayed"] | None = None
    making_amount: str | None = Field(default=None, alias="makingAmount")
    taking_amount: str | None = Field(default=None, alias="takingAmount")
    transaction_hashes: list[str] = Field(default_factory=list, alias="transactionsHashes")
    trade_ids: list[str] = Field(default_factory=list, alias="tradeIDs")
    error_msg: str = Field(default="", alias="errorMsg")


class WalletState(BaseModel):
    """État du wallet pour le RiskManager — utilisé côté Executor en `dry_run=false`."""

    model_config = ConfigDict(frozen=True)

    total_position_value_usd: float
    available_capital_usd: float
    open_positions_count: int


class ExecutorAuthError(Exception):
    """Erreur d'auth CLOB — fatale, stop l'orchestrateur."""


class ExecutorValidationError(Exception):
    """Erreur de validation CLOB — ordre rejeté définitivement, ne pas retry."""
