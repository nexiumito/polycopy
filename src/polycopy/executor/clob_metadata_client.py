"""Client read-only pour les métadonnées CLOB (tick size).

Le `neg_risk` est récupéré via `GammaApiClient.get_market` (M2) qui expose
le champ depuis la réponse Gamma — pas besoin d'un endpoint dédié.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)


class ClobMetadataClient:
    """`GET <polymarket_clob_host>/tick-size?token_id=...`. Cache TTL 5 min.

    M18 : `BASE_URL` consommé via `settings.polymarket_clob_host` (D7).
    `settings=None` reste accepté pour rétrocompat tests M2..M17 (default
    `https://clob.polymarket.com`).
    """

    DEFAULT_BASE_URL = "https://clob.polymarket.com"
    DEFAULT_TIMEOUT = 5.0
    CACHE_TTL = timedelta(minutes=5)

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings: Settings | None = None,
    ) -> None:
        self._http = http_client
        self._cache: dict[str, tuple[datetime, float]] = {}
        self._base_url = (
            settings.polymarket_clob_host if settings is not None else self.DEFAULT_BASE_URL
        )

    async def get_tick_size(self, token_id: str) -> float:
        """Retourne le tick size minimum pour un token. Cache 5 min."""
        cached = self._cache.get(token_id)
        if cached is not None:
            cached_at, value = cached
            if self._now() - cached_at < self.CACHE_TTL:
                log.debug("clob_tick_size_cache_hit", token_id=token_id)
                return value
        log.debug("clob_tick_size_cache_miss", token_id=token_id)
        value = await self._fetch(token_id)
        self._cache[token_id] = (self._now(), value)
        return value

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        stop=stop_after_attempt(3),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch(self, token_id: str) -> float:
        response = await self._http.get(
            f"{self._base_url}/tick-size",
            params={"token_id": token_id},
            timeout=self.DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        return float(data["minimum_tick_size"])

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)
