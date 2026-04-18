"""Collecteur de metrics M5 pour 1 wallet.

Fetch ``/positions`` + ``/activity`` sur la fenêtre ``SCORING_LOOKBACK_DAYS``,
agrège :

- ``resolved_positions_count`` / ``open_positions_count`` via `RawPosition.is_resolved`.
- ``win_rate`` = wins sur résolues (`cash_pnl > 0`).
- ``realized_roi`` = sum(realized_pnl) / sum(initial_value).
- ``total_volume_usd`` = somme `size × price` sur les activity trades.
- ``herfindahl_index`` = Σ((vol_i / total_vol)²) par condition_id.
- ``largest_position_value_usd`` = max(current_value).

Piège HHI (§14.4 #5) : si volume nul, default à 1.0 (concentration max).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from polycopy.discovery.data_api_client import DiscoveryDataApiClient
from polycopy.discovery.dtos import RawPosition, TraderMetrics

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


class MetricsCollector:
    """Calcule un `TraderMetrics` pour 1 wallet à partir de la Data API."""

    def __init__(
        self,
        data_api: DiscoveryDataApiClient,
        settings: Settings,
    ) -> None:
        self._data_api = data_api
        self._settings = settings

    async def collect(self, wallet_address: str) -> TraderMetrics:
        """Fetch + agrège metrics d'un wallet sur `scoring_lookback_days`."""
        since = datetime.now(tz=UTC) - timedelta(days=self._settings.scoring_lookback_days)
        positions = await self._data_api.get_positions(wallet_address)
        activity = await self._data_api.get_activity_trades(wallet_address, since=since)

        return self._compute(wallet_address, positions, activity)

    def _compute(
        self,
        wallet_address: str,
        positions: list[RawPosition],
        activity: list[dict[str, Any]],
    ) -> TraderMetrics:
        resolved = [p for p in positions if p.is_resolved]
        open_ = [p for p in positions if not p.is_resolved]

        # Win rate
        if resolved:
            wins = sum(1 for p in resolved if float(p.cash_pnl) > 0)
            win_rate = wins / len(resolved)
        else:
            win_rate = 0.0

        # ROI
        total_initial = sum(float(p.initial_value) for p in resolved)
        total_realized = sum(float(p.realized_pnl) for p in resolved)
        realized_roi = total_realized / total_initial if total_initial > 0 else 0.0

        # Volume + Herfindahl depuis activity
        volume_per_market: dict[str, float] = defaultdict(float)
        for t in activity:
            cid = t.get("conditionId")
            if not cid:
                continue
            try:
                size = float(t.get("size") or 0)
                price = float(t.get("price") or 0)
            except (TypeError, ValueError):
                continue
            volume_per_market[cid] += size * price
        total_volume = sum(volume_per_market.values())
        if total_volume > 0 and volume_per_market:
            hhi = sum((v / total_volume) ** 2 for v in volume_per_market.values())
        else:
            hhi = 1.0

        largest_pos = max(
            (float(p.current_value) for p in positions),
            default=0.0,
        )

        return TraderMetrics(
            wallet_address=wallet_address.lower(),
            resolved_positions_count=len(resolved),
            open_positions_count=len(open_),
            win_rate=win_rate,
            realized_roi=realized_roi,
            total_volume_usd=total_volume,
            herfindahl_index=hhi,
            nb_distinct_markets=len(volume_per_market),
            largest_position_value_usd=largest_pos,
            measurement_window_days=self._settings.scoring_lookback_days,
            fetched_at=datetime.now(tz=UTC),
        )
