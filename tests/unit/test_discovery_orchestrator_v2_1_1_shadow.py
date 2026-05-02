"""Tests M15 MB.9 — v2.1.1 parallel shadow compute when v2.1 pilot.

Wire-up des 3 changements ciblés MB.9 :

1. Settings cross-field validator refuse SCORING_V2_1_1_SHADOW_DAYS>0 sous
   pilote ≠ v2.1.
2. ``_should_compute_v2_1_1`` retourne False par default (off) → seul v2.1
   est écrit en DB.
3. ``SCORING_V2_1_1_SHADOW_DAYS=14`` + pilot v2.1 → écrit aussi des rows
   v2.1.1 en parallèle (mêmes wallets, même cycle_at).

Cf. spec MB.9 + CLAUDE.md §Anti-toxic M15.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.discovery.dtos import CandidateWallet, TraderMetrics
from polycopy.discovery.orchestrator import DiscoveryOrchestrator
from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2
from polycopy.monitoring.dtos import Alert
from polycopy.storage.repositories import TraderScoreRepository


def _settings_v21(**overrides: Any) -> Settings:
    """Settings v2.1 pilote + cold-start mode pour relâcher les gates dans les tests."""
    defaults: dict[str, Any] = {
        "target_wallets": "0xdummy",
        "discovery_enabled": True,
        "discovery_interval_seconds": 3600,
        "discovery_candidate_pool_size": 10,
        "discovery_backend": "data_api",
        "scoring_version": "v2.1",
        "scoring_promotion_threshold": 0.40,
        "scoring_demotion_threshold": 0.20,
        "scoring_min_closed_markets": 1,
        "trader_shadow_days": 0,
        "discovery_shadow_bypass": True,
        "max_active_traders": 5,
        "scoring_v2_shadow_days": 0,
        "trader_daily_pnl_enabled": False,
        "scoring_v2_cold_start_mode": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _trader_metrics(wallet: str) -> TraderMetrics:
    return TraderMetrics(
        wallet_address=wallet,
        resolved_positions_count=80,
        open_positions_count=2,
        win_rate=0.7,
        realized_roi=0.4,
        total_volume_usd=15_000.0,
        herfindahl_index=0.3,
        nb_distinct_markets=8,
        largest_position_value_usd=800.0,
        measurement_window_days=90,
        fetched_at=datetime.now(tz=UTC),
    )


def _trader_metrics_v2(wallet: str) -> TraderMetricsV2:
    """Stub TraderMetricsV2 qui passe les 7 gates en cold-start mode."""
    return TraderMetricsV2(
        base=_trader_metrics(wallet),
        sortino_90d=1.5,
        calmar_90d=0.8,
        brier_90d=0.18,
        timing_alpha_weighted=0.55,
        hhi_categories=0.5,
        monthly_pnl_positive_ratio=0.66,
        zombie_ratio=0.1,
        sizing_cv=0.3,
        cash_pnl_90d=500.0,
        trade_count_90d=80,
        days_active=60,
        monthly_equity_curve=[100.0] * 30,
        net_exposure_ratio=0.8,
    )


async def _run_one_cycle(orch: DiscoveryOrchestrator, *, delay: float = 0.5) -> None:
    """Lance le cycle puis stop. Helper DRY (copie de test_orchestrator_score_persistence)."""
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(delay)
        stop.set()

    await asyncio.gather(orch.run_forever(stop), stop_soon())


def test_v2_1_1_shadow_pilot_v1_raises_validation_error() -> None:
    """MB.9 — Pydantic cross-field validator refuse v2.1.1 shadow sous pilote v1.

    La shadow v2.1.1 saute la version intermédiaire v2.1 qui doit être
    validée d'abord — config incohérente, crash boot clair.
    """
    with pytest.raises(ValueError, match="MB.9.*SCORING_V2_1_1_SHADOW_DAYS"):
        Settings(  # type: ignore[call-arg]
            scoring_version="v1",
            scoring_v2_1_1_shadow_days=14,
        )


def test_v2_1_1_shadow_pilot_v2_1_1_raises_validation_error() -> None:
    """MB.9 — Pydantic refuse aussi v2.1.1 shadow sous pilote v2.1.1 (redondant).

    Si pilote=v2.1.1, la version est déjà calculée via le path pilote — le
    shadow écrirait des rows doublons (cycle_at + wallet identiques).
    """
    with pytest.raises(ValueError, match="MB.9.*SCORING_V2_1_1_SHADOW_DAYS"):
        Settings(  # type: ignore[call-arg]
            scoring_version="v2.1.1",
            scoring_v2_1_1_shadow_days=14,
        )


def test_v2_1_1_shadow_off_validates_under_any_pilot() -> None:
    """MB.9 — Default 0 (off) accepté sous tous les pilotes (zéro régression)."""
    # Pas d'exception attendue dans les 3 cas.
    Settings(scoring_version="v1", scoring_v2_1_1_shadow_days=0)  # type: ignore[call-arg]
    Settings(scoring_version="v2.1", scoring_v2_1_1_shadow_days=0)  # type: ignore[call-arg]
    Settings(scoring_version="v2.1.1", scoring_v2_1_1_shadow_days=0)  # type: ignore[call-arg]


async def test_v2_1_1_shadow_off_by_default_writes_only_v2_1(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MB.9 — Default off : 1 cycle scoring 2 wallets v2.1 pilote → 0 row v2.1.1."""
    candidates = [
        CandidateWallet(wallet_address=f"0xa{i}", discovered_via="holders", initial_signal=2.0)
        for i in range(2)
    ]

    async def fake_collect_v1(wallet: str) -> TraderMetrics:
        return _trader_metrics(wallet)

    async def fake_collect_v2(wallet: str) -> TraderMetricsV2:
        return _trader_metrics_v2(wallet)

    with (
        patch("polycopy.discovery.orchestrator.CandidatePool", autospec=True) as mock_pool_cls,
        patch("polycopy.discovery.orchestrator.MetricsCollector", autospec=True) as mock_mc_cls,
        patch(
            "polycopy.discovery.orchestrator.MetricsCollectorV2",
            autospec=True,
        ) as mock_mc2_cls,
        patch("polycopy.discovery.orchestrator.MarketCategoryResolver", autospec=True),
        patch("polycopy.discovery.orchestrator.TraderDailyPnlWriter", autospec=True),
    ):
        mock_pool_cls.return_value.build = AsyncMock(return_value=candidates)
        mock_mc_cls.return_value.collect = AsyncMock(side_effect=fake_collect_v1)
        mock_mc2_cls.return_value.collect = AsyncMock(side_effect=fake_collect_v2)

        alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
        # scoring_v2_1_1_shadow_days default = 0 (off).
        orch = DiscoveryOrchestrator(session_factory, _settings_v21(), alerts)
        await _run_one_cycle(orch)

    score_repo = TraderScoreRepository(session_factory)
    all_rows = []
    for wallet in ["0xa0", "0xa1"]:
        all_rows.extend(await score_repo.list_for_wallet(wallet))
    versions = {r.scoring_version for r in all_rows}
    assert versions == {"v2.1"}, f"attendu {{v2.1}} only, observé {versions}"


async def test_v2_1_1_shadow_active_writes_both_versions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MB.9 — Shadow ON : 1 cycle scoring 2 wallets → 2 rows v2.1 + 2 rows v2.1.1."""
    candidates = [
        CandidateWallet(wallet_address=f"0xb{i}", discovered_via="holders", initial_signal=2.0)
        for i in range(2)
    ]

    async def fake_collect_v1(wallet: str) -> TraderMetrics:
        return _trader_metrics(wallet)

    async def fake_collect_v2(wallet: str) -> TraderMetricsV2:
        return _trader_metrics_v2(wallet)

    with (
        patch("polycopy.discovery.orchestrator.CandidatePool", autospec=True) as mock_pool_cls,
        patch("polycopy.discovery.orchestrator.MetricsCollector", autospec=True) as mock_mc_cls,
        patch(
            "polycopy.discovery.orchestrator.MetricsCollectorV2",
            autospec=True,
        ) as mock_mc2_cls,
        patch("polycopy.discovery.orchestrator.MarketCategoryResolver", autospec=True),
        patch("polycopy.discovery.orchestrator.TraderDailyPnlWriter", autospec=True),
    ):
        mock_pool_cls.return_value.build = AsyncMock(return_value=candidates)
        mock_mc_cls.return_value.collect = AsyncMock(side_effect=fake_collect_v1)
        mock_mc2_cls.return_value.collect = AsyncMock(side_effect=fake_collect_v2)

        alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
        orch = DiscoveryOrchestrator(
            session_factory,
            _settings_v21(scoring_v2_1_1_shadow_days=14),
            alerts,
        )
        await _run_one_cycle(orch)

    score_repo = TraderScoreRepository(session_factory)
    all_rows = []
    for wallet in ["0xb0", "0xb1"]:
        all_rows.extend(await score_repo.list_for_wallet(wallet))
    v21_rows = [r for r in all_rows if r.scoring_version == "v2.1"]
    v211_rows = [r for r in all_rows if r.scoring_version == "v2.1.1"]
    assert len(v21_rows) == 2, f"attendu 2 rows v2.1, observé {len(v21_rows)}"
    assert len(v211_rows) == 2, f"attendu 2 rows v2.1.1, observé {len(v211_rows)}"
    # Les wallets sont identiques entre les 2 versions (même cycle).
    assert {r.wallet_address for r in v21_rows} == {r.wallet_address for r in v211_rows}
