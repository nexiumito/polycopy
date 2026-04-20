"""Heartbeat périodique Telegram (M7 §8.3).

Boucle simple : ``asyncio.wait_for(stop_event.wait(), timeout=H*3600)``.
Au tick, on vérifie qu'aucune alerte critique n'a été émise récemment
(``AlertDispatcher.has_recent_critical``) — sinon on saute le heartbeat pour
éviter la dissonance "kill_switch puis polycopy actif 5 min plus tard".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.monitoring.alert_dispatcher import AlertDispatcher
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import HeartbeatContext
from polycopy.monitoring.md_escape import humanize_duration
from polycopy.monitoring.telegram_client import TelegramClient
from polycopy.storage.models import MyPosition, TargetTrader

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


class HeartbeatScheduler:
    """Envoie ``heartbeat.md.j2`` toutes les N heures tant que le bot tourne."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        telegram_client: TelegramClient,
        renderer: AlertRenderer,
        settings: Settings,
        dispatcher: AlertDispatcher,
        *,
        paused: bool = False,
    ) -> None:
        self._sf = session_factory
        self._telegram = telegram_client
        self._renderer = renderer
        self._settings = settings
        self._dispatcher = dispatcher
        self._interval_seconds: int = settings.telegram_heartbeat_interval_hours * 3600
        self._boot_at: datetime = datetime.now(tz=UTC)
        self._count: int = 0
        self._paused = paused

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self._telegram.enabled:
            log.info("telegram_heartbeat_skipped", reason="telegram_disabled")
            return
        log.info(
            "heartbeat_scheduler_started",
            interval_hours=self._settings.telegram_heartbeat_interval_hours,
        )
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._interval_seconds,
                )
                break  # stop_event set → sortie propre
            except TimeoutError:
                pass
            if stop_event.is_set():
                break
            self._count += 1
            try:
                ctx = await self._build_context()
            except Exception:
                log.exception("telegram_heartbeat_context_failed")
                continue
            if ctx.critical_alerts_in_window > 0:
                log.info(
                    "telegram_heartbeat_skipped",
                    reason="recent_critical",
                    index=self._count,
                )
                continue
            try:
                body = self._renderer.render_heartbeat(ctx)
                ok = await self._telegram.send(body)
            except Exception:
                log.exception("telegram_heartbeat_failed", index=self._count)
                continue
            if ok:
                log.info("telegram_heartbeat_sent", index=self._count)
            else:
                log.warning("telegram_heartbeat_send_failed", index=self._count)
        log.info("heartbeat_scheduler_stopped")

    async def _build_context(self) -> HeartbeatContext:
        uptime_seconds = (datetime.now(tz=UTC) - self._boot_at).total_seconds()
        watcher_count = await self._count_active_wallets()
        positions_open = await self._count_open_positions()
        window = timedelta(seconds=self._interval_seconds // 2)
        recent_critical = 1 if self._dispatcher.has_recent_critical(window) else 0
        return HeartbeatContext(
            uptime_human=humanize_duration(uptime_seconds),
            heartbeat_index=self._count,
            watcher_count=watcher_count,
            positions_open=positions_open,
            critical_alerts_in_window=recent_critical,
            paused=self._paused,
        )

    async def _count_active_wallets(self) -> int:
        async with self._sf() as session:
            stmt = select(func.count(TargetTrader.id)).where(
                TargetTrader.status.in_(("active", "pinned")),
            )
            return int((await session.execute(stmt)).scalar_one())

    async def _count_open_positions(self) -> int:
        async with self._sf() as session:
            stmt = select(func.count(MyPosition.id)).where(MyPosition.closed_at.is_(None))
            return int((await session.execute(stmt)).scalar_one())
