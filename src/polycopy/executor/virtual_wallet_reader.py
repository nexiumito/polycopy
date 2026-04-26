"""Reader M8 du wallet virtuel : agrège positions virtuelles + valorisation mid.

Implémente le même contrat que ``WalletStateReader`` (méthode ``get_state``
async retournant un ``WalletState``) pour pouvoir être injecté dans le
``PnlSnapshotWriter`` M4 sans refactor (cf. spec §2.7).

``total_usdc = virtual_capital + realized_pnl + unrealized_pnl`` où
``unrealized_pnl = Σ (size × current_mid - size × avg_price)`` sur toutes les
positions virtuelles ouvertes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.executor.dtos import WalletState
from polycopy.storage.repositories import MyPositionRepository

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.strategy.clob_read_client import ClobReadClient

log = structlog.get_logger(__name__)


class VirtualWalletStateReader:
    """Lit l'état virtuel : positions virtuelles + valorisation mid-price.

    N'effectue **aucun appel CLOB authentifié** — uniquement read-only public
    (``ClobReadClient.get_midpoint``). Cohérent avec l'invariant M8 "aucune
    creds consommée par le path dry-run realistic fill".
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clob_read: ClobReadClient,
        settings: Settings,
    ) -> None:
        self._positions_repo = MyPositionRepository(session_factory)
        self._clob_read = clob_read
        self._settings = settings

    async def get_state(self) -> WalletState:
        """Retourne l'état du wallet virtuel pour le PnlSnapshotWriter M4."""
        positions = await self._positions_repo.list_open_virtual()
        unrealized = 0.0
        exposure = 0.0
        for pos in positions:
            mid = await self._safe_get_midpoint(pos.asset_id)
            if mid is None:
                # Skip cette position : la valorisation tombe à 0 cycle suivant.
                continue
            current_value = pos.size * mid
            unrealized += current_value - pos.size * pos.avg_price
            exposure += current_value
        realized = await self._positions_repo.sum_realized_pnl_virtual()
        # M17 MD.5 : source unique post-deprecation. Le validator Pydantic
        # MD.5 reroute `DRY_RUN_VIRTUAL_CAPITAL_USD` legacy vers
        # `dry_run_initial_capital_usd` au boot — un seul setting consommé ici.
        # Fallback `risk_available_capital_usd_stub` si ni l'un ni l'autre set.
        initial = float(
            self._settings.dry_run_initial_capital_usd
            if self._settings.dry_run_initial_capital_usd is not None
            else self._settings.risk_available_capital_usd_stub
        )
        total_usdc = initial + realized + unrealized
        # PnlSnapshotWriter._tick fait `total = pos_value + capital`. Pour
        # rester compat M4 sans refactor : on encode `pos_value=exposure`
        # (current mid-value) et `capital=total_usdc - exposure` afin que
        # leur somme reflète bien le total_usdc virtuel attendu.
        return WalletState(
            total_position_value_usd=exposure,
            available_capital_usd=total_usdc - exposure,
            open_positions_count=len(positions),
        )

    async def _safe_get_midpoint(self, asset_id: str) -> float | None:
        """Fetch midpoint en absorbant les erreurs réseau / 404 (warning + skip)."""
        try:
            return await self._clob_read.get_midpoint(asset_id)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning(
                "virtual_wallet_midpoint_fetch_failed",
                asset_id=asset_id,
                error=str(exc)[:120],
            )
            return None
