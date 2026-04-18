"""Scheduler léger pour purger ``trade_latency_samples`` au-delà de N jours.

M11 : boucle quotidienne co-lancée dans le TaskGroup principal. La purge au
boot est faite AVANT (dans `cli/runner.py`) — ce scheduler gère uniquement
les passes ultérieures 24h/24h.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from polycopy.storage.repositories import TradeLatencyRepository

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_DAILY_INTERVAL_SECONDS: float = 24 * 3600.0


class LatencyPurgeScheduler:
    """Boucle asyncio : purge ``trade_latency_samples`` toutes les 24h."""

    def __init__(
        self,
        repo: TradeLatencyRepository,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._settings = settings

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Attends 24h, purge, recommence. Sort proprement à ``stop_event``."""
        log.info(
            "latency_purge_scheduler_started",
            retention_days=self._settings.latency_sample_retention_days,
        )
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=_DAILY_INTERVAL_SECONDS,
                )
                break  # stop_event set
            except TimeoutError:
                pass
            try:
                deleted = await self._repo.purge_older_than(
                    days=self._settings.latency_sample_retention_days,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("latency_purge_error")
                continue
            log.info(
                "latency_purge_completed",
                deleted=deleted,
                retention_days=self._settings.latency_sample_retention_days,
            )
        log.info("latency_purge_scheduler_stopped")
