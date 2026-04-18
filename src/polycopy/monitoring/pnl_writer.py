"""Writer de snapshots PnL + kill switch (M4).

Calcule périodiquement ``total_usdc`` via ``WalletStateReader`` (M3), persiste
un snapshot en DB, et déclenche le kill switch si le drawdown dépasse le seuil
``KILL_SWITCH_DRAWDOWN_PCT`` **uniquement en mode réel** (jamais en dry-run).
Voir ``specs/M4-monitoring.md`` §2.3.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.monitoring.dtos import Alert
from polycopy.storage.dtos import PnlSnapshotDTO
from polycopy.storage.repositories import PnlSnapshotRepository

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_BACKOFF_SECONDS: float = 30.0
_DRAWDOWN_WARNING_RATIO: float = 0.75


class PnlSnapshotWriter:
    """Écrit un ``PnlSnapshot`` toutes les ``pnl_snapshot_interval_seconds``."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        wallet_state_reader: WalletStateReader,
        alerts_queue: asyncio.Queue[Alert],
    ) -> None:
        self._repo = PnlSnapshotRepository(session_factory)
        self._settings = settings
        self._reader = wallet_state_reader
        self._alerts = alerts_queue

    async def run(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : snapshot → kill-switch check → sleep."""
        interval = self._settings.pnl_snapshot_interval_seconds
        log.info("pnl_snapshot_writer_started", interval=interval, dry_run=self._settings.dry_run)
        while not stop_event.is_set():
            try:
                await self._tick(stop_event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pnl_writer_error")
                await self._sleep(stop_event, _BACKOFF_SECONDS)
                continue
            await self._sleep(stop_event, interval)
        log.info("pnl_snapshot_writer_stopped")

    async def _tick(self, stop_event: asyncio.Event) -> None:
        """Une itération : fetch état wallet, calcule drawdown, persist, alertes."""
        state = await self._reader.get_state()
        total = state.total_position_value_usd + state.available_capital_usd

        # Le drawdown all-time-high est calculé sur la même "bucket" (real vs dry)
        # pour éviter de mélanger des stubs et des vraies valeurs.
        only_real = not self._settings.dry_run
        max_ever = await self._repo.get_max_total_usdc(only_real=only_real)
        drawdown_pct = self._compute_drawdown_pct(max_ever, total)

        dto = PnlSnapshotDTO(
            total_usdc=total,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=drawdown_pct,
            open_positions_count=state.open_positions_count,
            cash_pnl_total=None,
            is_dry_run=self._settings.dry_run,
        )
        await self._repo.insert(dto)
        log.info(
            "pnl_snapshot_written",
            total_usdc=total,
            drawdown_pct=drawdown_pct,
            open_positions_count=state.open_positions_count,
            is_dry_run=self._settings.dry_run,
        )
        await self._maybe_trigger_alerts(total, drawdown_pct, stop_event)

    async def _maybe_trigger_alerts(
        self,
        total: float,
        drawdown_pct: float,
        stop_event: asyncio.Event,
    ) -> None:
        """Kill switch + drawdown warning. **Jamais en dry-run** (spec §2.3)."""
        if self._settings.dry_run:
            return
        threshold = self._settings.kill_switch_drawdown_pct
        if drawdown_pct >= threshold:
            log.error(
                "kill_switch_triggered",
                drawdown_pct=drawdown_pct,
                threshold=threshold,
                total_usdc=total,
            )
            self._push_alert(
                Alert(
                    level="CRITICAL",
                    event="kill_switch_triggered",
                    body=(
                        f"Kill switch — drawdown {drawdown_pct:.2f}% "
                        f">= seuil {threshold:.2f}%. "
                        f"total_usdc={total:.2f}. Stop du bot."
                    ),
                    cooldown_key="kill_switch",
                ),
            )
            stop_event.set()
            return
        warning_threshold = _DRAWDOWN_WARNING_RATIO * threshold
        if drawdown_pct >= warning_threshold:
            self._push_alert(
                Alert(
                    level="WARNING",
                    event="pnl_snapshot_drawdown",
                    body=(
                        f"Drawdown warning — {drawdown_pct:.2f}% "
                        f"(seuil kill switch {threshold:.2f}%)."
                    ),
                    cooldown_key="drawdown_warning",
                ),
            )

    def _push_alert(self, alert: Alert) -> None:
        try:
            self._alerts.put_nowait(alert)
        except asyncio.QueueFull:
            log.warning("alerts_queue_full", event=alert.event)

    @staticmethod
    def _compute_drawdown_pct(max_ever: float | None, total: float) -> float:
        """Retourne le drawdown en % (0 si pas d'historique ou max nul)."""
        if max_ever is None or max_ever <= 0:
            return 0.0
        if total >= max_ever:
            return 0.0
        return (max_ever - total) / max_ever * 100.0

    @staticmethod
    async def _sleep(stop_event: asyncio.Event, seconds: float) -> None:
        """Sleep interruptible : retourne si ``stop_event`` est set avant timeout."""
        with suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
