"""Modèles SQLAlchemy 2.0 async pour polycopy.

Tables peuplées à M1 : `target_traders`, `detected_trades`.
Tables structurelles M3+ : `my_orders`, `my_positions`, `pnl_snapshots`
(créées par `create_all` mais ni lues ni écrites avant l'Executor).
"""

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
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

    M5_bis étend le lifecycle avec deux nouveaux états — ``sell_only``
    (wind-down réversible, watcher continue à copier les SELL, BUY bloqués
    par ``TraderLifecycleFilter``) et ``blacklisted`` (terminal, piloté par
    ``BLACKLISTED_WALLETS`` env). Le status ``paused`` est retiré : la
    migration 0007 convertit les rows existantes en ``shadow`` avec
    ``previously_demoted_at`` posé comme flag UX.
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
    # --- M5 + M5_bis extensions -----------------------------------------
    # status ∈ {'shadow', 'active', 'sell_only', 'pinned', 'blacklisted'}.
    # Invariants :
    #   - active=True ⟺ status ∈ {'active', 'pinned', 'sell_only'} (watcher
    #     poll les trois pour copier SELL).
    #   - pinned=True ⟺ status='pinned'.
    # Width=16 : 'blacklisted'=11 chars, 'sell_only'=9 chars. M5 initial
    # était String(8) — migration 0007 widen.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
        index=True,
    )
    # True ⟺ wallet vient de TARGET_WALLETS env. Jamais demote-able ni
    # évinçable par M5/M5_bis.
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
    # M5_bis : flag UX posé quand un wallet repasse `active → shadow` via
    # demote hystérésis M5 (ex-`paused`) OU via migration 0007. Permet au
    # dashboard d'afficher "re-observation" plutôt qu'un shadow neuf.
    previously_demoted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # M5_bis : timestamp d'entrée dans le status courant quand celui-ci est
    # `sell_only`. Sert au dashboard (durée wind-down) et au debug audit.
    # Clear quand le wallet quitte `sell_only` (T6/T7/T8).
    eviction_state_entered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # M5_bis : wallet candidat qui a causé la transition `active → sell_only`.
    # Nullable. FK logique (pas de contrainte SQLite) vers wallet_address.
    # Clé pour évaluer la condition d'abort T6 (delta candidat↔self repasse
    # sous EVICTION_SCORE_MARGIN × N cycles).
    eviction_triggering_wallet: Mapped[str | None] = mapped_column(
        String(42),
        nullable=True,
    )


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
    """Audit trail append-only : chaque décision M5/M5_bis sur un wallet.

    Écrit avec un event_type descriptif (`discovered`, `promoted_active`,
    `demoted_to_shadow`, `kept`, `skipped_blacklist`, `skipped_cap`,
    `manual_override` pour M5 ; `promoted_active_via_eviction`,
    `demoted_to_sell_only`, `eviction_aborted`, `promoted_active_via_rebound`,
    `eviction_completed_to_shadow`, `blacklisted`, `blacklist_removed`
    pour M5_bis) + snapshot du score à l'instant et `scoring_version`.

    ``from_status`` / ``to_status`` sont élargis à ``String(16)`` par la
    migration 0007 pour accueillir ``sell_only`` (9 chars) et
    ``blacklisted`` (11 chars).
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
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    score_at_event: Mapped[float | None] = mapped_column(Float, nullable=True)
    scoring_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_trader_events_wallet_at", "wallet_address", "at"),)


class TraderDailyPnl(Base):
    """Snapshot quotidien de l'equity curve d'un wallet (M12, append-only).

    Source de reconstruction de l'equity curve nécessaire au calcul Sortino /
    Calmar / consistency dans le scoring v2 (cf. spec M12 §3.2, §3.6, §5.6).
    Écrit par ``TraderDailyPnlWriter`` (scheduler 24h co-lancé dans
    ``DiscoveryOrchestrator``). Dédup via contrainte unique ``(wallet_address,
    date)`` — idempotent sur re-run dans la même journée.

    Zéro PII, zéro secret : ``wallet_address`` est une adresse publique déjà
    loggée en M1..M11, ``equity_usdc`` est dérivé de ``/positions`` + ``/value``
    public Data API.
    """

    __tablename__ = "trader_daily_pnl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    equity_usdc: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl_day: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl_day: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    positions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshotted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "wallet_address",
            "date",
            name="uq_trader_daily_pnl_wallet_date",
        ),
        Index("ix_trader_daily_pnl_wallet_date", "wallet_address", "date"),
    )
