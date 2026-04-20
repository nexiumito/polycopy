"""Tests ``cli/boot.py::build_orchestrators`` + ``--force-resume`` (M12_bis Phase D)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.cli.boot import build_orchestrators
from polycopy.config import Settings
from polycopy.dashboard.orchestrator import DashboardOrchestrator
from polycopy.discovery.orchestrator import DiscoveryOrchestrator
from polycopy.executor.orchestrator import ExecutorOrchestrator
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.orchestrator import MonitoringOrchestrator
from polycopy.remote_control.orchestrator import RemoteControlOrchestrator
from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.latency_purge_scheduler import LatencyPurgeScheduler
from polycopy.strategy.dtos import OrderApproved
from polycopy.strategy.orchestrator import StrategyOrchestrator
from polycopy.watcher.orchestrator import WatcherOrchestrator

_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _fake_sf() -> async_sessionmaker[AsyncSession]:
    """Session factory fake — les orchestrateurs stockent sans I/O au __init__."""
    return cast("async_sessionmaker[AsyncSession]", MagicMock())


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def _queues() -> tuple[
    asyncio.Queue[DetectedTradeDTO], asyncio.Queue[OrderApproved], asyncio.Queue[Alert]
]:
    return asyncio.Queue(), asyncio.Queue(), asyncio.Queue()


# ===========================================================================
# Mode "normal" — liste complète (non-régression M12)
# ===========================================================================


def test_normal_mode_minimal_settings_4_core_orchestrators() -> None:
    """Defaults : Watcher + Strategy + Executor + Monitoring (opt-ins off)."""
    s = _settings()
    detected, approved, alerts = _queues()
    orchestrators = build_orchestrators(
        session_factory=_fake_sf(),
        settings=s,
        detected_trades_queue=detected,
        approved_orders_queue=approved,
        alerts_queue=alerts,
    )
    types = [type(o) for o in orchestrators]
    assert WatcherOrchestrator in types
    assert StrategyOrchestrator in types
    assert ExecutorOrchestrator in types
    assert MonitoringOrchestrator in types
    assert DashboardOrchestrator not in types
    assert DiscoveryOrchestrator not in types
    assert LatencyPurgeScheduler in types  # default True
    assert RemoteControlOrchestrator not in types


def test_normal_mode_monitoring_has_paused_false() -> None:
    s = _settings()
    detected, approved, alerts = _queues()
    orchestrators = build_orchestrators(
        session_factory=_fake_sf(),
        settings=s,
        detected_trades_queue=detected,
        approved_orders_queue=approved,
        alerts_queue=alerts,
    )
    monitoring = next(o for o in orchestrators if isinstance(o, MonitoringOrchestrator))
    assert monitoring._paused is False  # noqa: SLF001


def test_normal_mode_with_all_optional_flags_on() -> None:
    s = _settings(
        dashboard_enabled=True,
        discovery_enabled=True,
        latency_instrumentation_enabled=True,
    )
    detected, approved, alerts = _queues()
    orchestrators = build_orchestrators(
        session_factory=_fake_sf(),
        settings=s,
        detected_trades_queue=detected,
        approved_orders_queue=approved,
        alerts_queue=alerts,
    )
    types = [type(o) for o in orchestrators]
    assert DashboardOrchestrator in types
    assert DiscoveryOrchestrator in types
    assert LatencyPurgeScheduler in types


# ===========================================================================
# Mode "paused" — liste réduite (M12_bis §4.2)
# ===========================================================================


def test_paused_mode_excludes_trading_orchestrators() -> None:
    s = _settings()
    detected, approved, alerts = _queues()
    orchestrators = build_orchestrators(
        session_factory=_fake_sf(),
        settings=s,
        detected_trades_queue=detected,
        approved_orders_queue=approved,
        alerts_queue=alerts,
        mode="paused",
    )
    types = [type(o) for o in orchestrators]
    # Exclus en paused.
    for excluded in (
        WatcherOrchestrator,
        StrategyOrchestrator,
        ExecutorOrchestrator,
        DiscoveryOrchestrator,
        LatencyPurgeScheduler,
    ):
        assert excluded not in types, f"{excluded.__name__} should NOT be in paused"
    # Monitoring reste (réduit via flag paused=True).
    assert MonitoringOrchestrator in types


def test_paused_mode_monitoring_has_paused_true() -> None:
    s = _settings()
    detected, approved, alerts = _queues()
    orchestrators = build_orchestrators(
        session_factory=_fake_sf(),
        settings=s,
        detected_trades_queue=detected,
        approved_orders_queue=approved,
        alerts_queue=alerts,
        mode="paused",
    )
    monitoring = next(o for o in orchestrators if isinstance(o, MonitoringOrchestrator))
    assert monitoring._paused is True  # noqa: SLF001


def test_paused_mode_includes_dashboard_if_enabled() -> None:
    s = _settings(dashboard_enabled=True)
    detected, approved, alerts = _queues()
    orchestrators = build_orchestrators(
        session_factory=_fake_sf(),
        settings=s,
        detected_trades_queue=detected,
        approved_orders_queue=approved,
        alerts_queue=alerts,
        mode="paused",
    )
    assert any(isinstance(o, DashboardOrchestrator) for o in orchestrators)


def test_paused_mode_excludes_dashboard_if_disabled() -> None:
    s = _settings(dashboard_enabled=False)
    detected, approved, alerts = _queues()
    orchestrators = build_orchestrators(
        session_factory=_fake_sf(),
        settings=s,
        detected_trades_queue=detected,
        approved_orders_queue=approved,
        alerts_queue=alerts,
        mode="paused",
    )
    assert not any(isinstance(o, DashboardOrchestrator) for o in orchestrators)


def test_paused_mode_excludes_executor_even_when_live_without_keys(
    tmp_path: Path,
) -> None:
    """Important : en paused, ExecutorOrchestrator n'est PAS instancié —
    donc ``RuntimeError`` du garde-fou M3 (LIVE sans clés) ne peut pas
    se produire, même si `execution_mode=live` dans les settings.
    """
    _ = tmp_path  # pour signal au lint que c'est intentionnel
    s = _settings(execution_mode="live")
    detected, approved, alerts = _queues()
    # Normalement en mode normal, ce call leverait.
    # En paused, ça passe sans exception.
    orchestrators = build_orchestrators(
        session_factory=_fake_sf(),
        settings=s,
        detected_trades_queue=detected,
        approved_orders_queue=approved,
        alerts_queue=alerts,
        mode="paused",
    )
    assert not any(isinstance(o, ExecutorOrchestrator) for o in orchestrators)


def test_paused_mode_count_limits_minimal_to_3() -> None:
    """En paused avec tous les opt-ins activés, max 3 orchestrateurs :
    Monitoring + Dashboard + RemoteControl.
    """
    s = _settings(
        dashboard_enabled=True,
        discovery_enabled=True,
        latency_instrumentation_enabled=True,
        remote_control_enabled=True,
        remote_control_totp_secret=_TOTP_SECRET,
        remote_control_tailscale_ip_override="100.64.0.1",
    )
    detected, approved, alerts = _queues()
    orchestrators = build_orchestrators(
        session_factory=_fake_sf(),
        settings=s,
        detected_trades_queue=detected,
        approved_orders_queue=approved,
        alerts_queue=alerts,
        mode="paused",
    )
    assert len(orchestrators) == 3  # Monitoring + Dashboard + RemoteControl
