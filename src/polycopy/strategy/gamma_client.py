"""Client async pour la Gamma API Polymarket (`GET /markets`)."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from polycopy.strategy.dtos import MarketMetadata

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)


class GammaApiClient:
    """Client REST `https://gamma-api.polymarket.com/markets` avec cache TTL 60s."""

    BASE_URL = "https://gamma-api.polymarket.com"
    DEFAULT_TIMEOUT = 10.0
    CACHE_TTL = timedelta(seconds=60)

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client
        self._cache: dict[str, tuple[datetime, MarketMetadata]] = {}

    async def get_market(self, condition_id: str) -> MarketMetadata | None:
        """Retourne le marché ou `None` si Gamma renvoie un array vide.

        Cache TTL 60s par `condition_id`. Utilise `_now()` qui peut être
        monkeypatché par les tests.
        """
        cached = self._cache.get(condition_id)
        if cached is not None:
            cached_at, market = cached
            if self._now() - cached_at < self.CACHE_TTL:
                log.debug("gamma_cache_hit", condition_id=condition_id)
                return market
        log.debug("gamma_cache_miss", condition_id=condition_id)
        markets = await self._fetch(condition_id)
        if not markets:
            return None
        market = MarketMetadata.model_validate(markets[0])
        self._cache[condition_id] = (self._now(), market)
        return market

    async def list_top_markets(
        self,
        *,
        limit: int = 20,
        only_active: bool = True,
    ) -> list[MarketMetadata]:
        """Top-N marchés triés par liquidité descendante (usage M5 `/holders` bootstrap).

        Non-caché : appelé ~1 fois par cycle discovery (6h).
        """
        params: dict[str, Any] = {
            "limit": limit,
            "order": "liquidityNum",
            "ascending": "false",
        }
        if only_active:
            params["active"] = "true"
            params["closed"] = "false"
        response = await self._http.get(
            f"{self.BASE_URL}/markets",
            params=params,
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
        markets: list[MarketMetadata] = []
        for item in data:
            try:
                markets.append(MarketMetadata.model_validate(item))
            except Exception:  # noqa: BLE001 — skip on schema drift
                log.warning("gamma_top_market_parse_skipped")
        return markets

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch(self, condition_id: str) -> list[dict[str, Any]]:
        response = await self._http.get(
            f"{self.BASE_URL}/markets",
            params={"condition_ids": condition_id},
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
