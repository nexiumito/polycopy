"""Tests de la couche config (pydantic-settings)."""

import pytest

from polycopy.config import Settings


def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vide les env vars qui pourraient parasiter le test depuis le shell."""
    for var in (
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_FUNDER",
        "TARGET_WALLETS",
        "DRY_RUN",
        "EXECUTION_MODE",
    ):
        monkeypatch.delenv(var, raising=False)
    # Reset legacy detection flag for tests.
    import polycopy.config

    polycopy.config._LEGACY_DRY_RUN_DETECTED = False


def test_polymarket_keys_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated(monkeypatch)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.polymarket_private_key is None
    assert settings.polymarket_funder is None


def test_target_wallets_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated(monkeypatch)
    monkeypatch.setenv("TARGET_WALLETS", "0xabc, 0xdef")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.target_wallets == ["0xabc", "0xdef"]


def test_target_wallets_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated(monkeypatch)
    monkeypatch.setenv("TARGET_WALLETS", '["0xabc","0xdef"]')
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.target_wallets == ["0xabc", "0xdef"]


def test_target_wallets_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated(monkeypatch)
    monkeypatch.setenv("TARGET_WALLETS", "")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.target_wallets == []


def test_dry_run_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated(monkeypatch)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.dry_run is True


def test_risk_capital_stub_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated(monkeypatch)
    monkeypatch.delenv("RISK_AVAILABLE_CAPITAL_USD_STUB", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.risk_available_capital_usd_stub == 1000.0


def test_risk_capital_stub_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated(monkeypatch)
    monkeypatch.setenv("RISK_AVAILABLE_CAPITAL_USD_STUB", "2500")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.risk_available_capital_usd_stub == 2500.0


# ---------------------------------------------------------------------------
# M16 — Dynamic taker fees settings (cf. spec §9.3)
# ---------------------------------------------------------------------------


def test_m16_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults : flag=True, seuil=0.05$, cache cap=500."""
    from decimal import Decimal

    _isolated(monkeypatch)
    for var in (
        "STRATEGY_FEES_AWARE_ENABLED",
        "STRATEGY_MIN_EV_USD_AFTER_FEE",
        "STRATEGY_FEE_RATE_CACHE_MAX",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.strategy_fees_aware_enabled is True
    assert settings.strategy_min_ev_usd_after_fee == Decimal("0.05")
    assert settings.strategy_fee_rate_cache_max == 500


def test_m16_strategy_min_ev_validator_lower_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """STRATEGY_MIN_EV_USD_AFTER_FEE < 0.01 → ValidationError."""
    from pydantic import ValidationError

    _isolated(monkeypatch)
    monkeypatch.setenv("STRATEGY_MIN_EV_USD_AFTER_FEE", "0.005")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_m16_strategy_min_ev_validator_upper_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """STRATEGY_MIN_EV_USD_AFTER_FEE > 10.0 → ValidationError."""
    from pydantic import ValidationError

    _isolated(monkeypatch)
    monkeypatch.setenv("STRATEGY_MIN_EV_USD_AFTER_FEE", "15.0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_m16_strategy_fees_aware_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag opt-out vérifiable (debug / A/B test)."""
    _isolated(monkeypatch)
    monkeypatch.setenv("STRATEGY_FEES_AWARE_ENABLED", "false")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.strategy_fees_aware_enabled is False
