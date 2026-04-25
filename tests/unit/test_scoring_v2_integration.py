"""Tests intégration orchestrator dual-compute v1/v2 (M12 §5.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from polycopy.config import Settings
from polycopy.discovery.scoring.v2 import PoolContext
from polycopy.storage.dtos import TraderDailyPnlDTO
from polycopy.storage.repositories import (
    TargetTraderRepository,
    TraderDailyPnlRepository,
    TraderEventRepository,
    TraderScoreRepository,
)


def _settings(**overrides: Any) -> Settings:
    env: dict[str, Any] = {}
    env.update(overrides)
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_scoring_version_validator_rejects_invalid_literal() -> None:
    """`SCORING_VERSION` promu à Literal['v1','v2'] — 'v3' → rejet boot."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, scoring_version="v3")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_wash_cluster_wallets_csv_parsing() -> None:
    """WASH_CLUSTER_WALLETS accepte CSV et JSON, stocke lowercase."""
    s_csv = _settings(wash_cluster_wallets="0xAAA,0xBBB,0xccc")
    assert s_csv.wash_cluster_wallets == ["0xaaa", "0xbbb", "0xccc"]
    s_json = _settings(wash_cluster_wallets='["0xDEAD", "0xBEEF"]')
    assert s_json.wash_cluster_wallets == ["0xdead", "0xbeef"]
    s_empty = _settings(wash_cluster_wallets="")
    assert s_empty.wash_cluster_wallets == []


@pytest.mark.asyncio
async def test_v2_shadow_active_returns_true_when_no_v2_rows(
    session_factory: Any,
) -> None:
    """Première run (table ``trader_scores`` n'a jamais vu v2) → shadow actif."""
    from polycopy.discovery.orchestrator import DiscoveryOrchestrator

    orchestrator = DiscoveryOrchestrator(
        session_factory,
        _settings(scoring_version="v1", scoring_v2_shadow_days=14),
    )
    assert await orchestrator._is_v2_shadow_active() is True


@pytest.mark.asyncio
async def test_v2_shadow_active_returns_false_when_expired(
    session_factory: Any,
    trader_score_repo: TraderScoreRepository,
    target_trader_repo: TargetTraderRepository,
) -> None:
    """Première row v2 > SCORING_V2_SHADOW_DAYS → shadow expiré."""
    from sqlalchemy import update

    from polycopy.discovery.orchestrator import DiscoveryOrchestrator
    from polycopy.storage.dtos import TraderScoreDTO
    from polycopy.storage.models import TraderScore

    trader = await target_trader_repo.insert_shadow("0xabc")
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=trader.id,
            wallet_address="0xabc",
            score=0.5,
            scoring_version="v2.1",
            low_confidence=False,
            metrics_snapshot={},
        ),
    )
    # Back-date la row pour simuler 20 jours écoulés.
    old_cycle_at = datetime.now(tz=UTC) - timedelta(days=20)
    async with session_factory() as session:
        await session.execute(
            update(TraderScore)
            .where(TraderScore.wallet_address == "0xabc")
            .values(cycle_at=old_cycle_at),
        )
        await session.commit()

    orchestrator = DiscoveryOrchestrator(
        session_factory,
        _settings(scoring_version="v1", scoring_v2_shadow_days=14),
    )
    assert await orchestrator._is_v2_shadow_active() is False


@pytest.mark.asyncio
async def test_should_compute_v2_returns_false_when_v1_pilot_and_no_shadow(
    session_factory: Any,
) -> None:
    """``SCORING_VERSION=v1`` + ``SCORING_V2_SHADOW_DAYS=0`` → v2 skipped."""
    from polycopy.discovery.orchestrator import DiscoveryOrchestrator

    orchestrator = DiscoveryOrchestrator(
        session_factory,
        _settings(scoring_version="v1", scoring_v2_shadow_days=0),
    )
    assert await orchestrator._should_compute_v2() is False


@pytest.mark.asyncio
async def test_should_compute_v2_returns_true_when_v2_pilot(
    session_factory: Any,
) -> None:
    """``SCORING_VERSION=v2`` → toujours compute v2."""
    from polycopy.discovery.orchestrator import DiscoveryOrchestrator

    orchestrator = DiscoveryOrchestrator(
        session_factory,
        _settings(scoring_version="v2.1"),
    )
    assert await orchestrator._should_compute_v2() is True


@pytest.mark.asyncio
async def test_build_pool_context_from_metrics_aggregates_raw_values(
    session_factory: Any,
) -> None:
    """`_build_pool_context_from_metrics` agrège pool-wide + Brier baseline."""
    from datetime import UTC, datetime

    from polycopy.discovery.dtos import TraderMetrics
    from polycopy.discovery.orchestrator import DiscoveryOrchestrator
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2

    base = TraderMetrics(
        wallet_address="0xabc",
        resolved_positions_count=20,
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
    m1 = TraderMetricsV2(
        base=base.model_copy(update={"wallet_address": "0xaaa"}),
        sortino_90d=1.0,
        calmar_90d=0.5,
        brier_90d=0.18,
        timing_alpha_weighted=0.5,
        hhi_categories=0.3,
        monthly_pnl_positive_ratio=0.66,
        zombie_ratio=0.1,
        sizing_cv=0.2,
        cash_pnl_90d=100.0,
        trade_count_90d=80,
        days_active=60,
        monthly_equity_curve=[100.0] * 30,
    )
    m2 = m1.model_copy(
        update={
            "base": base.model_copy(update={"wallet_address": "0xbbb"}),
            "brier_90d": 0.22,
        },
    )
    orchestrator = DiscoveryOrchestrator(session_factory, _settings())
    ctx = orchestrator._build_pool_context_from_metrics([m1, m2])
    assert isinstance(ctx, PoolContext)
    assert len(ctx.risk_adjusted_pool) == 2
    assert len(ctx.calibration_pool) == 2
    # Baseline = moyenne des brier_90d = (0.18 + 0.22)/2 = 0.20
    assert ctx.brier_baseline_pool == pytest.approx(0.20)


@pytest.mark.asyncio
async def test_build_pool_context_fallback_brier_baseline_when_all_none(
    session_factory: Any,
) -> None:
    """Pool sans brier disponible → baseline = 0.25 (sentinel)."""
    from polycopy.discovery.dtos import TraderMetrics
    from polycopy.discovery.orchestrator import DiscoveryOrchestrator
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2

    base = TraderMetrics(
        wallet_address="0xabc",
        resolved_positions_count=20,
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
    m = TraderMetricsV2(
        base=base,
        brier_90d=None,  # pas assez de positions résolues
        monthly_equity_curve=[100.0] * 30,
    )
    orchestrator = DiscoveryOrchestrator(session_factory, _settings())
    ctx = orchestrator._build_pool_context_from_metrics([m])
    assert ctx.brier_baseline_pool == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_trader_daily_pnl_dto_roundtrip(
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    """Smoke : writer simulé → repo roundtrip avec TraderDailyPnlDTO."""
    now = datetime.now(tz=UTC)
    dto = TraderDailyPnlDTO(
        wallet_address="0xabc",
        date=now.date(),
        equity_usdc=123.45,
        realized_pnl_day=10.0,
        unrealized_pnl_day=-5.0,
        positions_count=3,
    )
    inserted = await trader_daily_pnl_repo.insert_if_new(dto)
    assert inserted is True
    curve = await trader_daily_pnl_repo.get_curve("0xabc", days=1)
    assert len(curve) == 1
    assert float(curve[0].equity_usdc) == pytest.approx(123.45)


@pytest.mark.asyncio
async def test_gate_rejected_event_persistence(
    trader_event_repo: TraderEventRepository,
) -> None:
    """Smoke test : event 'gate_rejected' écrit correctement via TraderEventDTO."""
    from polycopy.storage.dtos import TraderEventDTO

    await trader_event_repo.insert(
        TraderEventDTO(
            wallet_address="0xzombie",
            event_type="gate_rejected",
            from_status="shadow",
            to_status="shadow",
            score_at_event=None,
            scoring_version="v2.1",
            reason="zombie_ratio:0.52 >= 0.40",
            event_metadata={
                "gate": "zombie_ratio_max",
                "value": 0.52,
                "threshold": 0.40,
            },
        ),
    )
    events = await trader_event_repo.list_recent(limit=10)
    assert len(events) == 1
    assert events[0].event_type == "gate_rejected"
    assert events[0].scoring_version == "v2.1"
    assert events[0].event_metadata is not None
    assert events[0].event_metadata["gate"] == "zombie_ratio_max"
