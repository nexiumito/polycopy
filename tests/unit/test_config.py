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


# ---------------------------------------------------------------------------
# M18 — Polymarket V2 settings (cf. spec §7.1 + §7.2)
# ---------------------------------------------------------------------------


def test_settings_polymarket_clob_host_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default `https://clob.polymarket.com` (M18 §7.1)."""
    _isolated(monkeypatch)
    monkeypatch.delenv("POLYMARKET_CLOB_HOST", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.polymarket_clob_host == "https://clob.polymarket.com"


def test_settings_polymarket_clob_host_pattern_rejects_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pattern strict ^https:// — refuse http:// (M18 §7.1)."""
    from pydantic import ValidationError

    _isolated(monkeypatch)
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "http://insecure.example.com")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_polymarket_clob_host_v2_testnet_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override testnet pré-cutover (M18 §7.1)."""
    _isolated(monkeypatch)
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob-v2.polymarket.com")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.polymarket_clob_host == "https://clob-v2.polymarket.com"


def test_settings_polymarket_use_server_time_default_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default True — anti clock-skew (M18 §7.2 D8)."""
    _isolated(monkeypatch)
    monkeypatch.delenv("POLYMARKET_USE_SERVER_TIME", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.polymarket_use_server_time is True


# ---------------------------------------------------------------------------
# M18 ME.4 — pUSD collateral onramp settings
# ---------------------------------------------------------------------------


def test_settings_polymarket_collateral_onramp_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default `0x93070a847efEf7F70739046A929D47a521F5B8ee` (M18 §7.3)."""
    _isolated(monkeypatch)
    monkeypatch.delenv("POLYMARKET_COLLATERAL_ONRAMP_ADDRESS", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.polymarket_collateral_onramp_address == (
        "0x93070a847efEf7F70739046A929D47a521F5B8ee"
    )


def test_settings_polymarket_usdc_e_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (M18 §7.3)."""
    _isolated(monkeypatch)
    monkeypatch.delenv("POLYMARKET_USDC_E_ADDRESS", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.polymarket_usdc_e_address == "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def test_settings_collateral_onramp_pattern_rejects_invalid_hex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pattern strict ^0x + 40 hex (M18 §7.3)."""
    from pydantic import ValidationError

    _isolated(monkeypatch)
    monkeypatch.setenv("POLYMARKET_COLLATERAL_ONRAMP_ADDRESS", "invalid-not-hex")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_live_mode_requires_collateral_onramp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`EXECUTION_MODE=live` + onramp empty → ValueError (M18 §7.5)."""
    from pydantic import ValidationError

    _isolated(monkeypatch)
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xF0000000000000000000000000000000000000F0")
    # Empty string overrides default (Pydantic respects "" as a value).
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            polymarket_collateral_onramp_address="",
        )


def test_clob_clients_consume_polymarket_clob_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Les 4 read clients consomment ``settings.polymarket_clob_host`` (D7)."""
    import httpx

    from polycopy.executor.clob_metadata_client import ClobMetadataClient
    from polycopy.executor.clob_orderbook_reader import ClobOrderbookReader
    from polycopy.executor.fee_rate_client import FeeRateClient
    from polycopy.strategy.clob_read_client import ClobReadClient

    _isolated(monkeypatch)
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://test-host.example")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    http = httpx.AsyncClient()
    try:
        assert ClobReadClient(http, settings=settings)._base_url == "https://test-host.example"
        assert ClobMetadataClient(http, settings=settings)._base_url == "https://test-host.example"
        assert ClobOrderbookReader(http, settings=settings)._base_url == "https://test-host.example"
        assert FeeRateClient(http, settings=settings)._base_url == "https://test-host.example"
    finally:
        # httpx.AsyncClient must be closed; sync close is fine since no requests issued.
        import asyncio

        asyncio.run(http.aclose())
