"""Reader M8 du wallet virtuel : agrège positions virtuelles + valorisation mid.

Implémente le même contrat que ``WalletStateReader`` (méthode ``get_state``
async retournant un ``WalletState``) pour pouvoir être injecté dans le
``PnlSnapshotWriter`` M4 sans refactor (cf. spec §2.7).

``total_usdc = initial_capital + realized_pnl + unrealized_pnl`` où
``unrealized_pnl = Σ (size × current_mid - size × avg_price)`` sur toutes les
positions virtuelles ouvertes.

M17 MD.4 : un cache in-memory ``_last_known_mid`` (TTL 10 min = 2× snapshot
interval default) sert de fallback quand ``ClobReadClient.get_midpoint``
échoue transitoirement (5xx, 429, network blip). Si le mid manque ET le
last_known est stale ou absent → ``MidpointUnavailableError`` est levée :
le ``PnlSnapshotWriter`` catch et skip le snapshot, plutôt que d'écrire
un ``total_usdc`` creux qui corromprait le calcul de drawdown (audit C-004).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.executor.dtos import WalletState
from polycopy.executor.exceptions import MidpointUnavailableError
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

    # M17 MD.4 : TTL 10 min = 2× ``pnl_snapshot_interval_seconds`` default.
    # Au-delà on raise ``MidpointUnavailableError`` plutôt que servir une
    # valeur trop stale (cf. spec §5.4 D4 trade-off).
    _LAST_KNOWN_TTL_SECONDS: ClassVar[float] = 600.0

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clob_read: ClobReadClient,
        settings: Settings,
    ) -> None:
        self._positions_repo = MyPositionRepository(session_factory)
        self._clob_read = clob_read
        self._settings = settings
        # M17 MD.4 : write-through cache du dernier mid OK par asset_id.
        # Float (cohérent avec le retour `get_midpoint`) + datetime UTC.
        self._last_known_mid: dict[str, tuple[float, datetime]] = {}

    async def get_state(self) -> WalletState:
        """Retourne l'état du wallet virtuel pour le PnlSnapshotWriter M4."""
        positions = await self._positions_repo.list_open_virtual()
        unrealized = 0.0
        exposure = 0.0
        for pos in positions:
            mid = await self._safe_get_midpoint(pos.asset_id)
            if mid is None:
                # M17 MD.4 : fallback sur le last_known frais si dispo.
                last_known = self._fetch_last_known(pos.asset_id)
                if last_known is None:
                    age = self._last_known_age_seconds(pos.asset_id)
                    log.warning(
                        "virtual_wallet_midpoint_stale_last_known",
                        asset_id=pos.asset_id,
                        last_known_age_seconds=age,
                    )
                    raise MidpointUnavailableError(
                        asset_id=pos.asset_id,
                        last_known_age_seconds=age,
                    )
                mid = last_known
                log.info(
                    "virtual_wallet_using_last_known_mid",
                    asset_id=pos.asset_id,
                    mid=mid,
                )
            else:
                # M17 MD.4 : refresh write-through cache uniquement sur fetch OK.
                self._record_last_known(pos.asset_id, mid)
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

    def _record_last_known(self, asset_id: str, mid: float) -> None:
        """Write-through cache : overwrite (asset_id → (mid, now_utc))."""
        self._last_known_mid[asset_id] = (mid, datetime.now(tz=UTC))

    def _fetch_last_known(self, asset_id: str) -> float | None:
        """Retourne le mid cached si frais (< TTL), sinon ``None``."""
        entry = self._last_known_mid.get(asset_id)
        if entry is None:
            return None
        mid, recorded_at = entry
        age = (datetime.now(tz=UTC) - recorded_at).total_seconds()
        if age > self._LAST_KNOWN_TTL_SECONDS:
            # TTL expiré : on ne sert pas la valeur. On ne purge pas non
            # plus — un futur `_record_last_known` overwrite proprement.
            return None
        return mid

    def _last_known_age_seconds(self, asset_id: str) -> float | None:
        """Pour le diagnostic : âge du last_known en secondes (ou None)."""
        entry = self._last_known_mid.get(asset_id)
        if entry is None:
            return None
        return (datetime.now(tz=UTC) - entry[1]).total_seconds()
