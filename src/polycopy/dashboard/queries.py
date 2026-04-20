"""Helpers SELECT read-only pour le dashboard (M4.5).

Chaque fonction reçoit un ``session_factory`` et ouvre **une session courte**,
fermée avant tout rendering. Zéro ``add`` / ``commit`` / ``delete`` — le
dashboard ne peut littéralement pas muter la DB (vérifié via
``test_dashboard_security.py``).
"""

from __future__ import annotations

import asyncio
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.dashboard.dtos import DiscoveryStatus, KpiCard, PnlMilestone
from polycopy.storage.models import (
    DetectedTrade,
    MyOrder,
    MyPosition,
    PnlSnapshot,
    StrategyDecision,
    TargetTrader,
    TradeLatencySample,
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


_VALID_PNL_MODES = frozenset({"real", "dry_run", "both"})


async def fetch_pnl_series(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since: timedelta,
    include_dry_run: bool = False,
    mode: str | None = None,
) -> PnlSeries:
    """Charge la série PnL pour Chart.js (``timestamp`` asc).

    ``mode`` (M8) prend la priorité sur ``include_dry_run`` :
    - ``"real"``    → uniquement ``is_dry_run=False`` (default historique).
    - ``"dry_run"`` → uniquement ``is_dry_run=True``.
    - ``"both"``    → les deux (legacy ``include_dry_run=True``).
    """
    cutoff = datetime.now(tz=UTC) - since
    effective_mode = mode if mode in _VALID_PNL_MODES else None
    async with session_factory() as session:
        stmt = (
            select(PnlSnapshot)
            .where(PnlSnapshot.timestamp >= cutoff)
            .order_by(PnlSnapshot.timestamp.asc())
        )
        if effective_mode == "dry_run":
            stmt = stmt.where(PnlSnapshot.is_dry_run.is_(True))
        elif effective_mode == "real":
            stmt = stmt.where(PnlSnapshot.is_dry_run.is_(False))
        elif effective_mode == "both":
            pass  # no filter
        elif not include_dry_run:
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


# --- M6 : KPI cards Home + Discovery status + PnL milestones + version ------


def _delta_sign(delta: float | None) -> Literal["positive", "negative", "neutral"] | None:
    """Détermine le sens d'un delta numérique (cosmétique pour la card)."""
    if delta is None:
        return None
    if delta > 0:
        return "positive"
    if delta < 0:
        return "negative"
    return "neutral"


def _format_card_usd(value: float | None) -> str:
    """Formatage USD entier arrondi avec séparateurs de milliers (ex. ``$1,024``)."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(round(value)):,}"


def _format_card_delta(delta_pct: float | None) -> str | None:
    """Formate un delta % pour affichage card (None → invisible)."""
    if delta_pct is None:
        return None
    sign = "+" if delta_pct > 0 else ""
    return f"{sign}{delta_pct:.1f}%"


async def get_home_kpi_cards(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[KpiCard]:
    """Construit les 4 cards KPI Home avec sparkline 24h (cf. spec §7.2).

    Cards : Total USDC, Drawdown, Positions ouvertes, Trades détectés 24h.
    """
    now = datetime.now(tz=UTC)
    since_24h = now - timedelta(hours=24)
    async with session_factory() as session:
        snapshots = list(
            (
                await session.execute(
                    select(PnlSnapshot)
                    .where(
                        PnlSnapshot.timestamp >= since_24h,
                        PnlSnapshot.is_dry_run.is_(False),
                    )
                    .order_by(PnlSnapshot.timestamp.asc()),
                )
            )
            .scalars()
            .all(),
        )
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

    total_points: list[tuple[datetime, float]] = [
        (s.timestamp, float(s.total_usdc)) for s in snapshots
    ]
    drawdown_points: list[tuple[datetime, float]] = [
        (s.timestamp, float(s.drawdown_pct)) for s in snapshots
    ]
    total_value = float(latest_snapshot.total_usdc) if latest_snapshot is not None else None
    drawdown_value = float(latest_snapshot.drawdown_pct) if latest_snapshot is not None else None
    total_delta_pct: float | None = None
    if len(total_points) >= 2 and total_points[0][1] > 0:
        first = total_points[0][1]
        last = total_points[-1][1]
        total_delta_pct = ((last - first) / first) * 100.0

    return [
        KpiCard(
            title="Total USDC",
            value=_format_card_usd(total_value),
            delta=_format_card_delta(total_delta_pct),
            delta_sign=_delta_sign(total_delta_pct),
            sparkline_points=total_points,
            icon="dollar-sign",
        ),
        KpiCard(
            title="Drawdown",
            value=(f"{drawdown_value:.1f}%" if drawdown_value is not None else "—"),
            delta=None,
            delta_sign=("negative" if drawdown_value and drawdown_value > 0 else "neutral"),
            sparkline_points=drawdown_points,
            icon="trending-down",
        ),
        KpiCard(
            title="Positions ouvertes",
            value=str(open_positions_count),
            delta=None,
            delta_sign=None,
            sparkline_points=[],
            icon="layers",
        ),
        KpiCard(
            title="Trades détectés (24 h)",
            value=str(detected_trades_24h),
            delta=None,
            delta_sign=None,
            sparkline_points=[],
            icon="activity",
        ),
    ]


async def get_discovery_status(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    enabled: bool,
) -> DiscoveryStatus:
    """Agrégat ``target_traders`` + ``trader_events`` 24h pour le bloc Home M5."""
    now = datetime.now(tz=UTC)
    since_24h = now - timedelta(hours=24)
    async with session_factory() as session:
        status_rows = (
            await session.execute(
                select(TargetTrader.status, func.count(TargetTrader.id)).group_by(
                    TargetTrader.status,
                ),
            )
        ).all()
        counts = {str(r[0]): int(r[1]) for r in status_rows}

        events_rows = (
            await session.execute(
                select(TraderEvent.event_type, func.count(TraderEvent.id))
                .where(TraderEvent.at >= since_24h)
                .group_by(TraderEvent.event_type),
            )
        ).all()
        events_24h = {str(r[0]): int(r[1]) for r in events_rows}

        last_cycle = (
            await session.execute(
                select(func.max(TraderScore.cycle_at)),
            )
        ).scalar_one_or_none()

    return DiscoveryStatus(
        enabled=enabled,
        active_count=int(counts.get("active", 0)),
        shadow_count=int(counts.get("shadow", 0)),
        paused_count=int(counts.get("paused", 0)),
        pinned_count=int(counts.get("pinned", 0)),
        last_cycle_at=last_cycle,
        promotions_24h=int(events_24h.get("promoted_active", 0)),
        demotions_24h=int(events_24h.get("demoted_paused", 0)),
    )


# Cap UX : 8 milestones max sous le graph PnL (cf. spec §14.5 #12).
_PNL_MILESTONES_CAP: int = 8


async def get_pnl_milestones(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since: timedelta,
) -> list[PnlMilestone]:
    """Extrait les 8 derniers moments clés (fills, kill switches, promotions M5)."""
    cutoff = datetime.now(tz=UTC) - since
    async with session_factory() as session:
        first_fill = (
            await session.execute(
                select(MyOrder)
                .where(MyOrder.status == "FILLED", MyOrder.filled_at.is_not(None))
                .order_by(MyOrder.filled_at.asc())
                .limit(1),
            )
        ).scalar_one_or_none()

        recent_fills = list(
            (
                await session.execute(
                    select(MyOrder)
                    .where(
                        MyOrder.status == "FILLED",
                        MyOrder.filled_at.is_not(None),
                        MyOrder.filled_at >= cutoff,
                    )
                    .order_by(MyOrder.filled_at.desc())
                    .limit(_PNL_MILESTONES_CAP),
                )
            )
            .scalars()
            .all(),
        )

        promotions = list(
            (
                await session.execute(
                    select(TraderEvent)
                    .where(
                        TraderEvent.event_type == "promoted_active",
                        TraderEvent.at >= cutoff,
                    )
                    .order_by(TraderEvent.at.desc())
                    .limit(_PNL_MILESTONES_CAP),
                )
            )
            .scalars()
            .all(),
        )

        kill_switches = list(
            (
                await session.execute(
                    select(TraderEvent)
                    .where(
                        TraderEvent.event_type == "kill_switch",
                        TraderEvent.at >= cutoff,
                    )
                    .order_by(TraderEvent.at.desc())
                    .limit(_PNL_MILESTONES_CAP),
                )
            )
            .scalars()
            .all(),
        )

    milestones: list[PnlMilestone] = []
    if first_fill is not None and first_fill.filled_at is not None:
        milestones.append(
            PnlMilestone(
                at=first_fill.filled_at,
                event_type="first_fill",
                label="Premier fill",
                wallet_address=None,
                market_slug=first_fill.condition_id,
            ),
        )
    for order in recent_fills:
        if order.filled_at is None:
            continue
        if first_fill is not None and order.id == first_fill.id:
            continue
        milestones.append(
            PnlMilestone(
                at=order.filled_at,
                event_type="cycle_completed",
                label=f"Fill {order.side} {order.size:.2f}",
                wallet_address=None,
                market_slug=order.condition_id,
            ),
        )
    for event in promotions:
        milestones.append(
            PnlMilestone(
                at=event.at,
                event_type="trader_promoted",
                label="Trader promu (active)",
                wallet_address=event.wallet_address,
                market_slug=None,
            ),
        )
    for event in kill_switches:
        milestones.append(
            PnlMilestone(
                at=event.at,
                event_type="kill_switch",
                label="Kill switch déclenché",
                wallet_address=event.wallet_address,
                market_slug=None,
            ),
        )

    milestones.sort(key=lambda m: m.at, reverse=True)
    return milestones[:_PNL_MILESTONES_CAP]


# --- M11 /latency queries -----------------------------------------------------


_LATENCY_STAGES_ORDER: tuple[str, ...] = (
    "watcher_detected_ms",
    "strategy_enriched_ms",
    "strategy_filtered_ms",
    "strategy_sized_ms",
    "strategy_risk_checked_ms",
    "executor_submitted_ms",
)


def _percentile(sorted_samples: list[float], p: float) -> float:
    """Percentile naïf (nearest-rank). Retourne ``0.0`` si la liste est vide."""
    if not sorted_samples:
        return 0.0
    idx = min(int(p * len(sorted_samples)), len(sorted_samples) - 1)
    return sorted_samples[idx]


async def compute_latency_percentiles(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since: timedelta,
) -> dict[str, dict[str, float]]:
    """Retourne ``{stage_name: {p50, p95, p99, count}}`` sur la fenêtre ``since``.

    SQLite n'expose pas ``PERCENTILE_CONT`` natif → calcul Python côté client
    (volume ~6 × trades/fenêtre = quelques milliers max, négligeable).
    """
    cutoff = datetime.now(tz=UTC) - since
    async with session_factory() as session:
        stmt = select(
            TradeLatencySample.stage_name,
            TradeLatencySample.duration_ms,
        ).where(TradeLatencySample.timestamp >= cutoff)
        rows = (await session.execute(stmt)).all()
    by_stage: dict[str, list[float]] = {}
    for stage_name, ms in rows:
        by_stage.setdefault(stage_name, []).append(float(ms))
    # Préserve l'ordre logique des 6 stages + ajoute stages surprises in-tail.
    stage_order = list(_LATENCY_STAGES_ORDER)
    for stage in by_stage:
        if stage not in stage_order:
            stage_order.append(stage)
    result: dict[str, dict[str, float]] = {}
    for stage in stage_order:
        samples = sorted(by_stage.get(stage, []))
        result[stage] = {
            "p50": _percentile(samples, 0.50),
            "p95": _percentile(samples, 0.95),
            "p99": _percentile(samples, 0.99),
            "count": float(len(samples)),
        }
    return result


# Cache module-scope du SHA git (1 résolution par process — git n'est pas dans la hot path).
_APP_VERSION_CACHE: str | None = None
_APP_VERSION_LOCK = asyncio.Lock()
_APP_VERSION_FALLBACK: str = "0.6.0-unknown"


async def get_app_version() -> str:
    """Retourne le SHA git court (cached). Fallback ``0.6.0-unknown`` hors d'un repo."""
    global _APP_VERSION_CACHE
    if _APP_VERSION_CACHE is not None:
        return _APP_VERSION_CACHE
    async with _APP_VERSION_LOCK:
        if _APP_VERSION_CACHE is not None:
            return _APP_VERSION_CACHE
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            _APP_VERSION_CACHE = _APP_VERSION_FALLBACK
            return _APP_VERSION_CACHE
        if proc.returncode != 0:
            _APP_VERSION_CACHE = _APP_VERSION_FALLBACK
            return _APP_VERSION_CACHE
        sha = proc.stdout.strip()
        _APP_VERSION_CACHE = f"0.6.0-{sha}" if sha else _APP_VERSION_FALLBACK
        return _APP_VERSION_CACHE


# --- M12 : scoring v2 comparison queries ------------------------------------


@dataclass(frozen=True)
class ScoringComparisonRow:
    """Row pour le tableau v1|v2|delta_rank de ``/traders/scoring``.

    ``score_v1`` / ``score_v2`` = latest per wallet (pas forcément du même
    cycle — peut arriver transitoirement, documenté dans la template).
    ``delta_rank`` = ``rank_v1 - rank_v2`` signed (positif = améliore le rang).
    """

    wallet_address: str
    label: str | None
    status: str
    pinned: bool
    score_v1: float | None
    score_v2: float | None
    rank_v1: int | None
    rank_v2: int | None
    delta_rank: int | None
    last_scored_at: datetime | None


@dataclass(frozen=True)
class ScoringComparisonAggregates:
    """Métriques agrégées pool-wide pour la section header v1|v2.

    Calculées sur les derniers ``latest_per_wallet`` v1 et v2 respectivement.
    Spearman rank(v1, v2) reporté comme ``None`` si moins de 3 wallets avec
    les deux scores (pas de corrélation significative sur petit échantillon).
    """

    wallets_compared: int
    median_delta_rank: float | None
    spearman_rank: float | None
    top10_delta: int  # wallets in v2 top-10 but not v1 top-10
    shadow_days_elapsed: int | None
    shadow_days_remaining: int | None
    cutover_ready: bool


async def list_scoring_comparison(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: int = 100,
) -> list[ScoringComparisonRow]:
    """Liste les wallets avec leurs derniers scores v1 et v2.

    Pour chaque wallet ayant au moins 1 row `trader_scores`, on récupère le
    score v1 et v2 les plus récents (via sous-requêtes `MAX(cycle_at)` par
    version). Le rank est calculé côté Python sur l'échantillon filtré.
    Retourne au plus ``limit`` rows triées ``score_v2 DESC NULLS LAST``.
    """
    limit = _clamp_limit(limit)
    async with session_factory() as session:
        # Latest v1 score per wallet
        latest_v1_subq = (
            select(
                TraderScore.wallet_address,
                func.max(TraderScore.cycle_at).label("max_cycle_at"),
            )
            .where(TraderScore.scoring_version == "v1")
            .group_by(TraderScore.wallet_address)
            .subquery()
        )
        v1_stmt = (
            select(TraderScore)
            .join(
                latest_v1_subq,
                (TraderScore.wallet_address == latest_v1_subq.c.wallet_address)
                & (TraderScore.cycle_at == latest_v1_subq.c.max_cycle_at),
            )
            .where(TraderScore.scoring_version == "v1")
        )
        v1_rows = list((await session.execute(v1_stmt)).scalars().all())

        latest_v2_subq = (
            select(
                TraderScore.wallet_address,
                func.max(TraderScore.cycle_at).label("max_cycle_at"),
            )
            .where(TraderScore.scoring_version == "v2")
            .group_by(TraderScore.wallet_address)
            .subquery()
        )
        v2_stmt = (
            select(TraderScore)
            .join(
                latest_v2_subq,
                (TraderScore.wallet_address == latest_v2_subq.c.wallet_address)
                & (TraderScore.cycle_at == latest_v2_subq.c.max_cycle_at),
            )
            .where(TraderScore.scoring_version == "v2")
        )
        v2_rows = list((await session.execute(v2_stmt)).scalars().all())

        # TargetTrader metadata
        traders_stmt = select(TargetTrader)
        traders = list((await session.execute(traders_stmt)).scalars().all())
        trader_by_wallet = {t.wallet_address: t for t in traders}

    v1_by_wallet = {r.wallet_address: float(r.score) for r in v1_rows}
    v2_by_wallet = {r.wallet_address: float(r.score) for r in v2_rows}

    # Ranks 1-based (1 = meilleur), None si absent.
    v1_ranked = sorted(v1_by_wallet.items(), key=lambda kv: kv[1], reverse=True)
    v2_ranked = sorted(v2_by_wallet.items(), key=lambda kv: kv[1], reverse=True)
    rank_v1 = {w: i + 1 for i, (w, _) in enumerate(v1_ranked)}
    rank_v2 = {w: i + 1 for i, (w, _) in enumerate(v2_ranked)}

    all_wallets = set(v1_by_wallet) | set(v2_by_wallet) | set(trader_by_wallet)
    rows: list[ScoringComparisonRow] = []
    for wallet in all_wallets:
        s1 = v1_by_wallet.get(wallet)
        s2 = v2_by_wallet.get(wallet)
        r1 = rank_v1.get(wallet)
        r2 = rank_v2.get(wallet)
        delta = (r1 - r2) if (r1 is not None and r2 is not None) else None
        t = trader_by_wallet.get(wallet)
        rows.append(
            ScoringComparisonRow(
                wallet_address=wallet,
                label=t.label if t is not None else None,
                status=t.status if t is not None else "absent",
                pinned=bool(t.pinned) if t is not None else False,
                score_v1=s1,
                score_v2=s2,
                rank_v1=r1,
                rank_v2=r2,
                delta_rank=delta,
                last_scored_at=(t.last_scored_at if t is not None else None),
            ),
        )
    # Sort: score_v2 desc (None last), tiebreak score_v1 desc.
    rows.sort(
        key=lambda r: (
            -(r.score_v2 if r.score_v2 is not None else -1),
            -(r.score_v1 if r.score_v1 is not None else -1),
        ),
    )
    return rows[:limit]


async def scoring_comparison_aggregates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    shadow_days: int,
    cutover_ready: bool,
) -> ScoringComparisonAggregates:
    """Agrégats pool-wide pour la section header de ``/traders/scoring``.

    - ``spearman_rank`` : corrélation de rang sur les wallets ayant v1 ET v2.
      ``None`` si < 3 wallets (pas significatif).
    - ``top10_delta`` : nombre de wallets dans le top-10 v2 absents du top-10 v1.
    - ``shadow_days_elapsed`` : depuis la 1ère row v2 (None si pas encore commencé).
    - ``cutover_ready`` : passé-through depuis settings.
    """
    rows = await list_scoring_comparison(session_factory, limit=_MAX_LIMIT)
    with_both = [r for r in rows if r.score_v1 is not None and r.score_v2 is not None]

    median_delta: float | None = None
    spearman: float | None = None
    if len(with_both) >= 1:
        deltas = sorted(r.delta_rank for r in with_both if r.delta_rank is not None)
        if deltas:
            mid = len(deltas) // 2
            median_delta = (
                float(deltas[mid])
                if len(deltas) % 2 == 1
                else (deltas[mid - 1] + deltas[mid]) / 2.0
            )
    if len(with_both) >= 3:
        spearman = _spearman_rank(
            [float(r.rank_v1) for r in with_both if r.rank_v1 is not None],
            [float(r.rank_v2) for r in with_both if r.rank_v2 is not None],
        )

    top10_v1 = {r.wallet_address for r in rows if r.rank_v1 is not None and r.rank_v1 <= 10}
    top10_v2 = {r.wallet_address for r in rows if r.rank_v2 is not None and r.rank_v2 <= 10}
    top10_delta = len(top10_v2 - top10_v1)

    async with session_factory() as session:
        first_v2_cycle = (
            await session.execute(
                select(func.min(TraderScore.cycle_at)).where(
                    TraderScore.scoring_version == "v2",
                ),
            )
        ).scalar_one_or_none()
    shadow_elapsed: int | None = None
    shadow_remaining: int | None = None
    if first_v2_cycle is not None:
        if first_v2_cycle.tzinfo is None:
            first_v2_cycle = first_v2_cycle.replace(tzinfo=UTC)
        shadow_elapsed = (datetime.now(tz=UTC) - first_v2_cycle).days
        shadow_remaining = max(0, shadow_days - shadow_elapsed)

    return ScoringComparisonAggregates(
        wallets_compared=len(with_both),
        median_delta_rank=median_delta,
        spearman_rank=spearman,
        top10_delta=top10_delta,
        shadow_days_elapsed=shadow_elapsed,
        shadow_days_remaining=shadow_remaining,
        cutover_ready=cutover_ready,
    )


def _spearman_rank(ranks_a: list[float], ranks_b: list[float]) -> float | None:
    """Spearman ρ = 1 - (6 * Σd²) / (n * (n² - 1)).

    Pure fonction. Retourne None si n < 3 ou si toutes les valeurs égales.
    """
    n = min(len(ranks_a), len(ranks_b))
    if n < 3:
        return None
    d2_sum = sum((ranks_a[i] - ranks_b[i]) ** 2 for i in range(n))
    denom = n * (n * n - 1)
    if denom == 0:
        return None
    return 1.0 - (6.0 * d2_sum) / denom
