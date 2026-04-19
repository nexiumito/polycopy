"""Tests des 6 gates durs + check_all_gates fail-fast (M12 §4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from polycopy.config import Settings
from polycopy.discovery.dtos import TraderMetrics
from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2
from polycopy.discovery.scoring.v2.gates import (
    check_all_gates,
    check_cash_pnl,
    check_days_active,
    check_not_blacklisted,
    check_not_wash_cluster,
    check_trade_count,
    check_zombie_ratio,
)


def _metrics(**overrides: Any) -> TraderMetricsV2:
    """DTO TraderMetricsV2 avec valeurs neutres + overrides."""
    base = TraderMetrics(
        wallet_address=overrides.pop("wallet_address", "0xabc"),
        resolved_positions_count=60,
        open_positions_count=5,
        win_rate=0.6,
        realized_roi=0.1,
        total_volume_usd=10_000.0,
        herfindahl_index=0.4,
        nb_distinct_markets=10,
        largest_position_value_usd=500.0,
        measurement_window_days=90,
        fetched_at=datetime.now(tz=UTC),
    )
    data: dict[str, Any] = {
        "base": base,
        "sortino_90d": 1.0,
        "calmar_90d": 0.5,
        "brier_90d": 0.18,
        "timing_alpha_weighted": 0.55,
        "hhi_categories": 0.5,
        "monthly_pnl_positive_ratio": 0.66,
        "zombie_ratio": 0.1,
        "sizing_cv": 0.3,
        "cash_pnl_90d": 500.0,
        "trade_count_90d": 80,
        "days_active": 60,
        "monthly_equity_curve": [100.0] * 30,
    }
    data.update(overrides)
    return TraderMetricsV2(**data)


def _settings(**overrides: Any) -> Settings:
    env: dict[str, Any] = {}
    env.update(overrides)
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


# --- cash_pnl_positive ---------------------------------------------------


def test_gate_cash_pnl_passes_when_positive() -> None:
    result = check_cash_pnl(_metrics(cash_pnl_90d=100.0))
    assert result.passed is True
    assert result.gate_name == "cash_pnl_positive"


def test_gate_cash_pnl_fails_when_zero_or_negative() -> None:
    assert check_cash_pnl(_metrics(cash_pnl_90d=0.0)).passed is False
    result = check_cash_pnl(_metrics(cash_pnl_90d=-50.0))
    assert result.passed is False
    assert "-50" in result.reason


# --- trade_count_min -----------------------------------------------------


def test_gate_trade_count_passes_at_threshold() -> None:
    result = check_trade_count(_metrics(trade_count_90d=50), cold_start_mode=False)
    assert result.passed is True


def test_gate_trade_count_fails_below_threshold() -> None:
    result = check_trade_count(_metrics(trade_count_90d=30), cold_start_mode=False)
    assert result.passed is False
    assert "30" in result.reason


def test_gate_trade_count_cold_start_relaxed_to_20() -> None:
    # trade_count=25 : fail en mode strict, pass en cold_start
    assert check_trade_count(_metrics(trade_count_90d=25), cold_start_mode=False).passed is False
    assert check_trade_count(_metrics(trade_count_90d=25), cold_start_mode=True).passed is True


# --- days_active_min -----------------------------------------------------


def test_gate_days_active_passes_at_threshold() -> None:
    assert check_days_active(_metrics(days_active=30)).passed is True


def test_gate_days_active_fails_below_threshold() -> None:
    assert check_days_active(_metrics(days_active=15)).passed is False


def test_gate_days_active_cold_start_relaxed_to_7() -> None:
    # days_active=10 : fail en strict (< 30), pass en cold_start (>= 7).
    assert check_days_active(_metrics(days_active=10), cold_start_mode=False).passed is False
    assert check_days_active(_metrics(days_active=10), cold_start_mode=True).passed is True
    # days_active=6 : fail dans les deux modes.
    assert check_days_active(_metrics(days_active=6), cold_start_mode=True).passed is False


# --- zombie_ratio_max ----------------------------------------------------


def test_gate_zombie_ratio_passes_when_below_threshold() -> None:
    assert check_zombie_ratio(_metrics(zombie_ratio=0.2)).passed is True


def test_gate_zombie_ratio_fails_at_or_above_threshold() -> None:
    """0.40 strict — ``<`` pas ``≤``."""
    assert check_zombie_ratio(_metrics(zombie_ratio=0.40)).passed is False
    assert check_zombie_ratio(_metrics(zombie_ratio=0.50)).passed is False


# --- not_blacklisted -----------------------------------------------------


def test_gate_not_blacklisted_passes_when_wallet_not_in_env() -> None:
    settings = _settings(blacklisted_wallets=["0xbad"])
    assert check_not_blacklisted("0xabc", settings).passed is True


def test_gate_not_blacklisted_fails_when_wallet_in_env() -> None:
    settings = _settings(blacklisted_wallets=["0xabc", "0xbad"])
    assert check_not_blacklisted("0xabc", settings).passed is False


def test_gate_not_blacklisted_case_insensitive() -> None:
    """L'env store est lowercase, le gate doit aligner."""
    settings = _settings(blacklisted_wallets=["0xabc"])
    assert check_not_blacklisted("0xABC", settings).passed is False


# --- not_wash_cluster ----------------------------------------------------


def test_gate_not_wash_cluster_passes_when_settings_lacks_attr() -> None:
    """Settings M5/M11 n'ont pas `wash_cluster_wallets` → défaut pass."""
    settings = _settings()
    assert check_not_wash_cluster("0xabc", settings).passed is True


# --- check_all_gates fail-fast -------------------------------------------


def test_check_all_gates_returns_passed_when_all_pass() -> None:
    settings = _settings(blacklisted_wallets=[])
    result = check_all_gates(_metrics(), "0xabc", settings)
    assert result.passed is True
    assert result.failed_gate is None


def test_check_all_gates_fails_on_first_rejected_gate() -> None:
    """Blacklisted → gate 1 échoue, les suivants ne sont pas évalués."""
    settings = _settings(blacklisted_wallets=["0xabc"])
    # On met aussi zombie élevé pour vérifier que seul le 1er fail est retourné.
    metrics = _metrics(zombie_ratio=0.9, days_active=10)
    result = check_all_gates(metrics, "0xabc", settings)
    assert result.passed is False
    assert result.failed_gate is not None
    assert result.failed_gate.gate_name == "not_blacklisted"


def test_check_all_gates_returns_days_active_when_applicable() -> None:
    """Pas blacklisted, mais days_active insuffisant → gate 3 fail."""
    settings = _settings(blacklisted_wallets=[])
    metrics = _metrics(days_active=10)
    result = check_all_gates(metrics, "0xabc", settings)
    assert result.passed is False
    assert result.failed_gate is not None
    assert result.failed_gate.gate_name == "days_active_min"


def test_check_all_gates_returns_trade_count_when_applicable() -> None:
    settings = _settings(blacklisted_wallets=[])
    metrics = _metrics(trade_count_90d=5)
    result = check_all_gates(metrics, "0xabc", settings)
    assert result.failed_gate is not None
    assert result.failed_gate.gate_name == "trade_count_min"


def test_check_all_gates_returns_cash_pnl_when_applicable() -> None:
    settings = _settings(blacklisted_wallets=[])
    metrics = _metrics(cash_pnl_90d=-100.0)
    result = check_all_gates(metrics, "0xabc", settings)
    assert result.failed_gate is not None
    assert result.failed_gate.gate_name == "cash_pnl_positive"


def test_check_all_gates_returns_zombie_when_applicable() -> None:
    settings = _settings(blacklisted_wallets=[])
    metrics = _metrics(zombie_ratio=0.5)
    result = check_all_gates(metrics, "0xabc", settings)
    assert result.failed_gate is not None
    assert result.failed_gate.gate_name == "zombie_ratio_max"


def test_check_all_gates_cold_start_mode_from_settings_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cold_start_mode dérivé de ``settings.scoring_v2_cold_start_mode`` via getattr."""

    # Stub settings exposant l'attribut cold_start.
    class _Stub:
        blacklisted_wallets: list[str] = []
        scoring_v2_cold_start_mode = True

    metrics = _metrics(trade_count_90d=25)
    result = check_all_gates(metrics, "0xabc", _Stub())  # type: ignore[arg-type]
    # 25 passe en cold_start (seuil 20) alors qu'il faillerait en strict (50).
    assert result.passed is True
