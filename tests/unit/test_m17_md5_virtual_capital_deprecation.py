"""Tests M17 MD.5 — deprecation `DRY_RUN_VIRTUAL_CAPITAL_USD` (audit H-004).

Pattern strict copié de M10 `_migrate_legacy_dry_run` — un validator
``model_validator(mode="before")`` reroute la valeur legacy vers
``DRY_RUN_INITIAL_CAPITAL_USD`` avec un flag module pour le warning
émis au boot par ``cli/runner.py``.
"""

from __future__ import annotations

import polycopy.config as cfg
from polycopy.config import Settings, legacy_virtual_capital_rerouted


def _reset_module_flag() -> None:
    """Reset le flag global avant chaque test (isolation stricte)."""
    cfg._LEGACY_VIRTUAL_CAPITAL_REROUTED = None


def test_dry_run_initial_capital_is_single_source_of_truth() -> None:
    """Set uniquement le nouveau → fonctionne, pas de reroute."""
    _reset_module_flag()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run_initial_capital_usd=2500.0,
    )
    assert settings.dry_run_initial_capital_usd == 2500.0
    assert legacy_virtual_capital_rerouted() is None


def test_deprecation_warning_logged_on_legacy_virtual_capital_var() -> None:
    """Set uniquement legacy → reroute + flag module pour warning boot."""
    _reset_module_flag()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run_virtual_capital_usd=1500.0,
    )
    # Reroute appliqué : nouveau setting porte la valeur.
    assert settings.dry_run_initial_capital_usd == 1500.0
    # Flag module positionné → cli/runner.py émettra le warning.
    assert legacy_virtual_capital_rerouted() == 1500.0


def test_legacy_fallback_disabled_if_new_explicit() -> None:
    """Les deux set → nouveau gagne, pas de reroute (pas de warning)."""
    _reset_module_flag()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run_virtual_capital_usd=999.0,  # legacy ignoré
        dry_run_initial_capital_usd=3333.0,  # nouveau gagne
    )
    assert settings.dry_run_initial_capital_usd == 3333.0
    assert legacy_virtual_capital_rerouted() is None
