"""Reader de l'état wallet : positions Polymarket Data API + capital stub."""

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from polycopy.executor.dtos import WalletState

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)


class WalletStateReader:
    """`GET https://data-api.polymarket.com/positions?user=<funder>` + somme `currentValue`.

    En `dry_run=true`, retourne immédiatement un état stub sans appel réseau.
    Cache TTL 30s pour éviter de spammer Data API si plusieurs ordres en rafale.
    """

    BASE_URL = "https://data-api.polymarket.com"
    DEFAULT_TIMEOUT = 10.0
    CACHE_TTL = timedelta(seconds=30)

    def __init__(self, http_client: httpx.AsyncClient, settings: "Settings") -> None:
        self._http = http_client
        self._settings = settings
        self._cached: tuple[datetime, WalletState] | None = None

    async def get_state(self) -> WalletState:
        """Retourne l'état courant du wallet (capital + exposition)."""
        if self._settings.dry_run:
            return WalletState(
                total_position_value_usd=0.0,
                available_capital_usd=self._settings.risk_available_capital_usd_stub,
                open_positions_count=0,
            )
        if self._cached is not None:
            cached_at, state = self._cached
            if self._now() - cached_at < self.CACHE_TTL:
                return state
        if self._settings.polymarket_funder is None:
            raise RuntimeError(
                "WalletStateReader requires POLYMARKET_FUNDER when DRY_RUN=false",
            )
        positions = await self._fetch_positions(self._settings.polymarket_funder)
        total_value = sum(float(p.get("currentValue", 0) or 0) for p in positions)
        state = WalletState(
            total_position_value_usd=total_value,
            available_capital_usd=self._settings.risk_available_capital_usd_stub,
            open_positions_count=len(positions),
        )
        self._cached = (self._now(), state)
        return state

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch_positions(self, funder: str) -> list[dict[str, Any]]:
        response = await self._http.get(
            f"{self.BASE_URL}/positions",
            params={"user": funder.lower(), "limit": 500},
            timeout=self.DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        return data

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)
