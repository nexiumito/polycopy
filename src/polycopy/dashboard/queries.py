"""Helpers SELECT read-only pour le dashboard (M4.5).

Chaque fonction reçoit un ``session_factory`` et ouvre **une session courte**,
fermée avant tout rendering. Zéro ``add`` / ``commit`` / ``delete`` — le
dashboard ne peut littéralement pas muter la DB (vérifié via
``test_dashboard_security.py``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.models import (
    DetectedTrade,
    MyOrder,
    MyPosition,
    PnlSnapshot,
    StrategyDecision,
)

_MAX_LIMIT = 200

_SINCE_WINDOWS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _clamp_limit(limit: int) -> int:
    """Clamp ``limit`` dans ``[1, _MAX_LIMIT]``."""
    if limit < 1:
        return 1
    return min(limit, _MAX_LIMIT)


def _clamp_offset(offset: int) -> int:
    """Clamp ``offset`` à ``>= 0``."""
    return max(offset, 0)


def parse_since(raw: str | None) -> timedelta:
    """Parse ``?since=`` (``1h``/``24h``/``7d``/``30d``). Fallback défensif sur 24h.

    UX > strictness (cf. spec §5.4) : une valeur invalide ne doit pas 422.
    """
    if raw is None:
        return _SINCE_WINDOWS["24h"]
    return _SINCE_WINDOWS.get(raw.strip().lower(), _SINCE_WINDOWS["24h"])


@dataclass(frozen=True)
class HomeKpis:
    """KPIs agrégés pour la page Home (snapshot à l'instant T)."""

    latest_total_usdc: float | None
    latest_drawdown_pct: float | None
    open_positions_count: int
    detected_trades_24h: int
    orders_24h_by_status: dict[str, int]
    # TODO M5 : persister les alertes dans une table pour historisation dashboard.
    # À M4.5 les Alert M4 ne sont pas en DB (cf. spec §6.2) → toujours None.
    last_alert_event: str | None
    last_alert_at: datetime | None


@dataclass(frozen=True)
class PnlSeries:
    """Série temporelle pour Chart.js (x = timestamps ISO UTC)."""

    timestamps: list[datetime]
    total_usdc: list[float]
    drawdown_pct: list[float]


async def fetch_home_kpis(
    session_factory: async_sessionmaker[AsyncSession],
) -> HomeKpis:
    """Agrège les KPIs Home en une seule session courte."""
    now = datetime.now(tz=UTC)
    since_24h = now - timedelta(hours=24)
    async with session_factory() as session:
        latest_snapshot = (
            await session.execute(
                select(PnlSnapshot).order_by(PnlSnapshot.timestamp.desc()).limit(1),
            )
        ).scalar_one_or_none()

        open_positions_count = int(
            (
                await session.execute(
                    select(func.count(MyPosition.id)).where(MyPosition.closed_at.is_(None)),
                )
            ).scalar_one()
        )

        detected_trades_24h = int(
            (
                await session.execute(
                    select(func.count(DetectedTrade.id)).where(
                        DetectedTrade.timestamp >= since_24h,
                    ),
                )
            ).scalar_one()
        )

        orders_rows = (
            await session.execute(
                select(MyOrder.status, func.count(MyOrder.id))
                .where(MyOrder.sent_at >= since_24h)
                .group_by(MyOrder.status),
            )
        ).all()
        orders_24h_by_status = {str(row[0]): int(row[1]) for row in orders_rows}

    return HomeKpis(
        latest_total_usdc=(
            float(latest_snapshot.total_usdc) if latest_snapshot is not None else None
        ),
        latest_drawdown_pct=(
            float(latest_snapshot.drawdown_pct) if latest_snapshot is not None else None
        ),
        open_positions_count=open_positions_count,
        detected_trades_24h=detected_trades_24h,
        orders_24h_by_status=orders_24h_by_status,
        last_alert_event=None,
        last_alert_at=None,
    )


async def list_detected_trades(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    wallet: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[DetectedTrade]:
    """Liste les trades détectés (``timestamp`` desc). Filtre wallet optionnel."""
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    async with session_factory() as session:
        stmt = select(DetectedTrade).order_by(DetectedTrade.timestamp.desc())
        if wallet:
            stmt = stmt.where(DetectedTrade.target_wallet == wallet.lower())
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_strategy_decisions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    decision: Literal["APPROVED", "REJECTED"] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[StrategyDecision]:
    """Liste les décisions strategy (``decided_at`` desc). Filtre decision optionnel."""
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    async with session_factory() as session:
        stmt = select(StrategyDecision).order_by(StrategyDecision.decided_at.desc())
        if decision is not None:
            stmt = stmt.where(StrategyDecision.decision == decision)
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def count_strategy_reasons(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """Compte les décisions rejetées par ``reason`` (pour sidebar stats)."""
    async with session_factory() as session:
        stmt = (
            select(StrategyDecision.reason, func.count(StrategyDecision.id))
            .where(StrategyDecision.decision == "REJECTED")
            .group_by(StrategyDecision.reason)
        )
        result = await session.execute(stmt)
        return {
            (str(row[0]) if row[0] is not None else "unknown"): int(row[1]) for row in result.all()
        }


async def list_orders(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MyOrder]:
    """Liste les ordres (``sent_at`` desc). Filtre status optionnel (invalide → ignoré)."""
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    async with session_factory() as session:
        stmt = select(MyOrder).order_by(MyOrder.sent_at.desc())
        if status is not None and status in {
            "SIMULATED",
            "SENT",
            "FILLED",
            "PARTIALLY_FILLED",
            "REJECTED",
            "FAILED",
        }:
            stmt = stmt.where(MyOrder.status == status)
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_positions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    state: Literal["open", "closed"] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MyPosition]:
    """Liste les positions (``opened_at`` desc). Filtre state optionnel."""
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    async with session_factory() as session:
        stmt = select(MyPosition).order_by(MyPosition.opened_at.desc())
        if state == "open":
            stmt = stmt.where(MyPosition.closed_at.is_(None))
        elif state == "closed":
            stmt = stmt.where(MyPosition.closed_at.is_not(None))
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def fetch_pnl_series(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since: timedelta,
    include_dry_run: bool = False,
) -> PnlSeries:
    """Charge la série PnL pour Chart.js (``timestamp`` asc)."""
    cutoff = datetime.now(tz=UTC) - since
    async with session_factory() as session:
        stmt = (
            select(PnlSnapshot)
            .where(PnlSnapshot.timestamp >= cutoff)
            .order_by(PnlSnapshot.timestamp.asc())
        )
        if not include_dry_run:
            stmt = stmt.where(PnlSnapshot.is_dry_run.is_(False))
        result = await session.execute(stmt)
        snapshots = list(result.scalars().all())

    return PnlSeries(
        timestamps=[snap.timestamp for snap in snapshots],
        total_usdc=[float(snap.total_usdc) for snap in snapshots],
        drawdown_pct=[float(snap.drawdown_pct) for snap in snapshots],
    )


def aggregate_orders_by_status(orders: list[MyOrder]) -> dict[str, int]:
    """Petit helper in-memory pour afficher un compteur par status côté page."""
    return dict(Counter(o.status for o in orders))
