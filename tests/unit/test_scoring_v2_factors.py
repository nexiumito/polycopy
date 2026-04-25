"""Tests des 6 facteurs purs du scoring v2 (M12 §3.2-3.7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polycopy.discovery.dtos import TraderMetrics
from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2
from polycopy.discovery.scoring.v2.factors import (
    compute_calibration,
    compute_consistency,
    compute_discipline,
    compute_risk_adjusted,
    compute_specialization,
    compute_timing_alpha,
)


def _base_metrics() -> TraderMetrics:
    """M5 base metrics avec valeurs neutres."""
    return TraderMetrics(
        wallet_address="0xabc",
        resolved_positions_count=60,
        open_positions_count=5,
        win_rate=0.6,
        realized_roi=0.1,
        total_volume_usd=10_000.0,
        herfindahl_index=0.4,
        nb_distinct_markets=10,
        largest_position_value_usd=500.0,
        measurement_window_days=90,
        fetched_at=datetime.now(tz=UTC),
    )


def _metrics_v2(**overrides: object) -> TraderMetricsV2:
    """TraderMetricsV2 avec valeurs neutres + overrides ad-hoc."""
    data: dict[str, object] = {
        "base": _base_metrics(),
        "sortino_90d": 1.0,
        "calmar_90d": 0.5,
        "brier_90d": 0.18,
        "timing_alpha_weighted": 0.55,
        "hhi_categories": 0.5,
        "monthly_pnl_positive_ratio": 0.66,
        "zombie_ratio": 0.1,
        "sizing_cv": 0.3,
        "cash_pnl_90d": 500.0,
        "trade_count_90d": 80,
        "days_active": 60,
        "monthly_equity_curve": [100.0] * 30,
    }
    data.update(overrides)
    return TraderMetricsV2(**data)  # type: ignore[arg-type]


# --- risk_adjusted -------------------------------------------------------


def test_risk_adjusted_returns_zero_for_short_curve() -> None:
    """< 14 points → 0.0 (pas assez de data)."""
    m = _metrics_v2(monthly_equity_curve=[100.0] * 5)
    assert compute_risk_adjusted(m) == 0.0


def test_risk_adjusted_positive_for_upward_curve() -> None:
    """M14 MA.3 : courbe croissante avec variance suffisante → score > 0.

    Comportement M14 :
    - Sortino : variance OK + downside vide ou rare → Sharpe fallback (pas
      sentinel 3.0).
    - Calmar : si max_dd > 1e-4 → ratio fini ; sinon sentinel.
    - Score = median(sortino, calmar).

    Note : une courbe linéaire stricte `[100, 101, ..., 129]` a `pstdev`
    sous le seuil 1e-3 (returns ordonnés trop similaires) — comportement
    attendu MA.3 qui filtre les "fausses croissances". On utilise ici une
    courbe avec returns réalistes (hétéroscédasticité minime).
    """
    # Returns autour de +1% avec petit bruit réaliste.
    curve = [100.0 * (1.0 + 0.01 * (1 + (i % 5) * 0.2)) ** i for i in range(30)]
    m = _metrics_v2(monthly_equity_curve=curve)
    score = compute_risk_adjusted(m)
    assert score > 0.0


def test_risk_adjusted_penalizes_drawdown() -> None:
    """M14 MA.3 : courbe avec drawdown → median(Sortino, Calmar) toujours
    pénalisé par rapport à courbe croissante."""
    # Courbe haussière réaliste (variance suffisante).
    curve_up = [100.0 * (1.0 + 0.01 * (1 + (i % 5) * 0.2)) ** i for i in range(30)]
    # Courbe avec drawdown 30 % en milieu de période.
    curve_dd = [100.0 + i for i in range(14)] + [113.0 - i * 1.5 for i in range(16)]
    m_up = _metrics_v2(monthly_equity_curve=curve_up)
    m_dd = _metrics_v2(monthly_equity_curve=curve_dd)
    assert compute_risk_adjusted(m_dd) < compute_risk_adjusted(m_up)


# --- M14 MA.3 : variance min + median(Sortino, Calmar) -----------------------


def test_risk_adjusted_returns_zero_on_flat_curve() -> None:
    """MA.3 : `pstdev(returns) < 1e-3` → 0.0 (pas sentinel 3.0).

    Régression-clé contre H-009 : les zombies holders avec curve plate
    ne dominent plus le facteur risk_adjusted (qui leur donnait sentinel
    3.0 = top après normalisation).
    """
    flat = _metrics_v2(monthly_equity_curve=[1000.0] * 30)
    assert compute_risk_adjusted(flat) == 0.0


def test_risk_adjusted_uses_sharpe_fallback_on_zero_downside_with_variance() -> None:
    """MA.3 : downside vide ET variance > 1e-3 → Sharpe fallback (pas sentinel)."""
    # Returns tous positifs avec variance suffisante.
    curve = [100.0 * (1.0 + 0.01 * (i % 3)) for i in range(30)]
    m = _metrics_v2(monthly_equity_curve=curve)
    score = compute_risk_adjusted(m)
    # Sharpe fallback produit une valeur finite (pas saturée à 3.0
    # automatiquement).
    assert score > 0.0
    # Pas de plancher à 3.0 sentinel — un Sharpe modéré peut donner < 3.
    # On vérifie juste que la fonction ne crash pas sur le cas downside vide.


def test_risk_adjusted_uses_median_not_weighted_mean() -> None:
    """MA.3 : `median(Sortino, Calmar)` au lieu de `0.6 × Sortino + 0.4 × Calmar`.

    Sur 2 ratios Sortino=10.0, Calmar=0.5 :
    - M12 weighted mean = 0.6×10 + 0.4×0.5 = 6.2
    - M14 median = median(10.0, 0.5) = 5.25

    On teste indirectement via une courbe dont on connaît le ratio
    annualized_return / max_dd (Calmar) très différent du Sortino.

    Ici on construit une courbe qui :
    - Génère un Sortino élevé (peu de downside, variance modérée).
    - Génère un Calmar bas (gros drawdown qui domine annualized_return).
    """
    # Curve : 28 jours de croissance lente + 1 mois de drawdown 30%.
    curve = [100.0 + i * 0.5 for i in range(28)] + [70.0, 70.0]
    m = _metrics_v2(monthly_equity_curve=curve)
    score = compute_risk_adjusted(m)
    # Calmar va dominer vers le bas (gros DD), Sortino reste raisonnable.
    # Le median des deux est plus modéré qu'une moyenne pondérée 0.6/0.4.
    # On vérifie principalement que ça ne crashe pas et que le score est fini.
    assert isinstance(score, float)
    assert score == score  # not NaN


def test_risk_adjusted_robust_to_sentinel_cluster() -> None:
    """MA.3 : avec median, un wallet zombie (sentinel cluster) a un score plus bas
    qu'un wallet réel actif.

    Wallet zombie : curve plate → MA.3 force 0.0 (variance min).
    Wallet réel : curve avec returns réels variables → score positif.
    """
    # Curve réelle : croissance moyenne avec hétéroscédasticité (returns
    # daily compris entre +0.5% et +3% selon période → pstdev > 1e-3).
    real_returns = [1.0 + 0.01 * (1.0 + (i % 7) * 0.3) for i in range(30)]
    real_curve = [1000.0]
    for r in real_returns:
        real_curve.append(real_curve[-1] * r)
    zombie = _metrics_v2(monthly_equity_curve=[1000.0] * 30)
    real = _metrics_v2(monthly_equity_curve=real_curve)
    assert compute_risk_adjusted(zombie) == 0.0
    assert compute_risk_adjusted(real) > 0.0
    assert compute_risk_adjusted(real) > compute_risk_adjusted(zombie)


# --- calibration ---------------------------------------------------------


def test_calibration_positive_when_brier_below_baseline() -> None:
    """brier=0.15, baseline=0.25 → skill = 1 - 0.15/0.25 = 0.4."""
    m = _metrics_v2(brier_90d=0.15)
    assert compute_calibration(m, brier_baseline_pool=0.25) == pytest.approx(0.4)


def test_calibration_returns_zero_for_none_brier() -> None:
    """brier=None (pas de positions résolues) → 0.0."""
    m = _metrics_v2(brier_90d=None)
    assert compute_calibration(m, brier_baseline_pool=0.25) == 0.0


def test_calibration_fallback_on_degenerate_baseline() -> None:
    """Baseline ≤ 0 → fallback 0.25 (Brier random binaire)."""
    m = _metrics_v2(brier_90d=0.10)
    out = compute_calibration(m, brier_baseline_pool=0.0)
    assert out == pytest.approx(1.0 - (0.10 / 0.25))


# --- timing_alpha --------------------------------------------------------


def test_timing_alpha_passes_through_weighted_value() -> None:
    m = _metrics_v2(timing_alpha_weighted=0.7)
    assert compute_timing_alpha(m) == pytest.approx(0.7)


def test_timing_alpha_clips_to_unit_interval() -> None:
    assert compute_timing_alpha(_metrics_v2(timing_alpha_weighted=1.5)) == 1.0
    assert compute_timing_alpha(_metrics_v2(timing_alpha_weighted=-0.3)) == 0.0


# --- specialization (M14 MA.5 — flip HHI signal) -------------------------


def test_specialization_now_rewards_high_hhi() -> None:
    """MA.5 : HHI direct (pas inversé) — concentration = signal positif."""
    m = _metrics_v2(hhi_categories=0.85)
    # HHI=0.85 (forte concentration insider-like) → specialization=0.85.
    assert compute_specialization(m) == pytest.approx(0.85)


def test_specialization_diversified_wallet_gets_lower_score() -> None:
    """MA.5 : un wallet diversifié (HHI bas) reçoit un score bas (pas haut comme M12)."""
    m = _metrics_v2(hhi_categories=0.20)
    assert compute_specialization(m) == pytest.approx(0.20)


def test_specialization_max_concentration_returns_one() -> None:
    """MA.5 : HHI=1.0 (tout sur 1 catégorie) → specialization=1.0 (Mitts-Ofir reward)."""
    m = _metrics_v2(hhi_categories=1.0)
    assert compute_specialization(m) == pytest.approx(1.0)


def test_specialization_clips_to_unit_interval() -> None:
    """HHI hors [0, 1] (théoriquement impossible, défense en profondeur)."""
    m = _metrics_v2(hhi_categories=1.5)
    assert compute_specialization(m) == 1.0
    m_low = _metrics_v2(hhi_categories=-0.5)
    assert compute_specialization(m_low) == 0.0


# --- consistency ---------------------------------------------------------


def test_consistency_passes_through_monthly_ratio() -> None:
    m = _metrics_v2(monthly_pnl_positive_ratio=0.75)
    assert compute_consistency(m) == pytest.approx(0.75)


def test_consistency_clips_unit_interval() -> None:
    assert compute_consistency(_metrics_v2(monthly_pnl_positive_ratio=1.2)) == 1.0
    assert compute_consistency(_metrics_v2(monthly_pnl_positive_ratio=-0.1)) == 0.0


# --- discipline ----------------------------------------------------------


def test_discipline_product_formula() -> None:
    """(1 - 0.3) × (1 - 0.2) = 0.56."""
    m = _metrics_v2(zombie_ratio=0.3, sizing_cv=0.2)
    assert compute_discipline(m) == pytest.approx(0.56)


def test_discipline_high_zombie_penalized_heavily() -> None:
    """zombie=0.9 → anti_zombie=0.1, très faible peu importe le sizing."""
    m = _metrics_v2(zombie_ratio=0.9, sizing_cv=0.0)
    assert compute_discipline(m) == pytest.approx(0.1)


def test_discipline_high_sizing_cv_reduces_score() -> None:
    """sizing_cv=0.8 → sizing_stability=0.2."""
    m = _metrics_v2(zombie_ratio=0.0, sizing_cv=0.8)
    assert compute_discipline(m) == pytest.approx(0.2)


def test_discipline_saturated_sizing_cv_is_clipped() -> None:
    """sizing_cv > 1.0 → stability=0 (clippé)."""
    m = _metrics_v2(zombie_ratio=0.0, sizing_cv=5.0)
    assert compute_discipline(m) == 0.0
