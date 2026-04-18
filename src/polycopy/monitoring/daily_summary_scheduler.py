"""Scheduler du résumé quotidien Telegram (M7 §2.2 + §8.4).

Calcule le prochain tick (`hour` local dans `tz`), attend avec stop_event, puis
collecte l'agrégat 24 h et envoie ``daily_summary.md.j2``. Recalcul à chaque
itération → DST handled par ``zoneinfo``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.monitoring.alert_dispatcher import AlertDispatcher
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.daily_summary_queries import collect_daily_summary_context
from polycopy.monitoring.telegram_client import TelegramClient

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


def compute_next_summary_at(now: datetime, hour: int, tz: ZoneInfo) -> datetime:
    """Retourne le prochain datetime UTC pour ``hour:00`` dans ``tz``.

    - Si l'instant cible aujourd'hui est déjà passé → lendemain.
    - Robuste aux transitions DST car on repasse par ``astimezone(tz)`` puis
      ``astimezone(UTC)`` (``zoneinfo`` gère la disambiguation).
    """
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local_now = now_utc.astimezone(tz)
    target_local = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target_local <= local_now:
        target_local = target_local + timedelta(days=1)
    return target_local.astimezone(UTC)


class DailySummaryScheduler:
    """Envoie ``daily_summary.md.j2`` chaque jour à ``hour:00`` dans la TZ."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        telegram_client: TelegramClient,
        renderer: AlertRenderer,
        settings: Settings,
        dispatcher: AlertDispatcher,
    ) -> None:
        self._sf = session_factory
        self._telegram = telegram_client
        self._renderer = renderer
        self._settings = settings
        self._dispatcher = dispatcher
        self._hour = settings.tg_daily_summary_hour
        self._tz = ZoneInfo(settings.tg_daily_summary_timezone)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self._telegram.enabled:
            log.info("telegram_daily_summary_skipped", reason="telegram_disabled")
            return
        log.info(
            "daily_summary_scheduler_started",
            hour=self._hour,
            tz=self._settings.tg_daily_summary_timezone,
        )
        while not stop_event.is_set():
            now = datetime.now(tz=UTC)
            next_at = compute_next_summary_at(now, self._hour, self._tz)
            delta_seconds = max(1.0, (next_at - now).total_seconds())
            log.debug(
                "daily_summary_waiting",
                next_at=next_at.isoformat(),
                delta_seconds=delta_seconds,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delta_seconds)
                break
            except TimeoutError:
                pass
            if stop_event.is_set():
                break
            try:
                await self._send_summary()
            except Exception:
                log.exception("telegram_daily_summary_failed")
        log.info("daily_summary_scheduler_stopped")

    async def _send_summary(self) -> None:
        now = datetime.now(tz=UTC)
        since = now - timedelta(hours=24)
        date_human = now.astimezone(self._tz).strftime("%Y-%m-%d")
        ctx = await collect_daily_summary_context(
            self._sf,
            self._settings,
            since,
            date_human=date_human,
            alerts_counts=self._dispatcher.counts_since_boot,
        )
        body = self._renderer.render_daily_summary(ctx)
        ok = await self._telegram.send(body)
        if ok:
            log.info("telegram_daily_summary_sent", date=date_human)
        else:
            log.warning("telegram_daily_summary_send_failed", date=date_human)
