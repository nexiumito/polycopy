"""DTOs internes du module Executor.

`MyOrderDTO` (input pour `MyOrderRepository.insert`) vit dans
`storage/dtos.py` pour cohérence avec les autres DTOs de repos.
"""

from datetime import datetime
from decimal import Decimal
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


# --- M8 : orderbook + realistic fill simulation ----------------------------


class OrderbookLevel(BaseModel):
    """Un niveau de l'orderbook CLOB. ``price`` et ``size`` en ``Decimal`` —
    les payloads ``/book`` retournent des strings (precision arbitraire)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    price: Decimal
    size: Decimal


class Orderbook(BaseModel):
    """Snapshot orderbook ``GET /book?token_id=<id>`` (read-only public, M8).

    ``bids`` triés du meilleur au pire (prix décroissant). ``asks`` triés du
    meilleur au pire (prix croissant). Le tri est garanti par le client lecteur.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    asset_id: str
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]
    snapshot_at: datetime
    raw_hash: str | None = None


class RealisticFillResult(BaseModel):
    """Résultat de ``simulate_fill`` — soit fill virtuel, soit reject FOK.

    Les champs ``filled_size`` / ``avg_fill_price`` / ``shortfall`` sont en
    ``float`` pour la persistance DB et les logs structlog. Les calculs
    intermédiaires utilisent ``Decimal`` (cf. ``simulate_fill``).
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["SIMULATED", "REJECTED"]
    reason: str | None = None
    requested_size: float
    filled_size: float = 0.0
    avg_fill_price: float | None = None
    depth_consumed_shares: float = 0.0
    depth_consumed_levels: int = 0
    shortfall: float = 0.0
