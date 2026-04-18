"""Consommateur de la queue ``alerts_queue`` : cooldown + POST Telegram."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from polycopy.monitoring.dtos import Alert, AlertLevel
from polycopy.monitoring.telegram_client import TelegramClient

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_QUEUE_GET_TIMEOUT_SECONDS: float = 1.0

_LEVEL_EMOJI: dict[AlertLevel, str] = {
    "INFO": "🟢",
    "WARNING": "🟡",
    "ERROR": "🔴",
    "CRITICAL": "🚨",
}


class AlertDispatcher:
    """Drain la queue alertes vers Telegram avec cooldown par ``cooldown_key``.

    Pas d'écriture DB à M4 (logs structlog + envoi Telegram uniquement). Le
    cooldown est *in-memory* (reset au boot, rate-limit best-effort).
    """

    def __init__(
        self,
        queue: asyncio.Queue[Alert],
        telegram_client: TelegramClient,
        settings: Settings,
    ) -> None:
        self._queue = queue
        self._telegram = telegram_client
        self._cooldown_seconds = settings.alert_cooldown_seconds
        self._last_sent: dict[str, datetime] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : drain la queue jusqu'à ``stop_event.set()``."""
        log.info("alert_dispatcher_started", telegram_enabled=self._telegram.enabled)
        while not stop_event.is_set():
            try:
                alert = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=_QUEUE_GET_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            await self._handle(alert)
        log.info("alert_dispatcher_stopped")

    async def _handle(self, alert: Alert) -> None:
        """Applique cooldown + envoi Telegram pour une alerte unique."""
        if alert.cooldown_key is not None:
            now = self._now()
            last = self._last_sent.get(alert.cooldown_key)
            if last is not None and (now - last).total_seconds() < self._cooldown_seconds:
                log.debug(
                    "alert_throttled",
                    cooldown_key=alert.cooldown_key,
                    alert_event=alert.event,
                )
                return
            self._last_sent[alert.cooldown_key] = now

        emoji = _LEVEL_EMOJI.get(alert.level, "")
        formatted = f"{emoji} *[{alert.event}]*\n{alert.body}"
        sent = await self._telegram.send(formatted)
        if sent:
            log.info("alert_sent", alert_event=alert.event, level=alert.level)
        else:
            log.warning("alert_send_failed", alert_event=alert.event, level=alert.level)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)
