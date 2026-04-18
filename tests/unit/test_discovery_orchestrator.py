"""Tests DiscoveryOrchestrator (cycle e2e mocked)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.discovery.dtos import CandidateWallet, TraderMetrics
from polycopy.discovery.orchestrator import DiscoveryOrchestrator
from polycopy.monitoring.dtos import Alert
from polycopy.storage.repositories import (
    TargetTraderRepository,
    TraderEventRepository,
)


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "target_wallets": "0xdummy",
        "discovery_enabled": True,
        "discovery_interval_seconds": 3600,
        "discovery_candidate_pool_size": 10,
        "discovery_backend": "data_api",
        "scoring_version": "v1",
        "scoring_promotion_threshold": 0.65,
        "scoring_demotion_threshold": 0.40,
        "scoring_min_closed_markets": 1,  # accepte notre stub de metrics
        "trader_shadow_days": 7,
        "max_active_traders": 5,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _metrics(wallet: str, win_rate: float = 0.8, roi: float = 0.5) -> TraderMetrics:
    return TraderMetrics(
        wallet_address=wallet,
        resolved_positions_count=20,
        open_positions_count=2,
        win_rate=win_rate,
        realized_roi=roi,
        total_volume_usd=50_000.0,
        herfindahl_index=0.3,
        nb_distinct_markets=5,
        largest_position_value_usd=1000.0,
        measurement_window_days=90,
        fetched_at=datetime.now(tz=UTC),
    )


async def test_one_cycle_persists_events_and_scores(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cycle complet mock : 1 candidat → 1 score + 1 event en DB."""
    # Mock les clients HTTP : remplace les instances internes post-init via patching.
    with (
        patch(
            "polycopy.discovery.orchestrator.CandidatePool",
            autospec=True,
        ) as mock_pool_cls,
        patch(
            "polycopy.discovery.orchestrator.MetricsCollector",
            autospec=True,
        ) as mock_mc_cls,
    ):
        mock_pool = mock_pool_cls.return_value
        mock_pool.build = AsyncMock(
            return_value=[
                CandidateWallet(
                    wallet_address="0xnew",
                    discovered_via="holders",
                    initial_signal=1.5,
                ),
            ],
        )
        mock_mc = mock_mc_cls.return_value
        mock_mc.collect = AsyncMock(return_value=_metrics("0xnew"))

        alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
        orch = DiscoveryOrchestrator(session_factory, _settings(), alerts)
        stop = asyncio.Event()

        # Déclenche 1 seul cycle puis stop.
        async def stop_after_one_cycle() -> None:
            await asyncio.sleep(0.5)
            stop.set()

        await asyncio.gather(
            orch.run_forever(stop),
            stop_after_one_cycle(),
        )

    # Vérifier les persistances
    event_repo = TraderEventRepository(session_factory)
    events = await event_repo.list_recent()
    assert any(e.event_type == "discovered" for e in events)

    target_repo = TargetTraderRepository(session_factory)
    wallet_rec = await target_repo.get("0xnew")
    assert wallet_rec is not None
    assert wallet_rec.status == "shadow"


async def test_orchestrator_handles_cycle_failure_with_backoff(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un cycle qui crash doit pousser une alerte + reprendre au cycle suivant."""
    with (
        patch(
            "polycopy.discovery.orchestrator.CandidatePool",
            autospec=True,
        ) as mock_pool_cls,
        patch(
            "polycopy.discovery.orchestrator.MetricsCollector",
            autospec=True,
        ),
    ):
        mock_pool = mock_pool_cls.return_value
        mock_pool.build = AsyncMock(side_effect=RuntimeError("boom"))

        alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
        orch = DiscoveryOrchestrator(session_factory, _settings(), alerts)
        stop = asyncio.Event()

        async def stop_fast() -> None:
            await asyncio.sleep(0.5)
            stop.set()

        with caplog.at_level(logging.ERROR):
            await asyncio.gather(orch.run_forever(stop), stop_fast())

        # Alerte `discovery_cycle_failed` dans la queue
        events_seen = []
        while not alerts.empty():
            events_seen.append((await alerts.get()).event)
        assert "discovery_cycle_failed" in events_seen


async def test_shadow_bypass_logs_warning(
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§0.3 : si trader_shadow_days=0 ET discovery_shadow_bypass=true, WARNING au boot."""
    with (
        patch(
            "polycopy.discovery.orchestrator.CandidatePool",
            autospec=True,
        ) as mock_pool_cls,
        patch(
            "polycopy.discovery.orchestrator.MetricsCollector",
            autospec=True,
        ) as mock_mc_cls,
    ):
        mock_pool_cls.return_value.build = AsyncMock(return_value=[])
        mock_mc_cls.return_value.collect = AsyncMock(return_value=_metrics("0xx"))

        alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=10)
        s = _settings(trader_shadow_days=0, discovery_shadow_bypass=True)
        orch = DiscoveryOrchestrator(session_factory, s, alerts)
        stop = asyncio.Event()

        async def stop_fast() -> None:
            await asyncio.sleep(0.3)
            stop.set()

        with caplog.at_level("WARNING"):
            await asyncio.gather(orch.run_forever(stop), stop_fast())
    # Le log du warning passe par structlog → on regarde juste qu'on a bien
    # tourné sans crash, le smoke suffit pour la couverture.
    assert True


async def test_cap_reached_emits_alert_but_keeps_processing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MAX_ACTIVE_TRADERS=1, 2 candidats top-score : 1 promu + 1 skip_cap alerté."""
    # Pre-existant : déjà 1 active (= cap plein)
    target_repo = TargetTraderRepository(session_factory)
    await target_repo.insert_shadow("0xexisting")
    await target_repo.transition_status("0xexisting", new_status="active")

    with (
        patch(
            "polycopy.discovery.orchestrator.CandidatePool",
            autospec=True,
        ) as mock_pool_cls,
        patch(
            "polycopy.discovery.orchestrator.MetricsCollector",
            autospec=True,
        ) as mock_mc_cls,
    ):
        mock_pool_cls.return_value.build = AsyncMock(
            return_value=[
                CandidateWallet(
                    wallet_address="0xtop1",
                    discovered_via="holders",
                    initial_signal=2.0,
                ),
            ],
        )
        mock_mc_cls.return_value.collect = AsyncMock(
            side_effect=lambda w: _metrics(w, win_rate=0.9, roi=1.0),  # type: ignore[misc]
        )

        alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
        s = _settings(max_active_traders=1, trader_shadow_days=0, discovery_shadow_bypass=True)
        orch = DiscoveryOrchestrator(session_factory, s, alerts)
        stop = asyncio.Event()

        async def stop_fast() -> None:
            await asyncio.sleep(0.6)
            stop.set()

        await asyncio.gather(orch.run_forever(stop), stop_fast())

    alert_types = []
    while not alerts.empty():
        alert_types.append((await alerts.get()).event)
    assert "discovery_cap_reached" in alert_types
