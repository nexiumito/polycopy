"""Queries SQL agrégées pour le daily summary Telegram (M7 §6).

Lecture seule. 1 session par query pour limiter la contention. Les agrégats
portent sur la fenêtre ``[since, now]`` — typiquement 24 h en amont.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.monitoring.dtos import DailySummaryContext, TopWalletEntry
from polycopy.monitoring.md_escape import wallet_short
from polycopy.storage.models import (
    DetectedTrade,
    MyOrder,
    MyPosition,
    PnlSnapshot,
    StrategyDecision,
    TargetTrader,
    TraderEvent,
)

if TYPE_CHECKING:
    from polycopy.config import Settings


async def collect_daily_summary_context(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    since: datetime,
    *,
    date_human: str | None = None,
    alerts_counts: dict[str, int] | None = None,
) -> DailySummaryContext:
    """Construit un ``DailySummaryContext`` agrégé depuis la DB + compteurs alertes."""
    trades_24h = await _count_trades_since(session_factory, since)
    top_wallets = await _top_wallets_since(session_factory, since, limit=3)
    approved, rejected, top_reject_reason = await _decisions_stats_since(
        session_factory,
        since,
    )
    orders_sent, orders_filled, orders_rejected, volume = await _orders_stats_since(
        session_factory,
        since,
    )
    positions_open, positions_value = await _positions_current(session_factory)
    total_usdc, delta_pct, drawdown_pct = await _pnl_stats_since(session_factory, since)
    discovery_cycles, promotions, demotions, cap_reached = await _discovery_stats_since(
        session_factory,
        since,
    )
    alerts = alerts_counts or {}
    alerts_total = sum(alerts.values())
    compact = _compact_alerts(alerts)
    dashboard_url = (
        f"http://{settings.dashboard_host}:{settings.dashboard_port}/"
        if settings.dashboard_enabled
        else None
    )
    human = date_human if date_human is not None else datetime.now(tz=UTC).strftime("%Y-%m-%d")
    return DailySummaryContext(
        date_human=human,
        trades_24h=trades_24h,
        top_wallets=top_wallets,
        decisions_approved=approved,
        decisions_rejected=rejected,
        top_reject_reason=top_reject_reason,
        orders_sent=orders_sent,
        orders_filled=orders_filled,
        orders_rejected=orders_rejected,
        volume_executed_usd=volume,
        total_usdc=total_usdc,
        delta_24h_pct=delta_pct,
        drawdown_24h_pct=drawdown_pct,
        positions_open=positions_open,
        positions_value_usd=positions_value,
        discovery_enabled=settings.discovery_enabled,
        discovery_cycles_24h=discovery_cycles,
        discovery_promotions_24h=promotions,
        discovery_demotions_24h=demotions,
        discovery_cap_reached_24h=cap_reached,
        alerts_total_24h=alerts_total,
        alerts_by_type_compact=compact,
        dashboard_url=dashboard_url,
    )


# -----------------------------------------------------------------------------
# Queries unitaires
# -----------------------------------------------------------------------------


async def _count_trades_since(
    session_factory: async_sessionmaker[AsyncSession],
    since: datetime,
) -> int:
    async with session_factory() as session:
        stmt = select(func.count(DetectedTrade.id)).where(DetectedTrade.timestamp >= since)
        return int((await session.execute(stmt)).scalar_one())


async def _top_wallets_since(
    session_factory: async_sessionmaker[AsyncSession],
    since: datetime,
    limit: int,
) -> list[TopWalletEntry]:
    async with session_factory() as session:
        stmt = (
            select(
                DetectedTrade.target_wallet,
                func.count(DetectedTrade.id).label("n"),
            )
            .where(DetectedTrade.timestamp >= since)
            .group_by(DetectedTrade.target_wallet)
            .order_by(func.count(DetectedTrade.id).desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return []
        wallets = [row[0] for row in rows]
        label_stmt = select(TargetTrader.wallet_address, TargetTrader.label).where(
            TargetTrader.wallet_address.in_(wallets),
        )
        labels = {w: label for w, label in (await session.execute(label_stmt)).all()}
    return [
        TopWalletEntry(
            wallet_short=wallet_short(row[0]),
            label=labels.get(row[0]),
            trade_count=int(row[1]),
        )
        for row in rows
    ]


async def _decisions_stats_since(
    session_factory: async_sessionmaker[AsyncSession],
    since: datetime,
) -> tuple[int, int, str | None]:
    async with session_factory() as session:
        approved_stmt = select(func.count(StrategyDecision.id)).where(
            StrategyDecision.decided_at >= since,
            StrategyDecision.decision == "APPROVED",
        )
        rejected_stmt = select(func.count(StrategyDecision.id)).where(
            StrategyDecision.decided_at >= since,
            StrategyDecision.decision == "REJECTED",
        )
        approved = int((await session.execute(approved_stmt)).scalar_one())
        rejected = int((await session.execute(rejected_stmt)).scalar_one())
        reason_stmt = (
            select(StrategyDecision.reason, func.count(StrategyDecision.id).label("n"))
            .where(
                StrategyDecision.decided_at >= since,
                StrategyDecision.decision == "REJECTED",
                StrategyDecision.reason.is_not(None),
            )
            .group_by(StrategyDecision.reason)
            .order_by(func.count(StrategyDecision.id).desc())
            .limit(1)
        )
        top_reason_row = (await session.execute(reason_stmt)).first()
    top_reason = top_reason_row[0] if top_reason_row else None
    return approved, rejected, top_reason


async def _orders_stats_since(
    session_factory: async_sessionmaker[AsyncSession],
    since: datetime,
) -> tuple[int, int, int, float]:
    async with session_factory() as session:
        stmt = (
            select(MyOrder.status, func.count(MyOrder.id).label("n"))
            .where(MyOrder.sent_at >= since)
            .group_by(MyOrder.status)
        )
        counts = {row[0]: int(row[1]) for row in (await session.execute(stmt)).all()}
        volume_stmt = select(
            func.coalesce(func.sum(MyOrder.size * MyOrder.price), 0.0),
        ).where(
            MyOrder.sent_at >= since,
            MyOrder.status.in_(("FILLED", "PARTIALLY_FILLED")),
        )
        volume = float((await session.execute(volume_stmt)).scalar_one() or 0.0)
    sent = sum(counts.get(s, 0) for s in ("SENT", "FILLED", "PARTIALLY_FILLED", "SIMULATED"))
    filled = counts.get("FILLED", 0) + counts.get("PARTIALLY_FILLED", 0)
    rejected = counts.get("REJECTED", 0) + counts.get("FAILED", 0)
    return sent, filled, rejected, volume


async def _positions_current(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, float]:
    async with session_factory() as session:
        stmt = select(MyPosition).where(MyPosition.closed_at.is_(None))
        rows = list((await session.execute(stmt)).scalars().all())
    count = len(rows)
    value = float(sum(pos.size * pos.avg_price for pos in rows))
    return count, value


async def _pnl_stats_since(
    session_factory: async_sessionmaker[AsyncSession],
    since: datetime,
) -> tuple[float | None, float | None, float | None]:
    async with session_factory() as session:
        latest_stmt = select(PnlSnapshot.total_usdc).order_by(PnlSnapshot.timestamp.desc()).limit(1)
        latest_row = (await session.execute(latest_stmt)).first()
        total = float(latest_row[0]) if latest_row else None

        prev_stmt = (
            select(PnlSnapshot.total_usdc)
            .where(PnlSnapshot.timestamp <= since)
            .order_by(PnlSnapshot.timestamp.desc())
            .limit(1)
        )
        prev_row = (await session.execute(prev_stmt)).first()
        delta_pct: float | None = None
        if total is not None and prev_row is not None:
            prev_total = float(prev_row[0])
            if prev_total > 0:
                delta_pct = round((total - prev_total) / prev_total * 100.0, 2)

        dd_stmt = select(func.max(PnlSnapshot.drawdown_pct)).where(
            PnlSnapshot.timestamp >= since,
        )
        dd_val = (await session.execute(dd_stmt)).scalar()
        drawdown = float(dd_val) if dd_val is not None else None
    return total, delta_pct, drawdown


async def _discovery_stats_since(
    session_factory: async_sessionmaker[AsyncSession],
    since: datetime,
) -> tuple[int, int, int, int]:
    async with session_factory() as session:
        stmt = (
            select(TraderEvent.event_type, func.count(TraderEvent.id).label("n"))
            .where(TraderEvent.at >= since)
            .group_by(TraderEvent.event_type)
        )
        counts = {row[0]: int(row[1]) for row in (await session.execute(stmt)).all()}
    cycles = counts.get("cycle_started", 0)
    promotions = counts.get("promoted_active", 0) + counts.get("promoted", 0)
    demotions = counts.get("demoted_paused", 0) + counts.get("demoted", 0)
    cap_reached = counts.get("skipped_cap", 0)
    return cycles, promotions, demotions, cap_reached


def _compact_alerts(counts: dict[str, int]) -> str:
    """``{"order_filled_large": 5, "drawdown": 2}`` → ``"filled:5 · drawdown:2"``."""
    if not counts:
        return ""
    items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    parts = [f"{_short_event(name)}:{n}" for name, n in items]
    return " · ".join(parts)


def _short_event(event: str) -> str:
    """Raccourcit l'event_type pour les compteurs compacts (10 chars max)."""
    mapping = {
        "kill_switch_triggered": "kill_switch",
        "pnl_snapshot_drawdown": "drawdown",
        "order_filled_large": "filled",
        "executor_error": "exec_err",
        "executor_auth_fatal": "auth_fatal",
        "trader_promoted": "promoted",
        "trader_demoted": "demoted",
        "discovery_cap_reached": "cap",
        "discovery_cycle_failed": "disco_err",
    }
    return mapping.get(event, event[:10])
