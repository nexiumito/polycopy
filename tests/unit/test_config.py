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
    ):
        monkeypatch.delenv(var, raising=False)


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
