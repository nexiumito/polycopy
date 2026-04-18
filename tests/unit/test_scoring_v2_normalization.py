"""Tests winsorisation p5-p95 + normalisation pool-wide (M12 §3.8)."""

from __future__ import annotations

import pytest

from polycopy.discovery.scoring.v2.normalization import (
    apply_pool_normalization,
    winsorize_p5_p95,
)


def test_winsorize_p5_p95_basic() -> None:
    values = list(range(100))  # 0..99
    p5, p95 = winsorize_p5_p95(values)
    # int(0.05 * 100) = 5 ; int(0.95 * 100) = 95
    assert p5 == 5
    assert p95 == 95


def test_winsorize_p5_p95_empty_pool() -> None:
    """Pool vide → sentinel ``(0.0, 1.0)`` (identité de normalisation)."""
    p5, p95 = winsorize_p5_p95([])
    assert p5 == 0.0
    assert p95 == 1.0


def test_winsorize_p5_p95_small_pool() -> None:
    """Petit pool → p5 et p95 bornés aux indices valides."""
    values = [0.1, 0.5, 0.9]
    p5, p95 = winsorize_p5_p95(values)
    # Avec n=3, int(0.05*3)=0, int(0.95*3)=2 → sorted[0]=0.1, sorted[2]=0.9
    assert p5 == pytest.approx(0.1)
    assert p95 == pytest.approx(0.9)


def test_apply_pool_normalization_clips_upper_outlier() -> None:
    """Valeur > p95 → 1.0."""
    pool = list(range(100))  # p95 = 95
    assert apply_pool_normalization(200.0, pool) == pytest.approx(1.0)


def test_apply_pool_normalization_clips_lower_outlier() -> None:
    """Valeur < p5 → 0.0."""
    pool = list(range(100))  # p5 = 5
    assert apply_pool_normalization(-10.0, pool) == pytest.approx(0.0)


def test_apply_pool_normalization_median_is_around_0_5() -> None:
    """La médiane du pool normalise autour de 0.5."""
    pool = list(range(100))
    normalized = apply_pool_normalization(50.0, pool)
    # (50 - 5) / (95 - 5) = 45/90 = 0.5
    assert normalized == pytest.approx(0.5)


def test_apply_pool_normalization_empty_pool_clips_value() -> None:
    """Pool vide → clip value à ``[0, 1]``."""
    assert apply_pool_normalization(0.7, []) == pytest.approx(0.7)
    assert apply_pool_normalization(1.5, []) == pytest.approx(1.0)
    assert apply_pool_normalization(-0.3, []) == pytest.approx(0.0)


def test_apply_pool_normalization_degenerate_pool_returns_half() -> None:
    """Pool plat (``p5 == p95``) → sentinel 0.5."""
    pool = [1.0] * 100
    assert apply_pool_normalization(1.0, pool) == pytest.approx(0.5)
    assert apply_pool_normalization(100.0, pool) == pytest.approx(0.5)


def test_apply_pool_normalization_bounded_in_unit_interval() -> None:
    """Contrat : sortie ∈ [0, 1] systématiquement."""
    pool = [x * 0.1 for x in range(100)]
    for raw in [-100.0, -1.0, 0.0, 0.5, 1.0, 5.0, 100.0]:
        out = apply_pool_normalization(raw, pool)
        assert 0.0 <= out <= 1.0, f"out-of-range for {raw}: {out}"


def test_apply_pool_normalization_monotonic() -> None:
    """Contrat : ``a ≤ b`` et pool fixé → ``apply(a) ≤ apply(b)``."""
    pool = list(range(100))
    values = [-50.0, 0.0, 5.0, 30.0, 50.0, 80.0, 95.0, 200.0]
    outs = [apply_pool_normalization(v, pool) for v in values]
    for i in range(len(outs) - 1):
        assert outs[i] <= outs[i + 1], f"non-monotonic at idx {i}: {outs}"


def test_winsorize_deterministic() -> None:
    """Contrat : même entrée → même sortie (pure function)."""
    values = [3.1, 1.5, 2.7, 0.9, 4.2, 1.1]
    first = winsorize_p5_p95(values)
    second = winsorize_p5_p95(values)
    assert first == second
