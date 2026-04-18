"""Tests M10 §8.3 — ``Settings.execution_mode`` + backward-compat ``DRY_RUN``.

Couvre :
- Enum values acceptées / invalides.
- Legacy ``DRY_RUN=true/false`` → ``execution_mode`` avec flag detected.
- Priorité explicite ``EXECUTION_MODE`` > ``DRY_RUN`` legacy (sans warning).
- Property ``Settings.dry_run`` backward-compat.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import polycopy.config as cfg
from polycopy.config import Settings, legacy_dry_run_detected


def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DRY_RUN", "EXECUTION_MODE"):
        monkeypatch.delenv(var, raising=False)
    cfg._LEGACY_DRY_RUN_DETECTED = False


def test_execution_mode_enum_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated(monkeypatch)
    for mode in ("simulation", "dry_run", "live"):
        s = Settings(_env_file=None, execution_mode=mode)  # type: ignore[arg-type]
        assert s.execution_mode == mode


def test_execution_mode_invalid_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated(monkeypatch)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, execution_mode="other")  # type: ignore[arg-type]


def test_legacy_dry_run_true_maps_to_dry_run_mode_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.execution_mode == "dry_run"
    assert legacy_dry_run_detected() is True


def test_legacy_dry_run_false_maps_to_live_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.execution_mode == "live"
    assert legacy_dry_run_detected() is True


def test_explicit_execution_mode_wins_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("EXECUTION_MODE", "live")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.execution_mode == "live"
    assert legacy_dry_run_detected() is False


def test_dry_run_property_backward_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolated(monkeypatch)
    for mode, expected in (("simulation", True), ("dry_run", True), ("live", False)):
        s = Settings(_env_file=None, execution_mode=mode)  # type: ignore[arg-type]
        assert s.dry_run is expected


def test_default_execution_mode_is_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backward-compat M9 : un user qui ne touche pas son ``.env`` reste dry_run."""
    _isolated(monkeypatch)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.execution_mode == "dry_run"
    assert s.dry_run is True
    # Pas de DRY_RUN env ⇒ pas de legacy flag
    assert legacy_dry_run_detected() is False
