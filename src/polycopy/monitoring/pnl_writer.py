"""Writer de snapshots PnL + kill switch (M4 / M10).

Calcule périodiquement ``total_usdc`` via ``WalletStateReader`` (M3) ou
``VirtualWalletStateReader`` (M8), persiste un snapshot en DB, et déclenche
le kill switch si le drawdown dépasse ``KILL_SWITCH_DRAWDOWN_PCT``.

**M10** : parité dry-run ↔ live — le kill switch fire à l'identique dans
les 3 modes SIMULATION / DRY_RUN / LIVE. Le dry-run n'est plus silencieux
côté observabilité (inversion invariant M4 §2.3 / spec M10 §3.3).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.executor.exceptions import MidpointUnavailableError
from polycopy.executor.virtual_wallet_reader import VirtualWalletStateReader
from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.monitoring.dtos import Alert
from polycopy.storage.dtos import PnlSnapshotDTO, TraderEventDTO
from polycopy.storage.repositories import (
    MyPositionRepository,
    PnlSnapshotRepository,
    TraderEventRepository,
)

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.remote_control.sentinel import SentinelFile

log = structlog.get_logger(__name__)

_BACKOFF_SECONDS: float = 30.0
_DRAWDOWN_WARNING_RATIO: float = 0.75


class PnlSnapshotWriter:
    """Écrit un ``PnlSnapshot`` toutes les ``pnl_snapshot_interval_seconds``."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        wallet_state_reader: WalletStateReader | VirtualWalletStateReader,
        alerts_queue: asyncio.Queue[Alert],
        *,
        sentinel: SentinelFile | None = None,
        events_repo: TraderEventRepository | None = None,
    ) -> None:
        self._repo = PnlSnapshotRepository(session_factory)
        # M17 MD.6 : agrège `MyPosition.realized_pnl` par mode pour peupler
        # le DTO (au lieu de 0.0 hardcodé — audit H-002).
        self._positions_repo = MyPositionRepository(session_factory)
        self._settings = settings
        self._reader = wallet_state_reader
        self._alerts = alerts_queue
        # M12_bis Phase D : si injecté, le sentinel est touché AVANT
        # `stop_event.set()` sur kill switch. Optionnel pour compat
        # ascendante + tests unitaires qui n'ont pas de filesystem.
        self._sentinel: SentinelFile | None = sentinel
        # M17 MD.7 : si injecté, le kill switch écrit un TraderEvent
        # `kill_switch` (system-level, wallet_address=None) AVANT push_alert
        # AVANT touch sentinel AVANT stop_event.set() — ordre strict.
        # Optionnel pour compat ascendante + tests existants M4..M16.
        self._events_repo: TraderEventRepository | None = events_repo

    async def run(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : snapshot → kill-switch check → sleep."""
        interval = self._settings.pnl_snapshot_interval_seconds
        log.info(
            "pnl_snapshot_writer_started",
            interval=interval,
            mode=self._settings.execution_mode,
        )
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
        try:
            state = await self._reader.get_state()
        except MidpointUnavailableError as exc:
            # M17 MD.4 : panne CLOB midpoint prolongée → skip ce tick plutôt
            # que persister un total_usdc creux (qui corromprait la baseline
            # max et déclencherait un drawdown factice — audit C-004).
            log.warning(
                "pnl_snapshot_skipped_midpoint_unavailable",
                asset_id=exc.asset_id,
                last_known_age_seconds=exc.last_known_age_seconds,
                mode=self._settings.execution_mode,
            )
            return
        total = state.total_position_value_usd + state.available_capital_usd

        # M17 MD.3 : ségrégation stricte par mode (plus de pollution
        # cross-mode SIM/DRY/LIVE — audit C-003). Le drawdown se calcule
        # contre le max historique du **même** mode uniquement.
        mode = self._settings.execution_mode
        max_ever = await self._repo.get_max_total_usdc(execution_mode=mode)
        drawdown_pct = self._compute_drawdown_pct(max_ever, total)

        is_simulated = mode != "live"
        # M17 MD.6 : peuple realized + unrealized avec les vraies valeurs (au
        # lieu de 0.0 hardcodé — audit H-002). `unrealized = total - initial -
        # realized_cumulative` cohérent avec la formule `/home` PnL latent
        # M13 ([dashboard/queries.py:980-982](../dashboard/queries.py#L980-L982))
        # — convergence /home ↔ /performance garantie (audit C-005 effet).
        realized_cumulative = await self._positions_repo.sum_realized_pnl_by_mode(
            simulated=is_simulated,
        )
        initial_capital = float(
            self._settings.dry_run_initial_capital_usd
            if self._settings.dry_run_initial_capital_usd is not None
            else self._settings.risk_available_capital_usd_stub
        )
        unrealized_pnl = total - initial_capital - realized_cumulative
        dto = PnlSnapshotDTO(
            total_usdc=total,
            realized_pnl=realized_cumulative,
            unrealized_pnl=unrealized_pnl,
            drawdown_pct=drawdown_pct,
            open_positions_count=state.open_positions_count,
            cash_pnl_total=None,
            is_dry_run=is_simulated,
            execution_mode=mode,
        )
        await self._repo.insert(dto)
        log.info(
            "pnl_snapshot_written",
            total_usdc=total,
            drawdown_pct=drawdown_pct,
            open_positions_count=state.open_positions_count,
            mode=mode,
            is_dry_run=is_simulated,
        )
        await self._maybe_trigger_alerts(total, drawdown_pct, max_ever, stop_event)

    async def _maybe_trigger_alerts(
        self,
        total: float,
        drawdown_pct: float,
        max_ever: float | None,
        stop_event: asyncio.Event,
    ) -> None:
        """Kill switch + drawdown warning — **identique dans les 3 modes** (M10).

        M10 inverse l'invariant M4/M8 : le kill switch fire en SIMULATION /
        DRY_RUN / LIVE à l'identique. Seul le badge visuel dans l'alerte
        Telegram distingue les modes (injecté par ``AlertRenderer``). En
        SIMULATION, ``stop_event`` est local au backtest (réduit la simulation
        en cours), pas global — la sémantique "stop" reste portée par le caller.

        M17 MD.7 : ordre strict côté kill switch :
            1. ``insert_event(kill_switch)``  — audit trail DB (best-effort)
            2. ``push_alert(CRITICAL)``       — Telegram queue (non-bloquant)
            3. ``touch sentinel``             — respawn superviseur paused
            4. ``stop_event.set()``           — **strictement la dernière étape**
        Cf. CLAUDE.md §Sécurité M12_bis Phase D.
        """
        threshold = self._settings.kill_switch_drawdown_pct
        mode = self._settings.execution_mode
        if drawdown_pct >= threshold:
            log.error(
                "kill_switch_triggered",
                mode=mode,
                drawdown_pct=drawdown_pct,
                threshold=threshold,
                total_usdc=total,
            )
            # M17 MD.7 (étape 1) : audit trail AVANT alerte/sentinel/stop.
            # Try/except large — si la DB est lockée le kill switch fire
            # quand même (Telegram + sentinel + stop_event restent).
            if self._events_repo is not None:
                try:
                    await self._events_repo.insert(
                        TraderEventDTO(
                            wallet_address=None,
                            event_type="kill_switch",
                            event_metadata={
                                "drawdown_pct": drawdown_pct,
                                "total_usdc": total,
                                "max_total_usdc": max_ever,
                                "execution_mode": mode,
                                "threshold": threshold,
                            },
                        ),
                    )
                    log.info("kill_switch_event_recorded")
                except Exception:
                    log.exception("kill_switch_event_insert_failed")
            else:
                log.warning("kill_switch_event_repo_missing")

            # M17 MD.7 (étape 2) : alerte Telegram (push non-bloquant).
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
            # M12_bis Phase D §4.6 (étape 3) : touch sentinel AVANT
            # stop_event.set(). Ordre critique — si crash entre les deux
            # (kill -9), le respawn superviseur trouvera le sentinel posé
            # → mode paused correct. Inverse (stop_event set avant touch) =
            # mode normal au respawn malgré un drawdown = unsafe.
            if self._sentinel is not None:
                self._sentinel.touch(reason="kill_switch")
            # M12_bis Phase D (étape 4) : stop_event.set() = dernière étape.
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
