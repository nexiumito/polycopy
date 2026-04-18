"""Orchestrateur Monitoring (M4 + M7).

M4 : ``PnlSnapshotWriter`` + ``AlertDispatcher`` co-lancés dans un TaskGroup.

M7 ajoute (conditionnel) :
- ``StartupNotifier`` : one-shot au boot.
- ``HeartbeatScheduler`` : boucle périodique.
- ``DailySummaryScheduler`` : scheduler TZ-aware.

Tous partagent un même ``TelegramClient`` (1 httpx pool) + un ``AlertRenderer``
+ une ``AlertDigestWindow``. ``AlertDispatcher`` M4 est étendu par composition.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.monitoring.alert_digest import AlertDigestWindow
from polycopy.monitoring.alert_dispatcher import AlertDispatcher
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.daily_summary_scheduler import DailySummaryScheduler
from polycopy.monitoring.dtos import Alert, ShutdownContext
from polycopy.monitoring.heartbeat_scheduler import HeartbeatScheduler
from polycopy.monitoring.md_escape import humanize_duration
from polycopy.monitoring.pnl_writer import PnlSnapshotWriter
from polycopy.monitoring.startup_notifier import StartupNotifier
from polycopy.monitoring.telegram_client import TelegramClient

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_SHUTDOWN_SEND_TIMEOUT_SECONDS: float = 3.0


class MonitoringOrchestrator:
    """Co-lance writer + dispatcher + (startup, heartbeat, daily) selon settings."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert],
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._alerts = alerts_queue

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : tous les schedulers jusqu'à ``stop_event.set()``."""
        boot_at = datetime.now(tz=UTC)
        async with httpx.AsyncClient() as http_client:
            wallet_reader = WalletStateReader(http_client, self._settings)
            telegram = TelegramClient(http_client, self._settings)
            if telegram.enabled:
                log.info("telegram_enabled")
            else:
                log.warning("telegram_disabled")
            renderer = AlertRenderer()
            digest = AlertDigestWindow(
                window_seconds=self._settings.telegram_digest_window_minutes * 60,
                threshold=self._settings.telegram_digest_threshold,
            )
            dispatcher = AlertDispatcher(
                self._alerts,
                telegram,
                self._settings,
                renderer=renderer,
                digest=digest,
            )
            writer = PnlSnapshotWriter(
                self._session_factory,
                self._settings,
                wallet_reader,
                self._alerts,
            )
            log.info(
                "monitoring_started",
                telegram_enabled=telegram.enabled,
                pnl_interval=self._settings.pnl_snapshot_interval_seconds,
                startup_message=self._settings.telegram_startup_message,
                heartbeat=self._settings.telegram_heartbeat_enabled,
                daily_summary=self._settings.telegram_daily_summary,
            )

            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(writer.run(stop_event))
                    tg.create_task(dispatcher.run(stop_event))
                    if self._settings.telegram_startup_message:
                        startup = StartupNotifier(
                            self._session_factory,
                            telegram,
                            renderer,
                            self._settings,
                        )
                        tg.create_task(startup.send_once(stop_event))
                    if self._settings.telegram_heartbeat_enabled:
                        heartbeat = HeartbeatScheduler(
                            self._session_factory,
                            telegram,
                            renderer,
                            self._settings,
                            dispatcher,
                        )
                        tg.create_task(heartbeat.run(stop_event))
                    if self._settings.telegram_daily_summary:
                        daily = DailySummaryScheduler(
                            self._session_factory,
                            telegram,
                            renderer,
                            self._settings,
                            dispatcher,
                        )
                        tg.create_task(daily.run(stop_event))
            except* asyncio.CancelledError:
                pass

            if self._settings.telegram_startup_message and telegram.enabled:
                await self._send_shutdown_message(telegram, renderer, boot_at)

        log.info("monitoring_stopped")

    async def _send_shutdown_message(
        self,
        telegram: TelegramClient,
        renderer: AlertRenderer,
        boot_at: datetime,
    ) -> None:
        duration = (datetime.now(tz=UTC) - boot_at).total_seconds()
        try:
            version = importlib_metadata.version("polycopy")
        except importlib_metadata.PackageNotFoundError:
            version = "0.0.0"
        ctx = ShutdownContext(
            duration_human=humanize_duration(duration),
            version=version,
        )
        body = renderer.render_shutdown(ctx)
        try:
            await asyncio.wait_for(
                telegram.send(body),
                timeout=_SHUTDOWN_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.warning("telegram_shutdown_timeout")
        except Exception:
            log.exception("telegram_shutdown_failed")
