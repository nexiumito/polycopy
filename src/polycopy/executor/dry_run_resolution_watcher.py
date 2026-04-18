"""Watcher M8 : résolution périodique des positions virtuelles.

Boucle toutes les ``DRY_RUN_RESOLUTION_POLL_MINUTES`` minutes :
1. Liste les positions virtuelles ouvertes (``simulated=True, closed_at=NULL``).
2. Batch query Gamma ``/markets?condition_ids=<csv>`` pour leur état.
3. Pour chaque marché ``closed=true`` binaire YES/NO, calcule le realized_pnl.
4. ``close_virtual`` la position et logge ``dry_run_position_resolved``.
5. Skip + warning sur marchés neg_risk (v1, cf. spec §14.5 #3).

Lancé conditionnellement par ``ExecutorOrchestrator.run_forever`` (cf. spec
§8.2) — pas un nouveau top-level module.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.repositories import MyPositionRepository

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.storage.models import MyPosition
    from polycopy.strategy.dtos import MarketMetadata
    from polycopy.strategy.gamma_client import GammaApiClient

log = structlog.get_logger(__name__)


class DryRunResolutionWatcher:
    """Boucle périodique de résolution des positions virtuelles M8."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gamma_client: GammaApiClient,
        settings: Settings,
    ) -> None:
        self._positions_repo = MyPositionRepository(session_factory)
        self._gamma = gamma_client
        self._settings = settings

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle jusqu'à ``stop_event.set()``. No-op si M8 désactivé."""
        if not (self._settings.dry_run and self._settings.dry_run_realistic_fill):
            return
        interval_s = self._settings.dry_run_resolution_poll_minutes * 60
        log.info("dry_run_resolution_started", interval_s=interval_s)
        while not stop_event.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dry_run_resolution_cycle_failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                # stop_event set
                break
            except TimeoutError:
                continue
        log.info("dry_run_resolution_stopped")

    async def _run_once(self) -> None:
        log.info("dry_run_resolution_cycle_started")
        open_positions = await self._positions_repo.list_open_virtual()
        if not open_positions:
            log.debug("dry_run_resolution_no_open_positions")
            return
        condition_ids = sorted({p.condition_id for p in open_positions})
        markets = await self._gamma.get_markets_by_condition_ids(condition_ids)
        markets_by_cid = {m.condition_id: m for m in markets}
        resolved_at = datetime.now(tz=UTC)
        for pos in open_positions:
            market = markets_by_cid.get(pos.condition_id)
            if market is None:
                log.debug(
                    "dry_run_resolution_market_missing",
                    condition_id=pos.condition_id,
                )
                continue
            if not market.closed:
                continue
            if market.neg_risk:
                log.warning(
                    "dry_run_resolution_neg_risk_unsupported",
                    asset_id=pos.asset_id,
                    condition_id=pos.condition_id,
                )
                continue
            winning_idx = _winning_outcome_index(market)
            if winning_idx is None:
                log.warning(
                    "dry_run_resolution_winning_outcome_unknown",
                    condition_id=pos.condition_id,
                )
                continue
            payout = _resolution_payout(pos, market, winning_idx)
            realized_pnl = (payout - pos.avg_price) * pos.size
            await self._positions_repo.close_virtual(
                pos.id,
                closed_at=resolved_at,
                realized_pnl=realized_pnl,
            )
            log.info(
                "dry_run_position_resolved",
                asset_id=pos.asset_id,
                condition_id=pos.condition_id,
                size=pos.size,
                avg_price=pos.avg_price,
                payout=payout,
                realized_pnl=realized_pnl,
            )


def _winning_outcome_index(market: MarketMetadata) -> int | None:
    """Identifie l'index gagnant via ``outcome_prices`` (1.0 = YES gagnant).

    Marchés binaires YES/NO uniquement (v1). ``neg_risk`` filtré en amont.
    """
    if len(market.outcomes) != 2:
        return None
    if len(market.outcome_prices) != 2:
        return None
    try:
        prices = [float(p) for p in market.outcome_prices]
    except (ValueError, TypeError):
        return None
    if max(prices) < 0.99:
        # Marché closed mais pas encore matérialisé en outcome_prices définitifs.
        return None
    return prices.index(max(prices))


def _resolution_payout(
    pos: MyPosition,
    market: MarketMetadata,
    winning_idx: int,
) -> float:
    """Retourne 1.0 si la position est sur l'outcome gagnant, sinon 0.0.

    Match par ``asset_id ↔ clob_token_ids[winning_idx]``. Si le token id ne
    matche aucun outcome (drift Gamma), on assume perdant (0.0) et on log.
    """
    if winning_idx >= len(market.clob_token_ids):
        return 0.0
    return 1.0 if market.clob_token_ids[winning_idx] == pos.asset_id else 0.0
