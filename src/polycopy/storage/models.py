"""Modèles SQLAlchemy 2.0 async pour polycopy.

Tables peuplées à M1 : `target_traders`, `detected_trades`.
Tables structurelles M3+ : `my_orders`, `my_positions`, `pnl_snapshots`
(créées par `create_all` mais ni lues ni écrites avant l'Executor).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base déclarative commune à tous les modèles polycopy."""


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


class TargetTrader(Base):
    """Wallet observé par le watcher. Adresse stockée en lowercase."""

    __tablename__ = "target_traders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)  # peuplé à M5
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        nullable=False,
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
    """Ordre envoyé par notre Executor. Populated from M3 onwards."""

    __tablename__ = "my_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clob_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    side: Mapped[str | None] = mapped_column(String(4), nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MyPosition(Base):
    """Position courante. Populated from M3 onwards."""

    __tablename__ = "my_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    condition_id: Mapped[str | None] = mapped_column(String(66), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PnlSnapshot(Base):
    """Snapshot PnL périodique. Populated from M3 onwards."""

    __tablename__ = "pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
