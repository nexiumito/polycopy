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
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.models import (
    DetectedTrade,
    MyOrder,
    MyPosition,
    PnlSnapshot,
    StrategyDecision,
    TargetTrader,
    TraderEvent,
    TraderScore,
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
    # --- M5 discovery KPIs ------------------------------------------------
    discovery_last_cycle_at: datetime | None = None
    discovery_cycles_24h: int = 0
    discovery_promotions_24h: int = 0
    discovery_demotions_24h: int = 0
    discovery_shadow_count: int = 0
    discovery_active_count: int = 0
    discovery_paused_count: int = 0


@dataclass(frozen=True)
class TraderRow:
    """Ligne pour la page `/traders` (1 ligne = 1 wallet + latest score)."""

    wallet_address: str
    label: str | None
    status: str
    pinned: bool
    score: float | None
    scoring_version: str | None
    last_scored_at: datetime | None
    discovered_at: datetime | None
    consecutive_low_score_cycles: int


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

        # M5 KPIs : count per status + événements dernières 24h.
        status_counts_rows = (
            await session.execute(
                select(TargetTrader.status, func.count(TargetTrader.id)).group_by(
                    TargetTrader.status
                ),
            )
        ).all()
        status_counts = {str(r[0]): int(r[1]) for r in status_counts_rows}

        event_counts_rows = (
            await session.execute(
                select(TraderEvent.event_type, func.count(TraderEvent.id))
                .where(TraderEvent.at >= since_24h)
                .group_by(TraderEvent.event_type),
            )
        ).all()
        events_24h = {str(r[0]): int(r[1]) for r in event_counts_rows}

        last_cycle = (
            await session.execute(
                select(func.max(TraderScore.cycle_at)),
            )
        ).scalar_one_or_none()

        discovery_cycles_24h = int(
            (
                await session.execute(
                    select(func.count(func.distinct(TraderScore.cycle_at))).where(
                        TraderScore.cycle_at >= since_24h,
                    ),
                )
            ).scalar_one()
        )

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
        discovery_last_cycle_at=last_cycle,
        discovery_cycles_24h=discovery_cycles_24h,
        discovery_promotions_24h=int(events_24h.get("promoted_active", 0)),
        discovery_demotions_24h=int(events_24h.get("demoted_paused", 0)),
        discovery_shadow_count=int(status_counts.get("shadow", 0)),
        discovery_active_count=int(status_counts.get("active", 0))
        + int(status_counts.get("pinned", 0)),
        discovery_paused_count=int(status_counts.get("paused", 0)),
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


# --- M5 discovery queries ---------------------------------------------------


_VALID_TRADER_STATUSES = frozenset({"shadow", "active", "paused", "pinned"})


async def list_traders(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[TraderRow]:
    """Liste les traders pour la page `/traders`.

    Trié par ``score DESC NULLS LAST`` par défaut. Filtre ``status`` optionnel
    (invalide → ignoré, cf. pattern UX M4.5).
    """
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    async with session_factory() as session:
        stmt = select(TargetTrader)
        if status is not None and status in _VALID_TRADER_STATUSES:
            stmt = stmt.where(TargetTrader.status == status)
        # SQLite : score None au bout via coalesce(..., -1.0).
        stmt = (
            stmt.order_by(
                func.coalesce(TargetTrader.score, -1.0).desc(),
                TargetTrader.added_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        traders = list(result.scalars().all())
    return [
        TraderRow(
            wallet_address=t.wallet_address,
            label=t.label,
            status=t.status,
            pinned=bool(t.pinned),
            score=float(t.score) if t.score is not None else None,
            scoring_version=t.scoring_version,
            last_scored_at=t.last_scored_at,
            discovered_at=t.discovered_at,
            consecutive_low_score_cycles=int(t.consecutive_low_score_cycles),
        )
        for t in traders
    ]


async def count_traders_by_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """Compte de traders par status (pour sidebar /traders)."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(TargetTrader.status, func.count(TargetTrader.id)).group_by(
                    TargetTrader.status
                ),
            )
        ).all()
    return {str(r[0]): int(r[1]) for r in rows}


def backtest_report_path() -> Path:
    """Chemin canonique du rapport backtest (convention `/backtest` page)."""
    # Path au niveau repo root (2 niveaux au-dessus de src/polycopy/dashboard).
    return Path(__file__).resolve().parents[3] / "backtest_v1_report.html"


def backtest_report_exists() -> bool:
    """True si le backtest a déjà été généré à la racine du repo."""
    return backtest_report_path().is_file()
