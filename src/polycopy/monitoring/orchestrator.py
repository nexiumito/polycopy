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

from polycopy.executor.virtual_wallet_reader import VirtualWalletStateReader
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
from polycopy.remote_control.sentinel import SentinelFile

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_SHUTDOWN_SEND_TIMEOUT_SECONDS: float = 3.0


class MonitoringOrchestrator:
    """Co-lance writer + dispatcher + (startup, heartbeat, daily) selon settings.

    M12_bis Phase D : flag ``paused`` (opt-in au __init__) — quand le runner
    a détecté ``~/.polycopy/halt.flag`` au boot, l'orchestrateur réduit
    son périmètre :
    - ``PnlSnapshotWriter`` : **OFF** (pas de re-trigger kill switch, le
      sentinel est déjà posé).
    - ``DailySummaryScheduler`` : **OFF** (pas de trades ni décisions à
      résumer en paused).
    - ``AlertDispatcher``, ``StartupNotifier``, ``HeartbeatScheduler`` :
      **ON** (nécessaires pour alerter l'utilisateur de l'état paused).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert],
        *,
        paused: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._alerts = alerts_queue
        self._paused = paused

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : tous les schedulers jusqu'à ``stop_event.set()``."""
        boot_at = datetime.now(tz=UTC)
        async with httpx.AsyncClient() as http_client:
            wallet_reader: WalletStateReader | VirtualWalletStateReader
            if self._settings.execution_mode == "dry_run" and self._settings.dry_run_realistic_fill:
                # Lazy import : évite l'import circulaire monitoring↔strategy
                # au load module.
                from polycopy.strategy.clob_read_client import ClobReadClient

                wallet_reader = VirtualWalletStateReader(
                    self._session_factory,
                    ClobReadClient(http_client),
                    self._settings,
                )
                log.info("monitoring_virtual_wallet_reader_enabled")
            else:
                wallet_reader = WalletStateReader(http_client, self._settings)
            telegram = TelegramClient(http_client, self._settings)
            if telegram.enabled:
                log.info("telegram_enabled")
            else:
                log.warning("telegram_disabled")
            renderer = AlertRenderer(
                mode=self._settings.execution_mode,
                machine_id=self._settings.machine_id or "UNKNOWN",
                machine_emoji=self._settings.machine_emoji,
            )
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
            # M12_bis Phase D §4.6 : injection du sentinel → le
            # PnlSnapshotWriter touche halt.flag avant stop_event.set() sur
            # kill switch pour que le respawn superviseur retombe en paused.
            sentinel = SentinelFile(self._settings.remote_control_sentinel_path)
            writer = PnlSnapshotWriter(
                self._session_factory,
                self._settings,
                wallet_reader,
                self._alerts,
                sentinel=sentinel,
            )
            log.info(
                "monitoring_started",
                telegram_enabled=telegram.enabled,
                pnl_interval=self._settings.pnl_snapshot_interval_seconds,
                startup_message=self._settings.telegram_startup_message,
                heartbeat=self._settings.telegram_heartbeat_enabled,
                daily_summary=self._settings.telegram_daily_summary,
                execution_mode=self._settings.execution_mode,
                paused=self._paused,
            )

            try:
                async with asyncio.TaskGroup() as tg:
                    # M12_bis Phase D : en paused, pas de PnlSnapshotWriter
                    # (évite re-trigger kill switch) ni DailySummaryScheduler.
                    if not self._paused:
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
                    if self._settings.telegram_daily_summary and not self._paused:
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
