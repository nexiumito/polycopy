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
    assert settings.eviction_score_margin == 0.15
    assert settings.eviction_hysteresis_cycles == 3


def test_eviction_score_margin_range_validation() -> None:
    """EVICTION_SCORE_MARGIN doit être ∈ [0.05, 0.50]."""
    with pytest.raises(ValueError, match="eviction_score_margin"):
        Settings(eviction_score_margin=0.03)
    with pytest.raises(ValueError, match="eviction_score_margin"):
        Settings(eviction_score_margin=0.60)


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
