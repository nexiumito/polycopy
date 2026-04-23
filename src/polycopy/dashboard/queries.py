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
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.dashboard.dtos import DiscoveryStatus, KpiCard, PnlMilestone

if TYPE_CHECKING:
    from polycopy.config import Settings
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
    """Ligne pour la page `/traders` (1 ligne = 1 wallet + latest score).

    M5_bis Phase D.2 : ajout de ``previously_demoted_at`` (flag UX
    "re-observation après demote"), ``eviction_state_entered_at``
    (timer wind-down), ``eviction_triggering_wallet`` (audit cascade).
    """

    wallet_address: str
    label: str | None
    status: str
    pinned: bool
    score: float | None
    scoring_version: str | None
    last_scored_at: datetime | None
    discovered_at: datetime | None
    consecutive_low_score_cycles: int
    previously_demoted_at: datetime | None = None
    eviction_state_entered_at: datetime | None = None
    eviction_triggering_wallet: str | None = None


@dataclass(frozen=True)
class PnlSeries:
    """Série temporelle pour Chart.js (x = timestamps ISO UTC)."""

    timestamps: list[datetime]
    total_usdc: list[float]
    drawdown_pct: list[float]


@dataclass(frozen=True)
class HomeAllTimeStats:
    """Stats cumulatives all-time pour la section Home (commit 5).

    Ségrégation explicite dry-run vs live dans ``realized_pnl_total`` :
    - dry-run : ``Σ MyPosition.realized_pnl`` où ``simulated=True`` et
      ``closed_at IS NOT NULL`` (écrit par ``DryRunResolutionWatcher`` M8).
    - live : pour chaque position ``simulated=False, closed_at NOT NULL``,
      somme ``Σ(SELL.size*SELL.price) - Σ(BUY.size*BUY.price)`` sur les
      ``MyOrder FILLED`` du couple ``(condition_id, asset_id)``.
    """

    realized_pnl_total: float
    volume_usd_total: float
    fills_count: int
    fills_rate_pct: float | None
    strategy_approve_rate_pct: float | None
    top_trader: dict[str, float | str | None] | None
    uptime: timedelta | None
    # M13 Bug 6 + PnL latent (defaults non-cassants pour les tests existants).
    open_exposition_usd: float = 0.0
    open_max_profit_usd: float = 0.0
    open_latent_pnl_usd: float = 0.0
    win_rate_pct: float | None = None


@dataclass(frozen=True)
class TraderPerformanceRow:
    """Ligne trader-leaderboard pour ``/performance`` (commit 7).

    Inclut toutes les positions tracées (ouvertes + fermées) agrégées par
    trader source. Les ``active``, ``shadow``, ``sell_only``, ``blacklisted``
    et ``pinned`` sont tous inclus (filter-chips côté UI). Un trader sans
    aucune position tracée est **exclu** du leaderboard (``realized_pnl`` non
    significatif).
    """

    wallet_address: str
    label: str | None
    status: str
    pinned: bool
    score_v1: float | None
    positions_closed_count: int
    positions_open_count: int
    win_count: int
    loss_count: int
    win_rate_pct: float | None
    realized_pnl_total: float
    last_trade_at: datetime | None


@dataclass(frozen=True)
class ActivityRow:
    """Ligne d'historique PnL pour ``/activity`` (commit 6).

    Une ligne = une position fermée (``closed_at IS NOT NULL``).
    ``avg_sell_price`` est ``None`` quand la position a été résolue par
    ``DryRunResolutionWatcher`` M8 (aucun fill SELL émis) ; dans ce cas,
    ``realized_pnl`` vient de ``MyPosition.realized_pnl`` directement.
    """

    position_id: int
    closed_at: datetime
    opened_at: datetime
    condition_id: str
    outcome_label: str | None
    source_trader_wallet: str | None
    source_trader_label: str | None
    size: float
    avg_buy_price: float
    avg_sell_price: float | None
    realized_pnl: float | None
    holding_duration: timedelta
    simulated: bool


@dataclass(frozen=True)
class PositionRow:
    """Ligne enrichie pour la page ``/positions`` (commit 4).

    ``usdc_invested = size * avg_price`` ; ``payoff_max = size * 1.0``
    (outcome tokens CLOB paient $1 au gagnant) ; ``potential_profit =
    payoff_max - usdc_invested`` (gain net si l'outcome gagne). Sur une
    position proche du bord (ex. cote 0.99), le payoff brut est trompeur
    — le gain net peut être minuscule. ``outcome_label`` est joint depuis
    ``DetectedTrade.outcome`` sur ``(condition_id, asset_id)``, ``None`` si
    aucun trade source n'a persisté un outcome lisible.
    """

    id: int
    condition_id: str
    asset_id: str
    size: float
    avg_price: float
    usdc_invested: float
    payoff_max: float
    potential_profit: float
    outcome_label: str | None
    opened_at: datetime
    closed_at: datetime | None
    simulated: bool
    realized_pnl: float | None


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
        discovery_demotions_24h=int(events_24h.get("demoted_paused", 0))
        + int(events_24h.get("demoted_to_shadow", 0)),
        discovery_shadow_count=int(status_counts.get("shadow", 0)),
        discovery_active_count=int(status_counts.get("active", 0))
        + int(status_counts.get("pinned", 0)),
        discovery_paused_count=int(status_counts.get("paused", 0))
        + int(status_counts.get("sell_only", 0)),
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
) -> list[PositionRow]:
    """Liste les positions enrichies (``opened_at`` desc) + ``outcome_label``.

    ``outcome_label`` vient du premier ``DetectedTrade`` joignable sur
    ``(condition_id, asset_id)`` avec ``outcome IS NOT NULL``. Résultat
    stable en ordonnant par ``tx_hash`` croissant côté sous-requête.
    """
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    async with session_factory() as session:
        stmt = select(MyPosition).order_by(MyPosition.opened_at.desc())
        if state == "open":
            stmt = stmt.where(MyPosition.closed_at.is_(None))
        elif state == "closed":
            stmt = stmt.where(MyPosition.closed_at.is_not(None))
        stmt = stmt.limit(limit).offset(offset)
        positions = list((await session.execute(stmt)).scalars().all())

        # Résout ``outcome`` via une sous-requête minimale (plutôt qu'une
        # jointure : la cardinalité 1-N côté DetectedTrade varie beaucoup par
        # position, un lookup ciblé par (condition_id, asset_id) est plus
        # prévisible côté SQLite).
        outcome_by_key: dict[tuple[str, str], str] = {}
        if positions:
            keys = {(p.condition_id, p.asset_id) for p in positions}
            for cond_id, asset_id in keys:
                outcome = (
                    await session.execute(
                        select(DetectedTrade.outcome)
                        .where(
                            DetectedTrade.condition_id == cond_id,
                            DetectedTrade.asset_id == asset_id,
                            DetectedTrade.outcome.is_not(None),
                        )
                        .order_by(DetectedTrade.tx_hash.asc())
                        .limit(1),
                    )
                ).scalar_one_or_none()
                if outcome is not None:
                    outcome_by_key[(cond_id, asset_id)] = outcome

    rows_out: list[PositionRow] = []
    for p in positions:
        size_f = float(p.size)
        invested = size_f * float(p.avg_price)
        payoff = size_f * 1.0
        rows_out.append(
            PositionRow(
                id=p.id,
                condition_id=p.condition_id,
                asset_id=p.asset_id,
                size=size_f,
                avg_price=float(p.avg_price),
                usdc_invested=invested,
                payoff_max=payoff,
                potential_profit=payoff - invested,
                outcome_label=outcome_by_key.get((p.condition_id, p.asset_id)),
                opened_at=p.opened_at,
                closed_at=p.closed_at,
                simulated=bool(p.simulated),
                realized_pnl=(float(p.realized_pnl) if p.realized_pnl is not None else None),
            ),
        )
    return rows_out


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


_VALID_TRADER_STATUSES = frozenset(
    {
        "shadow",
        "active",
        "paused",  # deprecated M5_bis Phase A, retiré runtime Phase C
        "pinned",
        "sell_only",  # M5_bis : filtre dashboard wind-down
        "blacklisted",  # M5_bis : filtre dashboard exclusions
    },
)


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
            previously_demoted_at=t.previously_demoted_at,
            eviction_state_entered_at=t.eviction_state_entered_at,
            eviction_triggering_wallet=t.eviction_triggering_wallet,
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


_VALID_HOME_PNL_MODES = frozenset({"real", "dry_run", "both"})


def normalize_home_pnl_mode(raw: str | None) -> Literal["real", "dry_run", "both"]:
    """Normalise ``?pnl_mode=`` (``real``/``dry_run``/``both``). Fallback ``both``.

    Cohérent avec le filtre côté ``/pnl`` (même trio de tokens).
    """
    if raw is not None and raw in _VALID_HOME_PNL_MODES:
        return raw  # type: ignore[return-value]
    return "both"


async def get_home_alltime_stats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pnl_mode: Literal["real", "dry_run", "both"] = "both",
    settings: Settings | None = None,
) -> HomeAllTimeStats:
    """Agrège 6 stats all-time pour la section Home (commit 5).

    Une seule session courte, requêtes en cascade. Le PnL réalisé live est
    calculé via un LEFT JOIN entre les positions closed ``simulated=False`` et
    les fills ``MyOrder FILLED`` de même ``(condition_id, asset_id)``. Le
    dry-run lit directement ``MyPosition.realized_pnl`` (écrit par
    ``DryRunResolutionWatcher``).

    ``pnl_mode`` filtre ``realized_pnl_total``, ``volume_usd_total``,
    ``fills_count`` et ``fills_rate_pct`` de manière cohérente (bug 2 fix) :
    ``real`` = ordres ``FILLED``, ``dry_run`` = ordres ``SIMULATED``,
    ``both`` = les deux. Les autres champs (``strategy_approve_rate``,
    ``top_trader``, ``uptime``) restent mode-agnostiques.
    """
    async with session_factory() as session:
        # --- PnL réalisé dry-run : somme simple de la colonne dénormalisée.
        dry_run_pnl_raw = (
            await session.execute(
                select(func.coalesce(func.sum(MyPosition.realized_pnl), 0.0)).where(
                    MyPosition.simulated.is_(True),
                    MyPosition.closed_at.is_not(None),
                ),
            )
        ).scalar_one()
        dry_run_pnl = float(dry_run_pnl_raw) if dry_run_pnl_raw is not None else 0.0

        # --- PnL réalisé live : Σ(SELL fills) - Σ(BUY fills) sur positions
        # fermées non-simulées. Calcul en mémoire (volume typique < quelques
        # centaines de positions, SQLite aurait besoin de plusieurs CTE sinon).
        live_closed = list(
            (
                await session.execute(
                    select(MyPosition.condition_id, MyPosition.asset_id).where(
                        MyPosition.simulated.is_(False),
                        MyPosition.closed_at.is_not(None),
                    ),
                )
            ).all(),
        )
        live_pnl = 0.0
        for cond_id, asset_id in live_closed:
            fills = list(
                (
                    await session.execute(
                        select(MyOrder.side, MyOrder.size, MyOrder.price).where(
                            MyOrder.condition_id == cond_id,
                            MyOrder.asset_id == asset_id,
                            MyOrder.status == "FILLED",
                        ),
                    )
                ).all(),
            )
            for side, size, price in fills:
                signed = float(size) * float(price)
                live_pnl += signed if side == "SELL" else -signed

        if pnl_mode == "real":
            realized_pnl_total = live_pnl
        elif pnl_mode == "dry_run":
            realized_pnl_total = dry_run_pnl
        else:
            realized_pnl_total = dry_run_pnl + live_pnl

        # --- Volume USD total : Σ size*price sur MyOrder FILLED/SIMULATED
        # selon le mode. En dry-run, seuls les ordres SIMULATED existent ;
        # on veut pouvoir les visualiser pendant un test 14 jours même s'ils
        # sont virtuels (bug 2 fix).
        fill_statuses: list[str]
        if pnl_mode == "real":
            fill_statuses = ["FILLED"]
        elif pnl_mode == "dry_run":
            fill_statuses = ["SIMULATED"]
        else:
            fill_statuses = ["FILLED", "SIMULATED"]

        volume_usd_total = float(
            (
                await session.execute(
                    select(
                        func.coalesce(func.sum(MyOrder.size * MyOrder.price), 0.0),
                    ).where(MyOrder.status.in_(fill_statuses)),
                )
            ).scalar_one(),
        )

        # --- Comptes FILLED / SIMULATED / REJECTED / FAILED, filtrés par mode.
        order_status_rows = list(
            (
                await session.execute(
                    select(MyOrder.status, func.count(MyOrder.id)).group_by(MyOrder.status),
                )
            ).all(),
        )
        order_counts = {str(s): int(n) for s, n in order_status_rows}
        fills_count = sum(order_counts.get(s, 0) for s in fill_statuses)
        exec_total = fills_count + order_counts.get("REJECTED", 0) + order_counts.get("FAILED", 0)
        fills_rate_pct: float | None = (
            (fills_count / exec_total * 100.0) if exec_total > 0 else None
        )

        # --- Ratio approve/reject strategy.
        decision_rows = list(
            (
                await session.execute(
                    select(StrategyDecision.decision, func.count(StrategyDecision.id)).group_by(
                        StrategyDecision.decision,
                    ),
                )
            ).all(),
        )
        decision_counts = {str(d): int(n) for d, n in decision_rows}
        total_decisions = sum(decision_counts.values())
        strategy_approve_rate_pct: float | None = (
            (decision_counts.get("APPROVED", 0) / total_decisions * 100.0)
            if total_decisions > 0
            else None
        )

        # --- Meilleur trader actif (par score DESC).
        top_trader_row = (
            await session.execute(
                select(TargetTrader)
                .where(
                    TargetTrader.status == "active",
                    TargetTrader.score.is_not(None),
                )
                .order_by(TargetTrader.score.desc())
                .limit(1),
            )
        ).scalar_one_or_none()
        top_trader: dict[str, float | str | None] | None = None
        if top_trader_row is not None:
            top_trader = {
                "wallet_address": top_trader_row.wallet_address,
                "label": top_trader_row.label,
                "score": (
                    float(top_trader_row.score) if top_trader_row.score is not None else None
                ),
            }

        # --- Uptime approximé = now - timestamp min de PnlSnapshot.
        first_snapshot_ts = (
            await session.execute(select(func.min(PnlSnapshot.timestamp)))
        ).scalar_one_or_none()

        # --- M13 Bug 6 : exposition + gain max latent sur positions OUVERTES.
        open_filter = [MyPosition.closed_at.is_(None)]
        if pnl_mode == "real":
            open_filter.append(MyPosition.simulated.is_(False))
        elif pnl_mode == "dry_run":
            open_filter.append(MyPosition.simulated.is_(True))
        open_stats = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(MyPosition.size * MyPosition.avg_price),
                        0.0,
                    ).label("exposition"),
                    func.coalesce(
                        func.sum(MyPosition.size * (1.0 - MyPosition.avg_price)),
                        0.0,
                    ).label("max_profit"),
                ).where(*open_filter),
            )
        ).first()
        open_exposition_usd = float(open_stats.exposition) if open_stats else 0.0
        open_max_profit_usd = float(open_stats.max_profit) if open_stats else 0.0

        # --- M13 Bug 6 : win rate sur positions fermées avec PnL cristallisé.
        closed_filter = [
            MyPosition.closed_at.is_not(None),
            MyPosition.realized_pnl.is_not(None),
        ]
        if pnl_mode == "real":
            closed_filter.append(MyPosition.simulated.is_(False))
        elif pnl_mode == "dry_run":
            closed_filter.append(MyPosition.simulated.is_(True))
        closed_pnls = (
            (
                await session.execute(
                    select(MyPosition.realized_pnl).where(*closed_filter),
                )
            )
            .scalars()
            .all()
        )
        wins = sum(1 for p in closed_pnls if p is not None and float(p) > 0)
        losses = sum(1 for p in closed_pnls if p is not None and float(p) < 0)
        decided = wins + losses
        win_rate_pct: float | None = (wins / decided * 100.0) if decided > 0 else None

        # --- M13 PnL latent : total_usdc courant − initial_capital − realized_pnl.
        # ``settings is None`` = caller n'a pas besoin du latent (compat tests
        # unitaires qui n'injectent pas Settings) → 0.0.
        open_latent_pnl_usd = 0.0
        if settings is not None:
            latest_snapshot_total = (
                await session.execute(
                    select(PnlSnapshot.total_usdc).order_by(PnlSnapshot.timestamp.desc()).limit(1),
                )
            ).scalar_one_or_none()
            if latest_snapshot_total is not None:
                if settings.dry_run_initial_capital_usd is not None:
                    initial_capital = float(settings.dry_run_initial_capital_usd)
                else:
                    oldest = (
                        await session.execute(
                            select(PnlSnapshot.total_usdc)
                            .order_by(PnlSnapshot.timestamp.asc())
                            .limit(1),
                        )
                    ).scalar_one_or_none()
                    initial_capital = (
                        float(oldest)
                        if oldest is not None
                        else float(settings.risk_available_capital_usd_stub)
                    )
                open_latent_pnl_usd = (
                    float(latest_snapshot_total) - initial_capital - realized_pnl_total
                )

    uptime: timedelta | None = None
    if first_snapshot_ts is not None:
        if first_snapshot_ts.tzinfo is None:
            first_snapshot_ts = first_snapshot_ts.replace(tzinfo=UTC)
        uptime = datetime.now(tz=UTC) - first_snapshot_ts

    return HomeAllTimeStats(
        realized_pnl_total=realized_pnl_total,
        volume_usd_total=volume_usd_total,
        fills_count=fills_count,
        fills_rate_pct=fills_rate_pct,
        strategy_approve_rate_pct=strategy_approve_rate_pct,
        top_trader=top_trader,
        uptime=uptime,
        open_exposition_usd=open_exposition_usd,
        open_max_profit_usd=open_max_profit_usd,
        open_latent_pnl_usd=open_latent_pnl_usd,
        win_rate_pct=win_rate_pct,
    )


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
        demotions_24h=int(events_24h.get("demoted_paused", 0))
        + int(events_24h.get("demoted_to_shadow", 0)),
    )


# Cap UX : 8 milestones max sous le graph PnL (cf. spec §14.5 #12).
_PNL_MILESTONES_CAP: int = 8


async def get_pnl_milestones(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since: timedelta,
    mode: Literal["real", "dry_run", "both"] = "real",
) -> list[PnlMilestone]:
    """Extrait les 8 derniers moments clés (fills, kill switches, promotions M5).

    Le paramètre ``mode`` détermine quel statut d'ordre compte comme "fill" :
    ``real`` → ``FILLED`` (``filled_at`` requis), ``dry_run`` → ``SIMULATED``
    (``sent_at`` utilisé — les ordres simulés M8 n'écrivent pas ``filled_at``),
    ``both`` → les deux. Cohérent avec le toggle mode de ``/pnl`` (bug 2 fix
    : les milestones deviennent visibles en dry-run).
    """
    cutoff = datetime.now(tz=UTC) - since
    # COALESCE(filled_at, sent_at) : timestamp effectif du fill, en dry-run
    # seul ``sent_at`` est peuplé (insert_realistic_simulated ne set pas
    # filled_at) ; en live, ``filled_at`` écrase ``sent_at``.
    effective_at = func.coalesce(MyOrder.filled_at, MyOrder.sent_at)
    if mode == "real":
        status_clause = MyOrder.status == "FILLED"
    elif mode == "dry_run":
        status_clause = MyOrder.status == "SIMULATED"
    else:
        status_clause = MyOrder.status.in_(["FILLED", "SIMULATED"])
    async with session_factory() as session:
        first_fill = (
            await session.execute(
                select(MyOrder).where(status_clause).order_by(effective_at.asc()).limit(1),
            )
        ).scalar_one_or_none()

        recent_fills = list(
            (
                await session.execute(
                    select(MyOrder)
                    .where(
                        status_clause,
                        effective_at >= cutoff,
                    )
                    .order_by(effective_at.desc())
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
    if first_fill is not None:
        # Même logique COALESCE que le SQL : en dry-run ``filled_at`` est None
        # mais ``sent_at`` est toujours peuplé (default _now_utc à l'insert).
        first_at = first_fill.filled_at or first_fill.sent_at
        milestones.append(
            PnlMilestone(
                at=first_at,
                event_type="first_fill",
                label="Premier fill",
                wallet_address=None,
                market_slug=first_fill.condition_id,
            ),
        )
    for order in recent_fills:
        if first_fill is not None and order.id == first_fill.id:
            continue
        order_at = order.filled_at or order.sent_at
        milestones.append(
            PnlMilestone(
                at=order_at,
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
        # Spearman calculé sur les ranks **locaux à l'intersection v1∩v2**, pas
        # sur les ranks pool-wide affichés dans le tableau. Sans ça, un pool v1
        # beaucoup plus large que v2 biaise mécaniquement la corrélation : un
        # wallet 29e sur 34 en v1 vs 1er sur 10 en v2 donne d²=784 alors qu'il
        # serait peut-être 8e sur 10 vs 1er sur 10 (d²=49) sur l'intersection.
        # Les rank_v1/rank_v2 des ``ScoringComparisonRow`` restent pool-wide
        # côté UI — seul le calcul de ρ utilise les ranks locaux.
        v1_sorted = sorted(with_both, key=lambda r: r.score_v1 or 0.0, reverse=True)
        local_rank_v1 = {r.wallet_address: i + 1 for i, r in enumerate(v1_sorted)}
        v2_sorted = sorted(with_both, key=lambda r: r.score_v2 or 0.0, reverse=True)
        local_rank_v2 = {r.wallet_address: i + 1 for i, r in enumerate(v2_sorted)}
        spearman = _spearman_rank(
            [float(local_rank_v1[r.wallet_address]) for r in with_both],
            [float(local_rank_v2[r.wallet_address]) for r in with_both],
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


# --- M6 commit 6 : onglet /activity (historique des PnL réalisés) -----------


async def list_activity_closed_positions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[ActivityRow]:
    """Historique des positions fermées avec PnL réalisé + trader source.

    Pour chaque position ``closed_at IS NOT NULL`` on résout :

    - ``avg_buy_price`` et ``avg_sell_price`` via moyenne pondérée des
      ``MyOrder FILLED`` sur ``(condition_id, asset_id)``, ventilés par
      ``side``. ``avg_sell_price`` est ``None`` si aucun SELL FILLED (cas
      M8 : résolution par ``DryRunResolutionWatcher``).
    - ``realized_pnl`` : priorité à ``MyPosition.realized_pnl`` quand non-null
      (dry-run résolu) ; sinon ``Σ(SELL.size*SELL.price) − Σ(BUY.size*BUY.price)``
      (path live ou dry-run encore non-résolu).
    - ``source_trader_wallet`` / ``source_trader_label`` : remonte le premier
      ``MyOrder BUY`` → ``source_tx_hash`` → ``DetectedTrade.target_wallet``
      → ``TargetTrader.label``. ``None`` si la chaîne est cassée.
    - ``outcome_label`` : ``DetectedTrade.outcome`` sur la même ``source_tx_hash``.

    Ordre : ``closed_at DESC``.
    """
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    async with session_factory() as session:
        positions = list(
            (
                await session.execute(
                    select(MyPosition)
                    .where(MyPosition.closed_at.is_not(None))
                    .order_by(MyPosition.closed_at.desc())
                    .limit(limit)
                    .offset(offset),
                )
            )
            .scalars()
            .all(),
        )
        if not positions:
            return []

        # --- Agrégats BUY / SELL FILLED|SIMULATED par (condition_id, asset_id).
        # Bug 3 fix : en dry-run les ordres sont SIMULATED — sans ce filtre,
        # /activity affichait des lignes avec avg_buy/sell_price à None.
        keys = [(p.condition_id, p.asset_id) for p in positions]
        fill_stats: dict[tuple[str, str], dict[str, float]] = {}
        for cond_id, asset_id in set(keys):
            rows = list(
                (
                    await session.execute(
                        select(MyOrder.side, MyOrder.size, MyOrder.price).where(
                            MyOrder.condition_id == cond_id,
                            MyOrder.asset_id == asset_id,
                            MyOrder.status.in_(["FILLED", "SIMULATED"]),
                        ),
                    )
                ).all(),
            )
            buy_size = 0.0
            buy_cost = 0.0
            sell_size = 0.0
            sell_recovery = 0.0
            for side, size, price in rows:
                s_val = float(size)
                p_val = float(price)
                if side == "BUY":
                    buy_size += s_val
                    buy_cost += s_val * p_val
                elif side == "SELL":
                    sell_size += s_val
                    sell_recovery += s_val * p_val
            fill_stats[(cond_id, asset_id)] = {
                "buy_size": buy_size,
                "buy_cost": buy_cost,
                "sell_size": sell_size,
                "sell_recovery": sell_recovery,
            }

        # --- Première BUY order source → source_tx_hash → DetectedTrade.
        source_info: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        for cond_id, asset_id in set(keys):
            first_buy = (
                await session.execute(
                    select(MyOrder.source_tx_hash)
                    .where(
                        MyOrder.condition_id == cond_id,
                        MyOrder.asset_id == asset_id,
                        MyOrder.side == "BUY",
                    )
                    .order_by(MyOrder.sent_at.asc())
                    .limit(1),
                )
            ).scalar_one_or_none()
            wallet: str | None = None
            outcome: str | None = None
            if first_buy is not None:
                detected = (
                    await session.execute(
                        select(DetectedTrade.target_wallet, DetectedTrade.outcome).where(
                            DetectedTrade.tx_hash == first_buy,
                        ),
                    )
                ).first()
                if detected is not None:
                    wallet, outcome = detected
            source_info[(cond_id, asset_id)] = (wallet, outcome)

        # --- TargetTrader labels par wallet.
        wallets_needed = {w for (w, _) in source_info.values() if w is not None}
        trader_labels: dict[str, str | None] = {}
        if wallets_needed:
            trader_rows = list(
                (
                    await session.execute(
                        select(TargetTrader.wallet_address, TargetTrader.label).where(
                            TargetTrader.wallet_address.in_(wallets_needed),
                        ),
                    )
                ).all(),
            )
            trader_labels = {str(addr): label for addr, label in trader_rows}

    rows_out: list[ActivityRow] = []
    for p in positions:
        stats = fill_stats.get((p.condition_id, p.asset_id), {})
        buy_size = stats.get("buy_size", 0.0)
        buy_cost = stats.get("buy_cost", 0.0)
        sell_size = stats.get("sell_size", 0.0)
        sell_recovery = stats.get("sell_recovery", 0.0)
        avg_buy = (buy_cost / buy_size) if buy_size > 0 else float(p.avg_price)
        avg_sell: float | None = (sell_recovery / sell_size) if sell_size > 0 else None

        # Priorité : realized_pnl dénormalisé (dry-run résolu) > calcul fills.
        if p.realized_pnl is not None:
            realized_pnl: float | None = float(p.realized_pnl)
        elif sell_size > 0 or buy_size > 0:
            realized_pnl = sell_recovery - buy_cost
        else:
            realized_pnl = None

        wallet, outcome = source_info.get((p.condition_id, p.asset_id), (None, None))
        holding_duration = (p.closed_at or datetime.now(tz=UTC)) - p.opened_at

        rows_out.append(
            ActivityRow(
                position_id=p.id,
                closed_at=p.closed_at or datetime.now(tz=UTC),
                opened_at=p.opened_at,
                condition_id=p.condition_id,
                outcome_label=outcome,
                source_trader_wallet=wallet,
                source_trader_label=(trader_labels.get(wallet) if wallet else None),
                size=float(p.size),
                avg_buy_price=avg_buy,
                avg_sell_price=avg_sell,
                realized_pnl=realized_pnl,
                holding_duration=holding_duration,
                simulated=bool(p.simulated),
            ),
        )
    return rows_out


# --- M6 commit 7 : onglet /performance (traders leaderboard par PnL net) -----


_PERFORMANCE_STATUSES = frozenset(
    {"active", "shadow", "sell_only", "blacklisted", "pinned"},
)


async def list_trader_performance(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[TraderPerformanceRow]:
    """Leaderboard traders agrégé par PnL réalisé total.

    Scope : tous traders avec au moins 1 position tracée (chaîne
    ``MyOrder.source_tx_hash`` → ``DetectedTrade.tx_hash`` →
    ``DetectedTrade.target_wallet``). Les traders sans aucune position dans
    cette chaîne sont exclus du leaderboard. Filter ``status`` optionnel
    (``active`` / ``shadow`` / ``sell_only`` / ``blacklisted`` / ``pinned``).

    Agrégat PnL réalisé par position : priorité ``MyPosition.realized_pnl``
    (dry-run résolu M8) > calcul ``Σ SELL − Σ BUY`` sur fills FILLED.
    ``win_rate_pct = win_count / (win_count + loss_count)`` sur positions
    **fermées** uniquement ; ``None`` si aucune position fermée.

    Ordre : ``realized_pnl_total DESC``.
    """
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    async with session_factory() as session:
        traders = list((await session.execute(select(TargetTrader))).scalars().all())

        # --- Chaîne wallet → positions tracées (via premier BUY order).
        # Pour chaque position on résout son wallet source via
        # MyPosition → premier MyOrder BUY sur (condition_id, asset_id)
        # → DetectedTrade.tx_hash = source_tx_hash → target_wallet.
        positions = list((await session.execute(select(MyPosition))).scalars().all())
        if not positions:
            return []

        position_wallet: dict[int, str | None] = {}
        position_fills: dict[int, dict[str, float]] = {}
        for p in positions:
            first_buy = (
                await session.execute(
                    select(MyOrder.source_tx_hash)
                    .where(
                        MyOrder.condition_id == p.condition_id,
                        MyOrder.asset_id == p.asset_id,
                        MyOrder.side == "BUY",
                    )
                    .order_by(MyOrder.sent_at.asc())
                    .limit(1),
                )
            ).scalar_one_or_none()
            wallet: str | None = None
            if first_buy is not None:
                detected_wallet = (
                    await session.execute(
                        select(DetectedTrade.target_wallet).where(
                            DetectedTrade.tx_hash == first_buy,
                        ),
                    )
                ).scalar_one_or_none()
                wallet = detected_wallet
            position_wallet[p.id] = wallet

            # Fills (pour calcul PnL live + fallback dry-run). Bug 3 fix :
            # inclure SIMULATED pour que le leaderboard dry-run remonte bien
            # les winrate / PnL total. Pour les positions virtuelles closes
            # par SELL (Bug 1 fix), p.realized_pnl prend la main ligne ~1705 ;
            # ce fallback sert pour les positions ouvertes ou les résolutions
            # M8 (close_virtual) qui n'a pas de fills associés côté MyOrder.
            fills = list(
                (
                    await session.execute(
                        select(MyOrder.side, MyOrder.size, MyOrder.price).where(
                            MyOrder.condition_id == p.condition_id,
                            MyOrder.asset_id == p.asset_id,
                            MyOrder.status.in_(["FILLED", "SIMULATED"]),
                        ),
                    )
                ).all(),
            )
            buy_size = 0.0
            buy_cost = 0.0
            sell_size = 0.0
            sell_recovery = 0.0
            for side, size, price in fills:
                s_val = float(size)
                p_val = float(price)
                if side == "BUY":
                    buy_size += s_val
                    buy_cost += s_val * p_val
                elif side == "SELL":
                    sell_size += s_val
                    sell_recovery += s_val * p_val
            position_fills[p.id] = {
                "buy_size": buy_size,
                "buy_cost": buy_cost,
                "sell_size": sell_size,
                "sell_recovery": sell_recovery,
            }

        # --- Dernier trade par wallet (via DetectedTrade.timestamp).
        last_trade_rows = list(
            (
                await session.execute(
                    select(
                        DetectedTrade.target_wallet,
                        func.max(DetectedTrade.timestamp),
                    ).group_by(DetectedTrade.target_wallet),
                )
            ).all(),
        )
        last_trade_by_wallet: dict[str, datetime] = {}
        for w, ts in last_trade_rows:
            if ts is None:
                continue
            # SQLite renvoie parfois des datetimes naives même si persistées
            # avec tz=UTC — on normalise pour éviter compare naive/aware.
            normalized = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
            last_trade_by_wallet[str(w)] = normalized

    # --- Agrégation Python (in-memory, volume typique ≪ 500 traders × 20 pos).
    @dataclass
    class _Agg:
        realized_pnl_total: float = 0.0
        positions_closed: int = 0
        positions_open: int = 0
        wins: int = 0
        losses: int = 0

    aggregates: dict[str, _Agg] = {}
    for p in positions:
        wallet = position_wallet[p.id]
        if wallet is None:
            continue
        agg = aggregates.setdefault(wallet, _Agg())
        stats = position_fills[p.id]
        if p.realized_pnl is not None:
            pnl: float | None = float(p.realized_pnl)
        elif stats["buy_size"] > 0 or stats["sell_size"] > 0:
            pnl = stats["sell_recovery"] - stats["buy_cost"]
        else:
            pnl = None
        if p.closed_at is not None:
            agg.positions_closed += 1
            if pnl is not None:
                agg.realized_pnl_total += pnl
                if pnl > 0:
                    agg.wins += 1
                elif pnl < 0:
                    agg.losses += 1
        else:
            agg.positions_open += 1

    rows: list[TraderPerformanceRow] = []
    traders_by_wallet = {t.wallet_address: t for t in traders}
    for wallet, agg in aggregates.items():
        trader = traders_by_wallet.get(wallet)
        if trader is None:
            continue
        if status is not None and status in _PERFORMANCE_STATUSES and trader.status != status:
            continue
        decided_closed = agg.wins + agg.losses
        win_rate = (agg.wins / decided_closed * 100.0) if decided_closed > 0 else None
        rows.append(
            TraderPerformanceRow(
                wallet_address=wallet,
                label=trader.label,
                status=trader.status,
                pinned=bool(trader.pinned),
                score_v1=float(trader.score) if trader.score is not None else None,
                positions_closed_count=agg.positions_closed,
                positions_open_count=agg.positions_open,
                win_count=agg.wins,
                loss_count=agg.losses,
                win_rate_pct=win_rate,
                realized_pnl_total=agg.realized_pnl_total,
                last_trade_at=last_trade_by_wallet.get(wallet),
            ),
        )

    rows.sort(key=lambda r: r.realized_pnl_total, reverse=True)
    return rows[offset : offset + limit]
