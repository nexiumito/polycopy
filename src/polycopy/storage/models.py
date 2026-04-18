"""Modèles SQLAlchemy 2.0 async pour polycopy.

Tables peuplées à M1 : `target_traders`, `detected_trades`.
Tables structurelles M3+ : `my_orders`, `my_positions`, `pnl_snapshots`
(créées par `create_all` mais ni lues ni écrites avant l'Executor).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base déclarative commune à tous les modèles polycopy."""


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


class TargetTrader(Base):
    """Wallet observé par le watcher. Adresse stockée en lowercase.

    M5 étend ce modèle avec le lifecycle ``status``, le flag ``pinned`` (seed
    `TARGET_WALLETS`, jamais retiré par M5), le compteur d'hystérésis demote,
    et des timestamps d'audit (`discovered_at`, `promoted_at`, `last_scored_at`).
    """

    __tablename__ = "target_traders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)  # overwrite par cycle M5
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        nullable=False,
    )
    # --- M5 extensions (0003 migration) ----------------------------------
    # status ∈ {'shadow', 'active', 'paused', 'pinned'}. Dérivé en sync avec `active`
    # (active=True ⟺ status ∈ {'active', 'pinned'}). Indexé pour les queries
    # par status dans le dashboard + pipeline discovery.
    status: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="active",
        server_default="active",
        index=True,
    )
    # True ⟺ wallet vient de TARGET_WALLETS env. Jamais demote-able par M5.
    pinned: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    # Hystérésis demote : nombre de cycles consécutifs sous demotion_threshold.
    consecutive_low_score_cycles: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    discovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_scored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    scoring_version: Mapped[str | None] = mapped_column(String(16), nullable=True)


class DetectedTrade(Base):
    """Trade détecté on-chain pour un `TargetTrader`. Dédup par `tx_hash`."""

    __tablename__ = "detected_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(String(66), unique=True, index=True)
    target_wallet: Mapped[str] = mapped_column(String(42), index=True)
    condition_id: Mapped[str] = mapped_column(String(66), index=True)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    usdc_size: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
    )
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slug: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class StrategyDecision(Base):
    """Décision du pipeline strategy pour un `DetectedTrade` donné. Append-only."""

    __tablename__ = "strategy_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detected_trade_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(66), index=True, nullable=False)
    decision: Mapped[str] = mapped_column(String(8), nullable=False)  # APPROVED | REJECTED
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    my_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    my_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        index=True,
        nullable=False,
    )
    pipeline_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


# --- Populated from M3 onwards ----------------------------------------------


class MyOrder(Base):
    """Ordre envoyé (ou simulé en dry-run) par l'Executor. Append-only."""

    __tablename__ = "my_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_tx_hash: Mapped[str] = mapped_column(String(66), index=True, nullable=False)
    clob_order_id: Mapped[str | None] = mapped_column(String(66), index=True, nullable=True)
    condition_id: Mapped[str] = mapped_column(String(66), index=True, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY | SELL
    size: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    tick_size: Mapped[float] = mapped_column(Float, nullable=False)
    neg_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    order_type: Mapped[str] = mapped_column(String(4), nullable=False, default="FOK")
    # status enum strict : SIMULATED | SENT | FILLED | PARTIALLY_FILLED | REJECTED | FAILED
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    taking_amount: Mapped[str | None] = mapped_column(String(64), nullable=True)
    making_amount: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transaction_hashes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    error_msg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # M8 : True ⟺ fill simulé via orderbook /book (vs stub instantané M3).
    realistic_fill: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        nullable=False,
    )
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MyPosition(Base):
    """Position courante : 1 ligne par `(condition_id, asset_id, simulated)` ouvert.

    M8 : la 3ᵉ clé ``simulated`` permet la coexistence d'une position réelle et
    d'une position virtuelle (dry-run) sur le même marché sans collision.
    """

    __tablename__ = "my_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    condition_id: Mapped[str] = mapped_column(String(66), index=True, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    size: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        nullable=False,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # M8 : True ⟺ position virtuelle (dry-run realistic fill), ne correspond
    # à aucune position CLOB réelle.
    simulated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    # M8 : rempli à la résolution du marché pour les positions virtuelles
    # (`size * (winning ? 1 - avg_price : -avg_price)`). NULL si position
    # ouverte ou si position réelle (calculé hors snapshot).
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "condition_id",
            "asset_id",
            "simulated",
            name="uq_my_positions_condition_asset_simulated",
        ),
        Index("ix_my_positions_simulated_open", "simulated", "closed_at"),
    )


class PnlSnapshot(Base):
    """Snapshot PnL périodique écrit par le ``PnlSnapshotWriter`` (M4)."""

    __tablename__ = "pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        index=True,
        nullable=False,
    )
    total_usdc: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    open_positions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cash_pnl_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class TradeLatencySample(Base):
    """Échantillon de latence par stage du pipeline (M11, append-only).

    6 rows par trade (1 par stage du pipeline). Purgée à 7 jours par
    ``TradeLatencyRepository.purge_older_than`` — appelé au boot + quotidien
    par ``LatencyPurgeScheduler``. Zéro PII : ``trade_id`` est un uuid hex
    interne, pas une adresse wallet ni un tx_hash (cf. spec M11 §10.3).
    """

    __tablename__ = "trade_latency_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage_name: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        nullable=False,
    )

    __table_args__ = (Index("ix_trade_latency_samples_stage_ts", "stage_name", "timestamp"),)


# --- Populated from M5 onwards ----------------------------------------------


class TraderScore(Base):
    """Score historique append-only d'un wallet pour un cycle M5.

    Une ligne par `(wallet, cycle)`. Jamais d'update. Permet audit a posteriori
    et comparaison entre versions de formule (`scoring_version`).
    """

    __tablename__ = "trader_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_trader_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    wallet_address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(16), nullable=False)
    cycle_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        index=True,
        nullable=False,
    )
    low_confidence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metrics_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (Index("ix_trader_scores_wallet_cycle", "wallet_address", "cycle_at"),)


class TraderEvent(Base):
    """Audit trail append-only : chaque décision M5 sur un wallet.

    Écrit avec un event_type descriptif (`discovered`, `promoted_active`,
    `demoted_paused`, `kept`, `skipped_blacklist`, `skipped_cap`,
    `manual_override`) + snapshot du score à l'instant et `scoring_version`.
    """

    __tablename__ = "trader_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        index=True,
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    score_at_event: Mapped[float | None] = mapped_column(Float, nullable=True)
    scoring_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_trader_events_wallet_at", "wallet_address", "at"),)
