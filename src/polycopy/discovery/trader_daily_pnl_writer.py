"""Scheduler quotidien : snapshot equity curve par wallet (M12).

Co-lancé dans le ``TaskGroup`` interne de :class:`DiscoveryOrchestrator`
(cf. spec M12 §5.6, §7.11, §13.7). Cadence 24h par défaut (env var
``TRADER_DAILY_PNL_INTERVAL_SECONDS``). Scan des wallets ``status ∈
{shadow, active, paused, pinned}``, fetch ``/positions`` + ``/value``
publics, écriture d'une row par wallet et par date UTC dans
``trader_daily_pnl`` (dédup via contrainte unique).

Source unique de l'equity curve pour Sortino / Calmar / consistency dans
le scoring v2. Read-only strict côté API — aucune creds CLOB.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from datetime import date as date_type
from typing import TYPE_CHECKING, Literal

import structlog

from polycopy.discovery.data_api_client import DiscoveryDataApiClient
from polycopy.storage.dtos import TraderDailyPnlDTO
from polycopy.storage.repositories import (
    TargetTraderRepository,
    TraderDailyPnlRepository,
)

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_ScannableStatus = Literal[
    "shadow",
    "active",
    "paused",  # deprecated M5_bis Phase A, retiré runtime Phase C
    "pinned",
    "sell_only",  # M5_bis : wallet en wind-down, toujours scoré
]
_SCANNABLE_STATUSES: tuple[_ScannableStatus, ...] = (
    "shadow",
    "active",
    "paused",
    "pinned",
    "sell_only",
)


class TraderDailyPnlWriter:
    """Coroutine TaskGroup : snapshot 1×/jour de l'equity curve par wallet.

    Stratégie :

    1. Boucle ``run_forever(stop_event)`` : exécute un premier snapshot
       immédiatement puis sleep ``TRADER_DAILY_PNL_INTERVAL_SECONDS``.
    2. ``_snapshot_all()`` scanne les wallets ``status ∈ shadow/active/paused/
       pinned`` (ignore ``absent``), fetch ``/value`` (sanity) + ``/positions``
       pour chaque, agrège ``equity_usdc = value + sum(realized_pnl résolues)``
       et ``positions_count = len(non-résolues)``.
    3. ``TraderDailyPnlRepository.insert_if_new`` dédup automatiquement sur
       ``(wallet_address, date)`` — ré-exécution dans la même journée = no-op.

    Deltas ``realized_pnl_day`` / ``unrealized_pnl_day`` : calculés par
    différence avec la row du jour précédent via ``get_curve`` (lookup 2 rows
    max). Zéro si première row ou pas de historique.
    """

    def __init__(
        self,
        data_api: DiscoveryDataApiClient,
        target_repo: TargetTraderRepository,
        daily_pnl_repo: TraderDailyPnlRepository,
        settings: Settings,
    ) -> None:
        self._data_api = data_api
        self._target_repo = target_repo
        self._daily_pnl_repo = daily_pnl_repo
        self._settings = settings

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale. Exit quand ``stop_event`` est set."""
        cfg = self._settings
        interval = float(cfg.trader_daily_pnl_interval_seconds)
        log.info(
            "trader_daily_pnl_writer_starting",
            interval_s=interval,
            enabled=cfg.trader_daily_pnl_enabled,
        )

        while not stop_event.is_set():
            try:
                inserted = await self._snapshot_all()
                log.info(
                    "trader_daily_pnl_snapshot_done",
                    wallets_inserted=inserted,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("trader_daily_pnl_snapshot_failed")
                # Retry rapide après un crash (1 min) avant le sleep normal.
                if await _sleep_or_stop(stop_event, 60.0):
                    break
                continue
            if await _sleep_or_stop(stop_event, interval):
                break

        log.info("trader_daily_pnl_writer_stopped")

    async def _snapshot_all(self) -> int:
        """Écrit 1 row par wallet scanné. Retourne le nombre de rows insérées.

        Idempotent : re-run le même jour UTC retourne 0 (dédup).
        """
        today = datetime.now(tz=UTC).date()
        wallets_to_scan: list[str] = []
        for status in _SCANNABLE_STATUSES:
            scanned = await self._target_repo.list_by_status(status)
            wallets_to_scan.extend(t.wallet_address for t in scanned)

        # Dédup par ordre d'apparition (dict preserves insertion order).
        unique_wallets = list(dict.fromkeys(wallets_to_scan))

        inserted_count = 0
        for wallet in unique_wallets:
            try:
                dto = await self._compute_snapshot_dto(wallet, snapshot_date=today)
            except Exception:
                log.exception("trader_daily_pnl_compute_failed", wallet=wallet)
                continue
            try:
                if await self._daily_pnl_repo.insert_if_new(dto):
                    inserted_count += 1
            except Exception:
                log.exception("trader_daily_pnl_insert_failed", wallet=wallet)
        return inserted_count

    async def _compute_snapshot_dto(
        self,
        wallet: str,
        *,
        snapshot_date: date_type,
    ) -> TraderDailyPnlDTO:
        """Agrège ``/value`` + ``/positions`` en un :class:`TraderDailyPnlDTO`.

        ``equity_usdc`` = valeur totale (positions ouvertes via ``/value``) +
        cumul ``realized_pnl`` des positions résolues. Les deltas
        ``realized_pnl_day`` et ``unrealized_pnl_day`` sont calculés par
        différence avec la row de la veille si elle existe.
        """
        value_current = await self._data_api.get_value(wallet)
        positions = await self._data_api.get_positions(wallet)

        realized_cum = sum(float(p.realized_pnl) for p in positions if p.is_resolved)
        equity_usdc = float(value_current) + realized_cum
        open_positions = [p for p in positions if not p.is_resolved]
        positions_count = len(open_positions)

        # Calcul des deltas par rapport à la row précédente (au plus 2 rows
        # à lire grâce à l'index `(wallet, date)`). Cf. carnet M12 §Constantes.
        previous_rows = await self._daily_pnl_repo.get_curve(wallet, days=2)
        realized_pnl_day = 0.0
        unrealized_pnl_day = 0.0
        if previous_rows:
            prev = previous_rows[-1]  # row la plus récente avant aujourd'hui
            realized_pnl_day = realized_cum - float(prev.equity_usdc - prev.unrealized_pnl_day)
            unrealized_pnl_day = float(value_current) - (
                float(prev.equity_usdc) - float(prev.realized_pnl_day)
            )

        return TraderDailyPnlDTO(
            wallet_address=wallet,
            date=snapshot_date,
            equity_usdc=equity_usdc,
            realized_pnl_day=realized_pnl_day,
            unrealized_pnl_day=unrealized_pnl_day,
            positions_count=positions_count,
        )


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    """Attend ``seconds`` ou ``stop_event.set()``. Retourne True si stop reçu."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True
