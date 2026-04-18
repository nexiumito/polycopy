"""Tests M11 §9.3.E — valeurs par défaut + validators des nouveaux champs Settings."""

from __future__ import annotations

import pytest

from polycopy.config import Settings


def _defaults() -> Settings:
    return Settings(_env_file=None, target_wallets=[])  # type: ignore[call-arg]


def test_m11_feature_flags_default_true() -> None:
    s = _defaults()
    assert s.strategy_clob_ws_enabled is True
    assert s.strategy_gamma_adaptive_cache_enabled is True
    assert s.latency_instrumentation_enabled is True


def test_m11_default_values() -> None:
    s = _defaults()
    assert s.strategy_clob_ws_url.startswith("wss://")
    assert s.strategy_clob_ws_max_subscribed == 500
    assert s.strategy_clob_ws_inactivity_unsub_seconds == 300
    assert s.strategy_clob_ws_health_check_seconds == 30
    assert s.latency_sample_retention_days == 7


def test_m11_max_subscribed_rejects_below_50() -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            target_wallets=[],
            strategy_clob_ws_max_subscribed=10,
        )


def test_m11_health_check_accepts_bounds() -> None:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        strategy_clob_ws_health_check_seconds=5,
    )
    assert s.strategy_clob_ws_health_check_seconds == 5
    s2 = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        strategy_clob_ws_health_check_seconds=300,
    )
    assert s2.strategy_clob_ws_health_check_seconds == 300


def test_m11_health_check_rejects_out_of_bounds() -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            target_wallets=[],
            strategy_clob_ws_health_check_seconds=3,
        )
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            target_wallets=[],
            strategy_clob_ws_health_check_seconds=500,
        )


def test_m11_latency_retention_accepts_bounds() -> None:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        latency_sample_retention_days=1,
    )
    assert s.latency_sample_retention_days == 1
    s90 = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        latency_sample_retention_days=90,
    )
    assert s90.latency_sample_retention_days == 90


def test_m11_flags_can_be_disabled() -> None:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        strategy_clob_ws_enabled=False,
        strategy_gamma_adaptive_cache_enabled=False,
        latency_instrumentation_enabled=False,
    )
    assert s.strategy_clob_ws_enabled is False
    assert s.strategy_gamma_adaptive_cache_enabled is False
    assert s.latency_instrumentation_enabled is False
