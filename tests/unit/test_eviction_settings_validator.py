"""Tests du validator cross-field M5_bis _validate_m5_bis_eviction (Phase B.7).

Vérifie la règle :
- Si ``EVICTION_ENABLED=true`` et un wallet apparaît à la fois dans
  ``TARGET_WALLETS`` et ``BLACKLISTED_WALLETS``, boot crash clair.
- Si ``EVICTION_ENABLED=false``, l'overlap est silent (compat M5 stricte).

Cf. spec §5.3.
"""

from __future__ import annotations

import pytest

from polycopy.config import Settings


def test_eviction_off_allows_overlap_target_blacklist() -> None:
    """EVICTION_ENABLED=false → overlap cosmétique, pas de crash."""
    settings = Settings(
        target_wallets=["0xabc"],
        blacklisted_wallets=["0xabc"],
        eviction_enabled=False,
    )
    # Settings construit sans exception.
    assert settings.eviction_enabled is False


def test_eviction_on_rejects_overlap_target_blacklist() -> None:
    """EVICTION_ENABLED=true + overlap → ValueError au boot."""
    with pytest.raises(ValueError, match="both TARGET_WALLETS and BLACKLISTED_WALLETS"):
        Settings(
            target_wallets=["0xabc", "0xdef"],
            blacklisted_wallets=["0xabc"],
            eviction_enabled=True,
        )


def test_eviction_on_case_insensitive_overlap() -> None:
    """Normalisation lowercase entre les 2 listes avant comparaison."""
    with pytest.raises(ValueError, match="TARGET_WALLETS and BLACKLISTED_WALLETS"):
        Settings(
            target_wallets=["0xABC"],
            blacklisted_wallets=["0xabc"],
            eviction_enabled=True,
        )


def test_eviction_on_no_overlap_ok() -> None:
    """EVICTION_ENABLED=true sans overlap → construction OK."""
    settings = Settings(
        target_wallets=["0xabc"],
        blacklisted_wallets=["0xdef"],
        eviction_enabled=True,
    )
    assert settings.eviction_enabled is True
    # M14 MA.7 : default recalibré 0.15 → 0.10 (≈ 1σ empirique post-rank-transform).
    assert settings.eviction_score_margin == 0.10
    assert settings.eviction_hysteresis_cycles == 3


def test_eviction_score_margin_range_validation() -> None:
    """M14 MA.7 : EVICTION_SCORE_MARGIN doit être ∈ [0.02, 0.30].

    Bornes resserrées vs M5_bis (0.05 → 0.02 min, 0.50 → 0.30 max) pour
    refléter la distribution post-rank-transform v2.1 (variance σ ≈ 0.05-0.10
    vs σ ≈ 0.30 sur v2 winsorisée).
    """
    # Borne basse : 0.02 OK, 0.01 KO.
    Settings(eviction_score_margin=0.02)
    with pytest.raises(ValueError, match="eviction_score_margin"):
        Settings(eviction_score_margin=0.01)
    # Borne haute : 0.30 OK, 0.31 KO.
    Settings(eviction_score_margin=0.30)
    with pytest.raises(ValueError, match="eviction_score_margin"):
        Settings(eviction_score_margin=0.31)


# --- M14 MA.7 : new defaults --------------------------------------------------


def test_eviction_score_margin_default_is_0_10_post_rank_transform() -> None:
    """M14 MA.7 : default `EVICTION_SCORE_MARGIN=0.10` (1σ post-rank-transform)."""
    settings = Settings()
    assert settings.eviction_score_margin == 0.10


def test_eviction_hysteresis_cycles_range_validation() -> None:
    """EVICTION_HYSTERESIS_CYCLES ∈ [1, 10]."""
    with pytest.raises(ValueError, match="eviction_hysteresis_cycles"):
        Settings(eviction_hysteresis_cycles=0)
    with pytest.raises(ValueError, match="eviction_hysteresis_cycles"):
        Settings(eviction_hysteresis_cycles=11)


def test_max_sell_only_wallets_defaults_ok() -> None:
    """Default MAX_SELL_ONLY_WALLETS = 10 (aligné MAX_ACTIVE_TRADERS)."""
    settings = Settings()
    assert settings.max_sell_only_wallets == 10


def test_max_sell_only_wallets_override() -> None:
    """Override explicite accepté dans les bornes."""
    settings = Settings(max_sell_only_wallets=20)
    assert settings.max_sell_only_wallets == 20
