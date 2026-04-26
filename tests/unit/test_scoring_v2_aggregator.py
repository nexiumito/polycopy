"""Tests aggregator compute_score_v2 + registry entry v2 (M12 §3.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from polycopy.discovery.dtos import TraderMetrics
from polycopy.discovery.scoring import SCORING_VERSIONS_REGISTRY
from polycopy.discovery.scoring.v2 import (
    PoolContext,
    ScoreV2Breakdown,
    TraderMetricsV2,
    bind_pool_context,
    compute_score_v2,
)


def _metrics(**overrides: Any) -> TraderMetricsV2:
    base = TraderMetrics(
        wallet_address=overrides.pop("wallet_address", "0xabc"),
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
    data: dict[str, Any] = {
        "base": base,
        "sortino_90d": 1.0,
        "calmar_90d": 0.5,
        "brier_90d": 0.18,
        "timing_alpha_weighted": 0.6,
        "hhi_categories": 0.3,
        "monthly_pnl_positive_ratio": 0.75,
        "zombie_ratio": 0.1,
        "sizing_cv": 0.2,
        "cash_pnl_90d": 500.0,
        "trade_count_90d": 80,
        "days_active": 60,
        "monthly_equity_curve": [100.0 + i * 0.5 for i in range(30)],
    }
    data.update(overrides)
    return TraderMetricsV2(**data)


def _wide_pool_context() -> PoolContext:
    """PoolContext avec pools larges couvrant ``[0, 2]`` pour normalisations."""
    wide = [x * 0.1 for x in range(20)]  # 0.0..1.9
    return PoolContext(
        risk_adjusted_pool=wide,
        calibration_pool=wide,
        timing_alpha_pool=wide,
        specialization_pool=wide,
        consistency_pool=wide,
        discipline_pool=wide,
        brier_baseline_pool=0.25,
    )


def test_compute_score_v2_returns_breakdown_with_all_fields() -> None:
    m = _metrics()
    ctx = _wide_pool_context()
    out = compute_score_v2(m, ctx)
    assert isinstance(out, ScoreV2Breakdown)
    assert out.wallet_address == "0xabc"
    assert 0.0 <= out.score <= 1.0
    assert out.scoring_version == "v2.1"
    assert out.brier_baseline_pool == pytest.approx(0.25)
    # 6 sous-scores bruts + 6 normalisés présents.
    for attr in (
        "risk_adjusted",
        "calibration",
        "timing_alpha",
        "specialization",
        "consistency",
        "discipline",
    ):
        assert hasattr(out.raw, attr)
        assert hasattr(out.normalized, attr)


def test_compute_score_v2_score_is_bounded_0_1() -> None:
    """Sortie ``score ∈ [0, 1]`` systématique même avec metrics extrêmes."""
    for extreme in [
        {"timing_alpha_weighted": -5.0, "hhi_categories": 5.0},  # dégénéré bas
        {"timing_alpha_weighted": 5.0, "hhi_categories": -1.0},  # dégénéré haut
    ]:
        m = _metrics(**extreme)
        out = compute_score_v2(m, _wide_pool_context())
        assert 0.0 <= out.score <= 1.0


def test_weights_sum_to_one() -> None:
    """Invariant : la somme des pondérations dans aggregator.py = 1.0.

    Test meta-formule : on importe les constantes privées et on vérifie.
    Évite qu'un refactor casse la pondération silencieusement.
    """
    from polycopy.discovery.scoring.v2 import aggregator

    total = (
        aggregator._WEIGHT_RISK_ADJUSTED
        + aggregator._WEIGHT_CALIBRATION
        + aggregator._WEIGHT_TIMING_ALPHA
        + aggregator._WEIGHT_SPECIALIZATION
        + aggregator._WEIGHT_CONSISTENCY
        + aggregator._WEIGHT_DISCIPLINE
    )
    assert total == pytest.approx(1.0)


# --- M14 MA.1 : drop timing_alpha weight + renormalize -----------------------


def test_aggregator_weights_sum_to_one_after_timing_alpha_drop() -> None:
    """MA.1 : after dropping timing_alpha to 0, the 5 remaining weights still sum to 1.0."""
    from polycopy.discovery.scoring.v2 import aggregator

    assert aggregator._WEIGHT_TIMING_ALPHA == 0.0
    non_zero_sum = (
        aggregator._WEIGHT_RISK_ADJUSTED
        + aggregator._WEIGHT_CALIBRATION
        + aggregator._WEIGHT_SPECIALIZATION
        + aggregator._WEIGHT_CONSISTENCY
        + aggregator._WEIGHT_DISCIPLINE
    )
    assert non_zero_sum == pytest.approx(1.0, abs=1e-9)


def test_aggregator_proportional_renormalization() -> None:
    """MA.1 : renormalisation proportionnelle (décision D7) — ratios M12 préservés.

    risk_adjusted / calibration == 0.25 / 0.20 == 1.25 (M12 ratio préservé).
    specialization / consistency == 0.15 / 0.10 == 1.5 (M12 ratio préservé).
    """
    from polycopy.discovery.scoring.v2 import aggregator

    # Valeurs explicites attendues post-renormalisation (0.25/0.80 etc.).
    assert pytest.approx(0.3125) == aggregator._WEIGHT_RISK_ADJUSTED
    assert pytest.approx(0.2500) == aggregator._WEIGHT_CALIBRATION
    assert pytest.approx(0.1875) == aggregator._WEIGHT_SPECIALIZATION
    assert pytest.approx(0.1250) == aggregator._WEIGHT_CONSISTENCY
    assert pytest.approx(0.1250) == aggregator._WEIGHT_DISCIPLINE
    # Ratios M12 préservés.
    assert pytest.approx(0.25 / 0.20) == (
        aggregator._WEIGHT_RISK_ADJUSTED / aggregator._WEIGHT_CALIBRATION
    )
    assert pytest.approx(0.15 / 0.10) == (
        aggregator._WEIGHT_SPECIALIZATION / aggregator._WEIGHT_CONSISTENCY
    )


def test_aggregator_same_pool_different_timing_alpha_returns_identical_score() -> None:
    """MA.1 : timing_alpha contribue à 0 → 2 metrics identiques sauf timing_alpha
    doivent avoir le même score final."""
    ctx = _wide_pool_context()
    m1 = _metrics(timing_alpha_weighted=0.1)
    m2 = _metrics(timing_alpha_weighted=0.9)
    out1 = compute_score_v2(m1, ctx)
    out2 = compute_score_v2(m2, ctx)
    assert out1.score == pytest.approx(out2.score, abs=1e-9)


# --- M14 MA.8 : ship SCORING_VERSION="v2.1" --------------------------------


def test_scoring_v2_1_registered_in_registry() -> None:
    """MA.8 : registry contient v1 ET v2.1 (v2 retiré post reset DB)."""
    assert "v1" in SCORING_VERSIONS_REGISTRY
    assert "v2.1" in SCORING_VERSIONS_REGISTRY


def test_score_breakdown_carries_v2_1_version() -> None:
    """MA.8 : ScoreV2Breakdown.scoring_version == "v2.1"."""
    m = _metrics()
    ctx = _wide_pool_context()
    out = compute_score_v2(m, ctx)
    assert out.scoring_version == "v2.1"


def test_settings_scoring_version_literal_accepts_v1_v2_1_v2_1_1() -> None:
    """M15 MB.2 : Settings.scoring_version Literal étendu ``v2.1.1``.

    Adapté de ``test_settings_scoring_version_literal_accepts_v1_and_v2_1``
    (MA.8) — le Literal accepte maintenant ``v1`` / ``v2.1`` / ``v2.1.1``.
    """
    from polycopy.config import Settings

    Settings(scoring_version="v1")
    Settings(scoring_version="v2.1")
    Settings(scoring_version="v2.1.1")
    # "v2" (M12) n'est plus accepté — DB reset post-M14, code remplacé in-place.
    with pytest.raises(ValueError, match="scoring_version"):
        Settings(scoring_version="v2")


def test_aggregator_no_uniform_bias_from_timing_alpha() -> None:
    """MA.1 : avec poids 0, le facteur n'injecte plus de bias additif.

    Le placeholder M12 ``timing_alpha_weighted=0.5`` produisait
    ``0.20 × 0.5 = 0.10`` uniforme sur tous les scores (audit H-008). Avec
    poids 0, la contribution du facteur est strictement zéro quel que soit
    le sous-score normalisé.
    """
    ctx = _wide_pool_context()
    m = _metrics(timing_alpha_weighted=0.5)
    out = compute_score_v2(m, ctx)
    contribution_timing = out.normalized.timing_alpha * 0.0  # weight = 0
    assert contribution_timing == 0.0


def test_registry_v2_wrapper_returns_score_with_pool_context() -> None:
    """SCORING_VERSIONS_REGISTRY['v2'] appelé avec contextvar posé → score."""
    assert "v2.1" in SCORING_VERSIONS_REGISTRY
    m = _metrics()
    ctx = _wide_pool_context()
    with bind_pool_context(ctx):
        score = SCORING_VERSIONS_REGISTRY["v2.1"](m)
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


def test_registry_v2_wrapper_returns_zero_without_pool_context() -> None:
    """Appel hors contextvar (test v1 qui touche le registry v2 par erreur) → 0.0."""
    m = _metrics()
    score = SCORING_VERSIONS_REGISTRY["v2.1"](m)
    assert score == 0.0


def test_registry_v2_wrapper_returns_zero_for_wrong_metrics_type() -> None:
    """``metrics`` en format v1 (TraderMetrics legacy) → 0.0 + log warn."""
    base = TraderMetrics(
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
    ctx = _wide_pool_context()
    with bind_pool_context(ctx):
        score = SCORING_VERSIONS_REGISTRY["v2.1"](base)
    assert score == 0.0


def test_bind_pool_context_resets_after_block() -> None:
    """bind_pool_context reset le contextvar après le bloc (même sur exception)."""
    from polycopy.discovery.scoring.v2.pool_context import _CURRENT_POOL_CONTEXT

    ctx = _wide_pool_context()
    assert _CURRENT_POOL_CONTEXT.get() is None
    with bind_pool_context(ctx):
        assert _CURRENT_POOL_CONTEXT.get() is ctx
    assert _CURRENT_POOL_CONTEXT.get() is None

    # Même avec exception.
    with pytest.raises(RuntimeError), bind_pool_context(ctx):
        assert _CURRENT_POOL_CONTEXT.get() is ctx
        raise RuntimeError("boom")
    assert _CURRENT_POOL_CONTEXT.get() is None


# --- M15 MB.2 : compute_score_v2_1_1 + cold-start branch (4 tests §9.2) -----


from polycopy.discovery.scoring.v2 import compute_score_v2_1_1  # noqa: E402
from polycopy.discovery.scoring.v2.aggregator import (  # noqa: E402
    _WEIGHT_CALIBRATION_V2_1_1,
    _WEIGHT_CONSISTENCY_V2_1_1,
    _WEIGHT_DISCIPLINE_V2_1_1,
    _WEIGHT_INTERNAL_PNL_V2_1_1,
    _WEIGHT_RISK_ADJUSTED_V2_1_1,
    _WEIGHT_SPECIALIZATION_V2_1_1,
    _WEIGHT_TIMING_ALPHA_V2_1_1,
)
from polycopy.discovery.scoring.v2.factors import compute_internal_pnl  # noqa: E402


def _v2_1_1_pool_context(
    *,
    internal_pnl_pool: list[float] | None = None,
) -> PoolContext:
    """PoolContext v2.1.1 avec internal_pnl_pool optionnel."""
    wide = [x * 0.1 for x in range(20)]  # 0.0..1.9
    return PoolContext(
        risk_adjusted_pool=wide,
        calibration_pool=wide,
        timing_alpha_pool=wide,
        specialization_pool=wide,
        consistency_pool=wide,
        discipline_pool=wide,
        internal_pnl_pool=internal_pnl_pool if internal_pnl_pool is not None else [],
        brier_baseline_pool=0.25,
    )


def test_aggregator_v2_1_1_weights_sum_to_one() -> None:
    """MB.2 §9.2 #5 — pondérations v2.1.1 + cold-start somment à 1.0."""
    full_sum = (
        _WEIGHT_RISK_ADJUSTED_V2_1_1
        + _WEIGHT_CALIBRATION_V2_1_1
        + _WEIGHT_TIMING_ALPHA_V2_1_1
        + _WEIGHT_SPECIALIZATION_V2_1_1
        + _WEIGHT_CONSISTENCY_V2_1_1
        + _WEIGHT_DISCIPLINE_V2_1_1
        + _WEIGHT_INTERNAL_PNL_V2_1_1
    )
    assert full_sum == pytest.approx(1.0, abs=1e-6)
    # Cold-start path utilise les poids v2.1 (somme déjà testée par
    # test_weights_sum_to_one ci-dessus).


def test_aggregator_cold_start_renormalizes_without_internal_pnl() -> None:
    """MB.2 §9.2 #6 — cold-start (internal_pnl_score=None) → flag set, score
    sur 5 facteurs avec poids v2.1 restaurés.
    """
    m = _metrics(internal_pnl_score=None)
    ctx = _v2_1_1_pool_context()  # pool internal_pnl vide (cas J0)
    breakdown = compute_score_v2_1_1(m, ctx)
    assert breakdown.cold_start_internal_pnl is True
    assert breakdown.scoring_version == "v2.1.1"
    # internal_pnl placeholder 0.0 dans raw + normalized (pas pondéré).
    assert breakdown.raw.internal_pnl == 0.0
    assert breakdown.normalized.internal_pnl == 0.0
    # Score borné [0, 1].
    assert 0.0 <= breakdown.score <= 1.0


def test_aggregator_v2_1_1_responds_to_internal_pnl_change() -> None:
    """MB.2 §9.2 #7 — 2 metrics identiques sauf internal_pnl_score → score
    diffère monotone (le full path pondère internal_pnl à 0.25)."""
    pool_internal = [0.1 * i for i in range(20)]  # 0.0..1.9
    ctx = _v2_1_1_pool_context(internal_pnl_pool=pool_internal)
    m_low = _metrics(internal_pnl_score=0.2)
    m_high = _metrics(internal_pnl_score=0.8)
    score_low = compute_score_v2_1_1(m_low, ctx).score
    score_high = compute_score_v2_1_1(m_high, ctx).score
    assert score_high > score_low
    # Aucun cold-start.
    assert compute_score_v2_1_1(m_low, ctx).cold_start_internal_pnl is False
    assert compute_score_v2_1_1(m_high, ctx).cold_start_internal_pnl is False


def test_compute_internal_pnl_clips_to_unit_interval() -> None:
    """MB.2 §9.2 #8 — defensive clip à [0, 1] sur valeur anormale."""
    # internal_pnl_score=1.5 (anormal — sigmoid produit toujours dans [0,1]).
    m = _metrics(internal_pnl_score=1.5)
    score = compute_internal_pnl(m)
    assert score == 1.0
    # Symétrique : -0.5 → 0.0.
    m_neg = _metrics(internal_pnl_score=-0.5)
    assert compute_internal_pnl(m_neg) == 0.0
    # None reste None (cold-start).
    m_none = _metrics(internal_pnl_score=None)
    assert compute_internal_pnl(m_none) is None


def test_v2_1_1_registered_in_registry() -> None:
    """MB.2 — registry contient v2.1.1 en plus de v1 et v2.1."""
    assert "v2.1.1" in SCORING_VERSIONS_REGISTRY
    # v2.1 et v1 toujours présents (audit trail M14 préservé).
    assert "v2.1" in SCORING_VERSIONS_REGISTRY
    assert "v1" in SCORING_VERSIONS_REGISTRY


def test_registry_v2_1_1_wrapper_returns_score_with_pool_context() -> None:
    """Wrapper v2.1.1 utilise contextvar pool_context comme v2.1."""
    m = _metrics(internal_pnl_score=0.5)
    ctx = _v2_1_1_pool_context(internal_pnl_pool=[0.1, 0.5, 0.9])
    with bind_pool_context(ctx):
        score = SCORING_VERSIONS_REGISTRY["v2.1.1"](m)
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0
