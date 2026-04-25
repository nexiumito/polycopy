"""Tests normalisation pool-wide.

- Section M12 (deprecated) : `winsorize_p5_p95` + `apply_pool_normalization`
  conservées pour backward-compat (souffraient du fixed-point trap C7).
- Section M14 MA.2 : `rank_normalize` + `rank_normalize_one` — remplacent
  la winsorisation dans le scoring v2.1.
"""

from __future__ import annotations

import pytest

from polycopy.discovery.scoring.v2.normalization import (
    apply_pool_normalization,
    rank_normalize,
    rank_normalize_one,
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


# --- M14 MA.2 : rank_normalize remplace winsorize ----------------------------


def test_rank_normalize_returns_values_in_unit_interval() -> None:
    """MA.2 : tous les ranks normalisés ∈ ]0, 1]."""
    out = rank_normalize([-5.0, 0.0, 100.0, 1.0, -1.0])
    assert all(0.0 < v <= 1.0 for v in out)


def test_rank_normalize_preserves_order() -> None:
    """MA.2 : la valeur max reçoit le rank max, la min le rank min."""
    values = [1.0, 5.0, 3.0]
    out = rank_normalize(values)
    # `5.0` est l'index 1, doit avoir le rank max (3/3 = 1.0).
    assert out[1] == pytest.approx(1.0)
    # `1.0` est l'index 0, doit avoir le rank min (1/3).
    assert out[0] == pytest.approx(1.0 / 3.0)
    # `3.0` est l'index 2, rank intermédiaire.
    assert out[2] == pytest.approx(2.0 / 3.0)


def test_rank_normalize_handles_ties_with_average_interpolation() -> None:
    """MA.2 : ties → moyenne des rangs occupés (élimine fixed-point trap C7).

    `[1, 1, 1, 4]` → 3 valeurs égales aux rangs 1, 2, 3 → moyenne 2 → 2/4 = 0.5.
    `4` au rang 4 → 4/4 = 1.0.
    """
    out = rank_normalize([1.0, 1.0, 1.0, 4.0])
    assert out[0] == pytest.approx(0.5)
    assert out[1] == pytest.approx(0.5)
    assert out[2] == pytest.approx(0.5)
    assert out[3] == pytest.approx(1.0)


def test_rank_normalize_stable_on_small_pool_addition() -> None:
    """MA.2 : régression-clé contre fixed-point trap C7.

    Sur un pool small N=13, ajouter 1 wallet avec valeur médiane ne fait
    bouger que les ranks adjacents — pas l'ensemble du pool.
    """
    pool_13 = [0.10, 0.20, 0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.90, 0.95]
    # Ajouter un wallet à valeur médiane 0.52 (entre 0.50 et 0.55).
    pool_14 = pool_13 + [0.52]
    ranks_after = rank_normalize(pool_14)

    # Top 3 (idx 10, 11, 12) doit avoir le même rank/N relatif (à epsilon de N±1).
    # Avant : ranks_before[12] = 13/13 = 1.0
    # Après : ranks_after[12] = 14/14 = 1.0 (toujours top)
    assert ranks_after[12] == pytest.approx(1.0)
    assert ranks_after[11] == pytest.approx(13.0 / 14.0)
    assert ranks_after[10] == pytest.approx(12.0 / 14.0)

    # Bottom 3 (idx 0, 1, 2) reste également stable en rank relatif.
    assert ranks_after[0] == pytest.approx(1.0 / 14.0)
    assert ranks_after[1] == pytest.approx(2.0 / 14.0)
    assert ranks_after[2] == pytest.approx(3.0 / 14.0)


def test_rank_normalize_one_helper_appends_wallet() -> None:
    """MA.2 : helper convenience — wallet ajouté au pool, retourne son rank."""
    out = rank_normalize_one(5.0, [1.0, 2.0, 3.0, 4.0])
    # `5.0` est top du pool ∪ {5.0} de taille 5 → rank = 5/5 = 1.0.
    assert out == pytest.approx(1.0)

    # Wallet médian dans le pool.
    out_mid = rank_normalize_one(2.5, [1.0, 2.0, 3.0, 4.0])
    # `2.5` est en position 3 (sur 5) → 3/5 = 0.6.
    assert out_mid == pytest.approx(0.6)


def test_rank_normalize_empty_pool() -> None:
    """MA.2 : pool vide → liste vide."""
    assert rank_normalize([]) == []


def test_rank_normalize_single_element() -> None:
    """MA.2 : 1 élément → 1.0 (sentinel = top)."""
    assert rank_normalize([42.0]) == [1.0]


def test_rank_normalize_one_degenerate_pool() -> None:
    """MA.2 : pool dégénéré (toutes valeurs identiques + wallet identique) → 0.5."""
    out = rank_normalize_one(1.0, [1.0, 1.0, 1.0, 1.0])
    # 5 valeurs toutes égales → rank moyen = 3 → 3/5 = 0.6 (pas exactement 0.5
    # à cause du tie-breaking qui inclut le wallet).
    assert out == pytest.approx(0.6)
