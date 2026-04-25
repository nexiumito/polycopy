"""Facteur ``risk_adjusted`` (M14 MA.3 — variance min + median robuste).

Formule retenue M14 : ``median(Sortino, Calmar)`` (au lieu de
``0.6 × Sortino + 0.4 × Calmar`` M12) — robuste au sentinel cluster
(Claude C10, audit H-009 fix).

**Sortino** pénalise uniquement la volatilité négative — plus approprié pour
des distributions asymétriques binaires que Sharpe standard.
**Calmar** pénalise le max drawdown — résilience aux événements extrêmes.

Source data : :attr:`TraderMetricsV2.monthly_equity_curve` (~90 points, 1 par
jour). Reconstruction amont par :class:`MetricsCollectorV2` depuis
``trader_daily_pnl``.

**Changements M14 vs M12** (3 fixes pour audit H-009) :

1. **Variance min `pstdev > 1e-3` exigée** : si la curve est plate
   (zombie holder qui ne trade plus), le facteur retourne **0.0**, pas le
   sentinel 3.0. On scorrait sinon "absence d'évidence comme évidence de
   skill" (Claude C10).
2. **Sharpe fallback** : quand `downside_dev == 0` (que des returns positifs)
   mais la variance totale est OK, on utilise `mean / pstdev(all_returns)`
   au lieu du sentinel 3.0. Corrélation Sharpe/Sortino r > 0.95
   (Rollinger & Hoffman 2013, CME).
3. **`median(Sortino, Calmar)`** au lieu de moyenne pondérée — la médiane
   est insensible au sentinel cluster (1 ratio sentinel dans le mix ne
   tire plus l'autre vers le haut).

Pure function — aucun I/O, aucun state. Testable isolément sur un curve
synthétique.

Cf. spec M14 §5.3 (MA.3) + Claude §3.1 + audit H-009.
"""

from __future__ import annotations

import math
from statistics import mean, median, pstdev
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


# Cap supérieur sentinel pour Calmar quand max_dd négligeable. Garde sa
# sémantique M12 (Calmar n'a pas de fallback Sharpe-équivalent — un
# drawdown nul est legitimately ratio-undefined). Le `median()` aval absorbe
# cette singularité.
_RATIO_CAP_SENTINEL: float = 3.0
# Minimum de points dans l'equity curve pour calculer un ratio significatif.
# Aligné avec la gate days_active >= 30.
_MIN_CURVE_POINTS: int = 14
# M14 MA.3 : variance min sous laquelle un wallet est "inobservable" (pas
# génial). En dessous → facteur = 0.0 (vs sentinel 3.0 M12 qui faisait
# remonter les zombies au top).
_MIN_VARIANCE_THRESHOLD: float = 1e-3


def compute_risk_adjusted(metrics: TraderMetricsV2) -> float:
    """Retourne la valeur brute ``median(Sortino, Calmar)``.

    Zéro si :
    - la curve est trop courte (< 14 points), ou
    - la variance des returns est sous ``_MIN_VARIANCE_THRESHOLD`` (curve
      plate = zombie holder, pas skill).

    M14 MA.3 : combinaison via `median()` au lieu de moyenne pondérée
    `0.6 · Sortino + 0.4 · Calmar` (M12). La médiane est robuste au
    sentinel cluster (1 ratio à 3.0 sentinel ne tire plus l'autre).
    """
    curve = list(metrics.monthly_equity_curve)
    if len(curve) < _MIN_CURVE_POINTS:
        return 0.0
    daily_returns = _daily_returns(curve)
    if not daily_returns:
        return 0.0
    # M14 MA.3 critical : variance min pour considérer le facteur observable.
    if pstdev(daily_returns) < _MIN_VARIANCE_THRESHOLD:
        return 0.0  # curve plate = pas de skill mesurable (pas "skill inactif")

    sortino = _sortino_ratio(daily_returns, risk_free_rate=0.0)
    calmar = _calmar_ratio(curve, daily_returns)
    # M14 MA.3 : median robust to sentinel cluster (Claude C10).
    return median([sortino, calmar])


def _daily_returns(curve: list[float]) -> list[float]:
    """``(e[i] / e[i-1]) - 1`` pour chaque point consécutif.

    Points à zéro ignorés (évite ``ZeroDivisionError``). Pour une curve
    quasi-constante, retourne une liste proche de 0.0 — la garde
    `pstdev < _MIN_VARIANCE_THRESHOLD` aval renvoie 0.0 dans `compute_risk_adjusted`.
    """
    returns: list[float] = []
    prev = curve[0]
    for current in curve[1:]:
        if prev != 0 and math.isfinite(prev) and math.isfinite(current):
            returns.append((current / prev) - 1.0)
        prev = current
    return returns


def _sortino_ratio(returns: list[float], *, risk_free_rate: float) -> float:
    """Sortino = ``(mean_return - risk_free) / downside_dev``.

    M14 MA.3 — fallback Sharpe quand downside vide :

    - Pas de returns négatifs (downside vide) ET variance totale OK →
      retourne ``(mean - risk_free) / pstdev(all_returns)`` (Sharpe).
      Corrélation Sharpe/Sortino r > 0.95 (Rollinger 2013, CME). Évite
      le sentinel 3.0 M12 qui dominait la winsorisation pool.
    - Downside présent → Sortino classique.
    - ``downside_dev == 0`` (1 seul downside avec valeur 0) → même fallback Sharpe.

    Le caller `compute_risk_adjusted` a déjà early-return 0.0 si la
    variance totale est < 1e-3, donc ``pstdev(returns)`` est ici garanti
    > 0 (sauf cas pathologique liste vide géré).
    """
    if not returns:
        return 0.0
    mean_ret = mean(returns)
    downside = [r for r in returns if r < 0]
    total_dev = pstdev(returns)
    if not downside or (
        (pstdev(downside) if len(downside) > 1 else abs(downside[0])) == 0.0
    ):
        # M14 MA.3 : Sharpe fallback (pas sentinel 3.0).
        if total_dev == 0.0:
            return 0.0  # défense en profondeur (caller a déjà gardé)
        return (mean_ret - risk_free_rate) / total_dev
    downside_dev = pstdev(downside) if len(downside) > 1 else abs(downside[0])
    return (mean_ret - risk_free_rate) / downside_dev


def _calmar_ratio(curve: list[float], returns: list[float]) -> float:
    """Calmar = ``annualized_return / max_drawdown``.

    - Curve plate ou ``max_dd < 1e-4`` → sentinel ``_RATIO_CAP_SENTINEL``
      (drawdown nul = ratio mathématiquement undefined). Le `median()` aval
      dans `compute_risk_adjusted` absorbe cette singularité.
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
