"""Facteur ``risk_adjusted`` (M12 §3.2).

Formule retenue : ``0.6 × Sortino + 0.4 × Calmar`` (synthèse §1.2).

**Sortino** pénalise uniquement la volatilité négative — plus approprié pour
des distributions asymétriques binaires que Sharpe standard.
**Calmar** pénalise le max drawdown — résilience aux événements extrêmes.

Source data : :attr:`TraderMetricsV2.monthly_equity_curve` (~90 points, 1 par
jour). Reconstruction amont par :class:`MetricsCollectorV2` depuis
``trader_daily_pnl``.

Les ratios bruts peuvent être fortement bimodaux (cap sentinel à 3.0 quand
aucun downside observé). La winsorisation p5-p95 pool-wide applicée plus tard
par :func:`apply_pool_normalization` compresse les outliers.

Pure function — aucun I/O, aucun state. Testable isolément sur un curve
synthétique.
"""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


# Cap supérieur sentinel quand un ratio n'est pas calculable (pas de downside,
# drawdown négligeable). Évite les valeurs infinies qui casseraient la
# winsorisation p5-p95.
_RATIO_CAP_SENTINEL: float = 3.0
# Minimum de points dans l'equity curve pour calculer un Sortino significatif.
# Aligné avec la gate days_active >= 30 (§4.1).
_MIN_CURVE_POINTS: int = 14


def compute_risk_adjusted(metrics: TraderMetricsV2) -> float:
    """Retourne la valeur brute ``0.6 · Sortino + 0.4 · Calmar``.

    Zéro si la curve est trop courte (< 14 points) — la normalisation
    pool-wide traduira ça par un score très bas (p5).
    """
    curve = list(metrics.monthly_equity_curve)
    if len(curve) < _MIN_CURVE_POINTS:
        return 0.0
    daily_returns = _daily_returns(curve)
    if not daily_returns:
        return 0.0
    sortino = _sortino_ratio(daily_returns, risk_free_rate=0.0)
    calmar = _calmar_ratio(curve, daily_returns)
    return 0.6 * sortino + 0.4 * calmar


def _daily_returns(curve: list[float]) -> list[float]:
    """``(e[i] / e[i-1]) - 1`` pour chaque point consécutif.

    Points à zéro ignorés (évite ``ZeroDivisionError``). Pour une curve
    quasi-constante, retourne une liste de 0.0 — Sortino / Calmar seront
    alors saturés au sentinel ``_RATIO_CAP_SENTINEL``.
    """
    returns: list[float] = []
    prev = curve[0]
    for current in curve[1:]:
        if prev != 0 and math.isfinite(prev) and math.isfinite(current):
            returns.append((current / prev) - 1.0)
        prev = current
    return returns


def _sortino_ratio(returns: list[float], *, risk_free_rate: float) -> float:
    """Sortino = (mean_return - risk_free) / downside_dev.

    - Aucun downside observé → cap ``_RATIO_CAP_SENTINEL`` (sentinel upper).
    - ``downside_dev == 0`` → même sentinel.
    """
    if not returns:
        return 0.0
    mean_ret = mean(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return _RATIO_CAP_SENTINEL
    downside_dev = pstdev(downside) if len(downside) > 1 else abs(downside[0])
    if downside_dev == 0.0:
        return _RATIO_CAP_SENTINEL
    return (mean_ret - risk_free_rate) / downside_dev


def _calmar_ratio(curve: list[float], returns: list[float]) -> float:
    """Calmar = annualized_return / max_drawdown.

    - Curve plate ou max_dd négligeable → sentinel (pas de risque observable).
    """
    if not returns or not curve:
        return 0.0
    annualized_ret = mean(returns) * 365.0
    max_dd = _max_drawdown(curve)
    if max_dd < 1e-4:
        return _RATIO_CAP_SENTINEL
    return annualized_ret / max_dd


def _max_drawdown(curve: list[float]) -> float:
    """Max drawdown relatif observé sur la curve. ∈ ``[0, 1]``.

    ``max((peak - trough) / peak)`` en scannant la série. Peak = max courant.
    """
    if not curve:
        return 0.0
    peak = curve[0]
    max_dd = 0.0
    for point in curve:
        if point > peak:
            peak = point
        if peak > 0:
            drawdown = (peak - point) / peak
            if drawdown > max_dd:
                max_dd = drawdown
    return max_dd
