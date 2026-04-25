"""Collecteur M12 ``TraderMetricsV2`` — extension composée du :class:`MetricsCollector` M5.

Fetch en plus de M5 :

- ``/positions`` → calcul ``brier_90d`` (sur positions résolues) + ``zombie_ratio``
  (Gemini §1.1, positions < 2% initial_value non liquidées, excluant < 30j).
- ``/activity`` → ``cash_pnl_90d``, ``trade_count_90d``, ``days_active``,
  ``sizing_cv``, ``hhi_categories`` (via :class:`MarketCategoryResolver`).
- :class:`TraderDailyPnlRepository` → ``monthly_equity_curve`` +
  ``sortino_90d`` + ``calmar_90d`` + ``monthly_pnl_positive_ratio``.

**v1 M12 `timing_alpha`** : valeur neutre ``0.5`` pour tous les wallets (cf.
décision pragmatique docs/logbook_module/m12_notes.md 2026-04-18). Implémentation
complète (reconstruction ``mid_price(t)`` via `/activity?market=...`) reportée
v2.1 — coût API prohibitif en v1 (§14.5 spec).

Composition préférée à l'héritage : :class:`MetricsCollectorV2` délègue à un
:class:`MetricsCollector` M5 pour les mesures de base (``win_rate``, ``ROI``,
``HHI markets``, ``volume``) qu'il expose via ``TraderMetricsV2.base``. Zéro
ligne modifiée dans le collector M5.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from statistics import mean, pstdev
from typing import TYPE_CHECKING, Any

import structlog

from polycopy.discovery.dtos import RawPosition
from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.discovery.data_api_client import DiscoveryDataApiClient
    from polycopy.discovery.metrics_collector import MetricsCollector
    from polycopy.discovery.scoring.v2.category_resolver import MarketCategoryResolver
    from polycopy.storage.models import TraderDailyPnl
    from polycopy.storage.repositories import TraderDailyPnlRepository


log = structlog.get_logger(__name__)


# Neutre v1 M12 (voir module docstring + m12_notes 2026-04-18).
_TIMING_ALPHA_NEUTRAL: float = 0.5

# Zombie ratio : seuil de "mort" = current_value / initial_value < 2%, sur
# position non résolue (jamais liquidée). Positions ouvertes depuis < 30j
# exclues du dénominateur pour ne pas pénaliser les wallets récents (§3.7).
_ZOMBIE_CURRENT_VALUE_PCT: float = 0.02
_ZOMBIE_MIN_AGE_DAYS: int = 30

# Brier : minimum de positions résolues pour un calcul non bruité.
_BRIER_MIN_RESOLVED: int = 5

# Calcul "jour actif" : on considère actif tout jour avec ≥ 1 trade dans
# /activity. days_active = nombre de jours distincts observés.


class MetricsCollectorV2:
    """Étend :class:`MetricsCollector` M5 avec les 12 mesures v2.

    Composition : délègue à ``base_collector`` pour les mesures M5 qui restent
    identiques (win_rate, ROI, HHI markets, volume). Ajoute en propre les
    12 mesures v2 (Sortino / Calmar / Brier / timing / HHI catégories /
    consistency / discipline + 3 gates).
    """

    def __init__(
        self,
        base_collector: MetricsCollector,
        daily_pnl_repo: TraderDailyPnlRepository,
        data_api: DiscoveryDataApiClient,
        category_resolver: MarketCategoryResolver,
        settings: Settings,
    ) -> None:
        self._base = base_collector
        self._daily_pnl_repo = daily_pnl_repo
        self._data_api = data_api
        self._category_resolver = category_resolver
        self._settings = settings

    async def collect(self, wallet_address: str) -> TraderMetricsV2:
        """Agrège l'ensemble des metrics v2 pour 1 wallet."""
        window_days = self._settings.scoring_v2_window_days
        since = datetime.now(tz=UTC) - timedelta(days=window_days)

        # Base M5 (réutilise le fetch /positions + /activity du collector M5).
        base_metrics = await self._base.collect(wallet_address)

        # Re-fetch /positions raw pour zombie_ratio + brier (on n'a pas les
        # raw positions dans TraderMetrics M5).
        positions = await self._data_api.get_positions(wallet_address)
        activity = await self._data_api.get_activity_trades(wallet_address, since=since)

        # Equity curve depuis la DB locale (peuplée par TraderDailyPnlWriter).
        curve_rows = await self._daily_pnl_repo.get_curve(
            wallet_address,
            days=window_days,
        )
        equity_curve = [float(r.equity_usdc) for r in curve_rows]

        # Sortino / Calmar — délégués au factor risk_adjusted via equity_curve.
        # Ici on calcule juste les raw stats utiles au Brier / consistency
        # (le factor fera le reste à partir de l'equity curve).
        sortino = _compute_sortino_from_curve(equity_curve)
        calmar = _compute_calmar_from_curve(equity_curve)
        monthly_ratio = _compute_monthly_pnl_positive_ratio(curve_rows)

        brier = _compute_brier(positions)
        zombie_ratio = _compute_zombie_ratio(positions)
        sizing_cv = _compute_sizing_cv(activity)
        cash_pnl_90d = _compute_cash_pnl_90d(positions)
        trade_count_90d = len(activity)
        days_active = _compute_days_active(activity)

        # HHI par catégorie Gamma (via MarketCategoryResolver).
        condition_ids = {
            t.get("conditionId") for t in activity if isinstance(t.get("conditionId"), str)
        }
        cid_to_cat: dict[str, str] = {}
        if condition_ids:
            cid_to_cat = await self._category_resolver.resolve_batch(
                [cid for cid in condition_ids if cid is not None],
            )
        hhi_categories = _compute_hhi_categories(activity, cid_to_cat)

        return TraderMetricsV2(
            base=base_metrics,
            sortino_90d=sortino,
            calmar_90d=calmar,
            brier_90d=brier,
            # v1 M12 : valeur neutre (§3.4 décision pragmatique). Reportable v2.1.
            timing_alpha_weighted=_TIMING_ALPHA_NEUTRAL,
            hhi_categories=hhi_categories,
            monthly_pnl_positive_ratio=monthly_ratio,
            zombie_ratio=zombie_ratio,
            sizing_cv=sizing_cv,
            cash_pnl_90d=cash_pnl_90d,
            trade_count_90d=trade_count_90d,
            days_active=days_active,
            monthly_equity_curve=equity_curve,
        )


# --- Helpers purs (réutilisés par les factors sur l'equity curve) -------------


def _compute_brier(positions: list[RawPosition]) -> float | None:
    """Brier score sur positions résolues (approximation §3.3).

    ``outcome = 1 if cash_pnl > 0 else 0`` (approximation YES-gagnante),
    ``predicted_prob = avg_price``. Moins fiable sur neg_risk multi-outcome
    — reportable v2.1 avec Gamma ``resolvedOutcome`` officiel.

    Retourne None si < 5 positions résolues (données insuffisantes).
    """
    resolved = [p for p in positions if p.is_resolved]
    if len(resolved) < _BRIER_MIN_RESOLVED:
        return None
    sq_errors: list[float] = []
    for p in resolved:
        outcome = 1.0 if float(p.cash_pnl) > 0 else 0.0
        pred = float(p.avg_price)
        sq_errors.append((outcome - pred) ** 2)
    return mean(sq_errors)


def _compute_zombie_ratio(
    positions: list[RawPosition],
    *,
    now: datetime | None = None,
) -> float:
    """``zombie_ratio`` (Gemini §1.1) — proportion capital immobilisé.

    - Position "zombie" : ``current_value / initial_value < 2%`` ET
      ``is_resolved=False`` (jamais liquidée).
    - **Excluded du dénominateur** : positions ouvertes depuis < 30 j
      *quand l'info est disponible* (M14 MA.6 — fix audit H-014).

    M14 MA.6 — implémentation du filtre temporel <30j :

    - Si ``p.opened_at != None`` ET ``p.opened_at > now - 30d`` →
      position trop récente, exclue (ne pas pénaliser un wallet qui vient
      d'ouvrir une nouvelle position).
    - Si ``p.opened_at is None`` (Data API ne fournit pas le timestamp,
      cas par défaut 2026-04-25) → position **incluse** (fallback
      comportement M12, dégradation gracieuse). À durcir dès qu'une source
      de timestamp est branchée (Goldsky subgraph M16, `detected_trades`
      proxy MB).

    ``now`` est injectable pour test reproductibilité.

    Cf. spec M14 §5.6 (MA.6) + audit H-014.
    """
    if now is None:
        now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=_ZOMBIE_MIN_AGE_DAYS)

    eligible: list[RawPosition] = []
    for p in positions:
        if float(p.initial_value) <= 0:
            continue
        # M14 MA.6 : filtre temporel uniquement quand l'info est disponible.
        # Fallback safe : opened_at=None → on inclut (ne pas sous-estimer
        # zombie_ratio à 0 partout sur les wallets sans data).
        if p.opened_at is not None and p.opened_at > cutoff:
            continue  # position trop récente, exclue du dénominateur
        eligible.append(p)

    if not eligible:
        return 0.0
    capital_total = sum(float(p.initial_value) for p in eligible)
    zombies = [
        p
        for p in eligible
        if not p.is_resolved
        and float(p.current_value) < _ZOMBIE_CURRENT_VALUE_PCT * float(p.initial_value)
    ]
    capital_zombie = sum(float(p.initial_value) for p in zombies)
    if capital_total == 0:
        return 0.0
    return capital_zombie / capital_total


def _compute_sizing_cv(activity: list[dict[str, Any]]) -> float:
    """Coefficient of variation des sizes de trade. Clippé ``[0, 1]``.

    CV = stdev / mean. Si < 2 trades, retourne 1.0 (worst-case = pas de
    sizing_stability). Clip à 1.0 pour borner le facteur discipline.
    """
    sizes: list[float] = []
    for t in activity:
        try:
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        notional = size * price
        if notional > 0:
            sizes.append(notional)
    if len(sizes) < 2:
        return 1.0
    m = mean(sizes)
    if m == 0:
        return 1.0
    std = pstdev(sizes)
    return min(1.0, std / m)


def _compute_cash_pnl_90d(positions: list[RawPosition]) -> float:
    """Sum de ``cash_pnl`` sur positions résolues."""
    return sum(float(p.cash_pnl) for p in positions if p.is_resolved)


def _compute_days_active(activity: list[dict[str, Any]]) -> int:
    """Nombre de jours distincts UTC où ≥ 1 trade a été exécuté."""
    days: set[date_type] = set()
    for t in activity:
        ts = t.get("timestamp")
        if isinstance(ts, int | float):
            try:
                days.add(datetime.fromtimestamp(float(ts), tz=UTC).date())
            except (ValueError, OSError):
                continue
    return len(days)


def _compute_hhi_categories(
    activity: list[dict[str, Any]],
    cid_to_cat: dict[str, str],
) -> float:
    """HHI sur volume (USD notional) agrégé par catégorie Gamma top-level.

    Markets sans catégorie résolue → bucket ``"other"``.
    Pas de trades ou volume nul → retourne 1.0 (concentration max, défaut
    défavorable pour `specialization`).
    """
    vol_per_cat: dict[str, float] = defaultdict(float)
    for t in activity:
        cid = t.get("conditionId")
        if not isinstance(cid, str):
            continue
        cat = cid_to_cat.get(cid, "other")
        try:
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        vol_per_cat[cat] += size * price
    total = sum(vol_per_cat.values())
    if total <= 0:
        return 1.0
    return sum((v / total) ** 2 for v in vol_per_cat.values())


def _compute_sortino_from_curve(curve: list[float]) -> float:
    """Exporte ``Sortino`` (raw, pré-normalisation) pour :class:`TraderMetricsV2`.

    Réutilise la logique de ``factors.risk_adjusted`` via import circulaire
    évité (on dupliquerait peu de code ici — 1 appel via private symbole).
    """
    from polycopy.discovery.scoring.v2.factors.risk_adjusted import (
        _daily_returns,
        _sortino_ratio,
    )

    if len(curve) < 2:
        return 0.0
    returns = _daily_returns(curve)
    if not returns:
        return 0.0
    return _sortino_ratio(returns, risk_free_rate=0.0)


def _compute_calmar_from_curve(curve: list[float]) -> float:
    from polycopy.discovery.scoring.v2.factors.risk_adjusted import (
        _calmar_ratio,
        _daily_returns,
    )

    if len(curve) < 2:
        return 0.0
    returns = _daily_returns(curve)
    if not returns:
        return 0.0
    return _calmar_ratio(curve, returns)


def _compute_monthly_pnl_positive_ratio(
    curve_rows: list[TraderDailyPnl],
) -> float:
    """Fraction de mois calendaires avec PnL > 0 sur la fenêtre.

    Un "mois" = couple (year, month). ``pnl_month = last_equity - first_equity``
    sur le mois. Si ``pnl > 0`` → mois positif. Retourne ``positive / total``.
    Zéro si pas assez de data (< 1 mois).
    """
    if not curve_rows:
        return 0.0
    by_month: dict[tuple[int, int], list[float]] = defaultdict(list)
    for r in curve_rows:
        d = r.date
        by_month[(d.year, d.month)].append(float(r.equity_usdc))
    if not by_month:
        return 0.0
    positive = 0
    for equities in by_month.values():
        if len(equities) < 2:
            # Mois avec 1 seul point → ignoré (pas d'évaluation possible).
            continue
        delta = equities[-1] - equities[0]
        if delta > 0:
            positive += 1
    total = sum(1 for eqs in by_month.values() if len(eqs) >= 2)
    if total == 0:
        return 0.0
    return positive / total
