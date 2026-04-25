"""DTOs Pydantic v2 du module discovery M5.

Tous frozen (immutabilité -> facilite le debug + raisonnement concurrent).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DiscoverySource = Literal[
    "holders",
    "global_trades",
    "goldsky",
    "seed_target_wallets",
]

DecisionKind = Literal[
    "discovered_shadow",
    "promote_active",
    "demote_paused",  # deprecated M5_bis Phase C : remplacé par demote_shadow
    "demote_shadow",  # M5_bis : active → shadow (fusion avec previously_demoted_at)
    "keep",
    "skip_blacklist",
    "skip_cap",
    "revived_shadow",
]

TraderStatus = Literal[
    "shadow",
    "active",
    "paused",  # deprecated M5_bis Phase A, retiré par DecisionEngine Phase C
    "pinned",
    "absent",
    "sell_only",  # M5_bis : wind-down réversible
    "blacklisted",  # M5_bis : terminal, piloté par BLACKLISTED_WALLETS env
]


class CandidateWallet(BaseModel):
    """Wallet candidat extrait du pool de découverte M5."""

    model_config = ConfigDict(frozen=True)

    wallet_address: str
    discovered_via: DiscoverySource
    initial_signal: float = 0.0  # signal pré-scoring (appearance count × log(volume))
    sample_market: str | None = None  # conditionId où on l'a vu (debug)


class TraderMetrics(BaseModel):
    """Metrics agrégées sur la fenêtre `SCORING_LOOKBACK_DAYS` d'un wallet."""

    model_config = ConfigDict(frozen=True)

    wallet_address: str
    resolved_positions_count: int = 0
    open_positions_count: int = 0
    win_rate: float = 0.0  # ∈ [0, 1]
    realized_roi: float = 0.0  # peut être négatif, clipped en scoring
    total_volume_usd: float = 0.0
    herfindahl_index: float = 1.0  # ∈ [0, 1] — 1 = tout sur 1 marché
    nb_distinct_markets: int = 0
    largest_position_value_usd: float = 0.0
    measurement_window_days: int = 90
    fetched_at: datetime


class ScoringResult(BaseModel):
    """Résultat du scoring d'un wallet pour un cycle donné."""

    model_config = ConfigDict(frozen=True)

    wallet_address: str
    score: float = Field(ge=0.0, le=1.0)
    scoring_version: str
    low_confidence: bool
    metrics: TraderMetrics
    cycle_at: datetime


class DiscoveryDecision(BaseModel):
    """Décision prise par le decision_engine pour 1 wallet sur 1 cycle."""

    model_config = ConfigDict(frozen=True)

    wallet_address: str
    decision: DecisionKind
    from_status: TraderStatus | None = None
    to_status: TraderStatus
    score_at_event: float | None = None
    scoring_version: str
    reason: str
    event_metadata: dict[str, Any] = Field(default_factory=dict)


class HolderEntry(BaseModel):
    """Entrée brute `/holders` filtrée aux champs utiles M5."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    proxy_wallet: str = Field(alias="proxyWallet")
    amount: float = 0.0
    outcome_index: int | None = Field(default=None, alias="outcomeIndex")
    pseudonym: str | None = None
    name: str | None = None


class GlobalTrade(BaseModel):
    """Entrée brute `/trades` (feed global)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    proxy_wallet: str = Field(alias="proxyWallet")
    asset: str
    condition_id: str = Field(alias="conditionId")
    side: Literal["BUY", "SELL"]
    size: float
    price: float
    # usdcSize n'est pas retourné par l'API (divergence §14.5 #2) : recalculé en propriété.
    timestamp: int
    transaction_hash: str = Field(alias="transactionHash")
    title: str | None = None
    slug: str | None = None

    @property
    def usdc_size(self) -> float:
        """USD notional du trade = size × price (calculé client-side, cf. §14.5 #2)."""
        return float(self.size) * float(self.price)


class WalletValue(BaseModel):
    """Entrée brute `/value?user=<addr>` (sanity check capital)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    user: str
    value: float = 0.0


class RawPosition(BaseModel):
    """Position Polymarket `/positions` — subset utile au metrics_collector M5.

    Les nombres sont renvoyés en string par l'API ; `mode='before'` du Pydantic
    v2 les convertit automatiquement en float grâce au typage.

    M14 (MA.6) : ajout `opened_at` (optionnel). Data API `/positions` actuelle
    n'expose pas ce champ (vérifié 2026-04-25 sur fixture sample) — laissé
    `None` par le collector. Permet aux futures sources (Goldsky subgraph,
    `detected_trades` first-trade proxy) d'alimenter le filtre temporel
    `_compute_zombie_ratio` <30j sans nouvelle migration DTO.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    condition_id: str = Field(alias="conditionId")
    asset: str
    size: float = 0.0
    avg_price: float = Field(default=0.0, alias="avgPrice")
    initial_value: float = Field(default=0.0, alias="initialValue")
    current_value: float = Field(default=0.0, alias="currentValue")
    cash_pnl: float = Field(default=0.0, alias="cashPnl")
    realized_pnl: float = Field(default=0.0, alias="realizedPnl")
    total_bought: float = Field(default=0.0, alias="totalBought")
    redeemable: bool = False
    opened_at: datetime | None = None  # M14 MA.6 — Data API ne fournit pas (yet)

    @property
    def is_resolved(self) -> bool:
        """Heuristique "position résolue" (§14.5 #4).

        Une position est considérée résolue si au moins 1 des 3 indicateurs :
        - `redeemable=True` (le market a été résolu, l'utilisateur peut redeem)
        - `current_value == 0` (valeur nulle = position fermée)
        - `realized_pnl != 0` (PnL comptabilisé)
        """
        return (
            self.redeemable or float(self.current_value) == 0.0 or float(self.realized_pnl) != 0.0
        )


class GoldskyUserPosition(BaseModel):
    """Entrée GraphQL Goldsky `userPositions` (pnl-subgraph)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    user: str
    token_id: str = Field(alias="tokenId")
    amount: str
    avg_price: str = Field(alias="avgPrice")
    realized_pnl: str = Field(alias="realizedPnl")
    total_bought: str = Field(alias="totalBought")
