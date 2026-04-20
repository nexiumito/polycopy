"""Boot helpers — construction des orchestrateurs du TaskGroup top-level.

Extrait de ``cli/runner.py::_async_main`` (M12_bis Phase D) pour permettre :
1. la bifurcation ``normal``/``paused`` pilotée par le sentinel
   (Phase D commit #2).
2. des tests unitaires qui inspectent la liste retournée sans lancer
   ``asyncio.run`` ni un vrai TaskGroup.

Invariant : chaque orchestrateur expose ``async def run_forever(stop_event)``
— vérifié implicitement au runtime par le ``Protocol`` ``_HasRunForever``.

Zéro changement de comportement vs M9..M12 dans ce commit : la liste
est construite dans le même ordre, avec les mêmes opt-ins
(``DASHBOARD_ENABLED``, ``DISCOVERY_ENABLED``,
``LATENCY_INSTRUMENTATION_ENABLED``, ``REMOTE_CONTROL_ENABLED``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, Protocol

from polycopy.dashboard.orchestrator import DashboardOrchestrator
from polycopy.discovery.orchestrator import DiscoveryOrchestrator
from polycopy.executor.orchestrator import ExecutorOrchestrator
from polycopy.monitoring.orchestrator import MonitoringOrchestrator
from polycopy.remote_control.orchestrator import RemoteControlOrchestrator
from polycopy.storage.latency_purge_scheduler import LatencyPurgeScheduler
from polycopy.storage.repositories import TradeLatencyRepository
from polycopy.strategy.orchestrator import StrategyOrchestrator
from polycopy.watcher.orchestrator import WatcherOrchestrator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from polycopy.config import Settings
    from polycopy.monitoring.dtos import Alert
    from polycopy.storage.dtos import DetectedTradeDTO
    from polycopy.strategy.dtos import OrderApproved


class HasRunForever(Protocol):
    """Interface minimale des orchestrateurs consommés par le TaskGroup."""

    async def run_forever(self, stop_event: asyncio.Event) -> None: ...


BootMode = Literal["normal", "paused"]


def build_orchestrators(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    detected_trades_queue: asyncio.Queue[DetectedTradeDTO],
    approved_orders_queue: asyncio.Queue[OrderApproved],
    alerts_queue: asyncio.Queue[Alert],
    mode: BootMode = "normal",
) -> list[HasRunForever]:
    """Construit la liste ordonnée des orchestrateurs selon ``mode``.

    **Mode ``normal``** (spec §4.2) — stack complet :
    1. ``WatcherOrchestrator`` — détection trades on-chain.
    2. ``StrategyOrchestrator`` — filtres + sizing + risk.
    3. ``ExecutorOrchestrator`` — peut ``RuntimeError`` au __init__ si
       ``execution_mode=live`` et clés CLOB absentes (garde-fou M3).
    4. ``MonitoringOrchestrator`` — PnL + alerts + Telegram M7.
    5. ``DashboardOrchestrator`` (opt-in ``DASHBOARD_ENABLED``).
    6. ``DiscoveryOrchestrator`` (opt-in ``DISCOVERY_ENABLED``).
    7. ``LatencyPurgeScheduler`` (opt-in ``LATENCY_INSTRUMENTATION_ENABLED``).
    8. ``RemoteControlOrchestrator`` (opt-in ``REMOTE_CONTROL_ENABLED``) —
       instancié en dernier mais **avant** le TaskGroup pour que
       ``RemoteControlBootError`` (Tailscale absent, etc.) remonte clair.

    **Mode ``paused``** (M12_bis Phase D §4.2) — stack réduit :
    - ``MonitoringOrchestrator`` avec ``paused=True`` (PnlSnapshotWriter
      + DailySummaryScheduler désactivés, heartbeat conservé).
    - ``DashboardOrchestrator`` (si enabled) — utile pour inspecter PnL
      + traders avant ``/resume``.
    - ``RemoteControlOrchestrator`` (si enabled) — **mandatory** pour
      pouvoir servir ``/resume`` depuis le téléphone.
    - Watcher/Strategy/Executor/Discovery/LatencyPurge **exclus** (pas
      de trading en paused, pas de re-trigger kill switch).
    """
    if mode == "paused":
        paused_list: list[HasRunForever] = [
            MonitoringOrchestrator(session_factory, settings, alerts_queue, paused=True),
        ]
        if settings.dashboard_enabled:
            paused_list.append(DashboardOrchestrator(session_factory, settings))
        if settings.remote_control_enabled:
            paused_list.append(
                RemoteControlOrchestrator(settings, alerts_queue=alerts_queue),
            )
        return paused_list

    orchestrators: list[HasRunForever] = [
        WatcherOrchestrator(
            session_factory,
            settings,
            detected_trades_queue=detected_trades_queue,
            alerts_queue=alerts_queue,
        ),
        StrategyOrchestrator(
            session_factory,
            settings,
            detected_trades_queue=detected_trades_queue,
            approved_orders_queue=approved_orders_queue,
            alerts_queue=alerts_queue,
        ),
        ExecutorOrchestrator(
            session_factory,
            settings,
            approved_orders_queue=approved_orders_queue,
            alerts_queue=alerts_queue,
        ),
        MonitoringOrchestrator(session_factory, settings, alerts_queue),
    ]
    if settings.dashboard_enabled:
        orchestrators.append(DashboardOrchestrator(session_factory, settings))
    if settings.discovery_enabled:
        orchestrators.append(
            DiscoveryOrchestrator(session_factory, settings, alerts_queue),
        )
    if settings.latency_instrumentation_enabled:
        orchestrators.append(
            LatencyPurgeScheduler(TradeLatencyRepository(session_factory), settings),
        )
    if settings.remote_control_enabled:
        orchestrators.append(
            RemoteControlOrchestrator(settings, alerts_queue=alerts_queue),
        )
    return orchestrators
