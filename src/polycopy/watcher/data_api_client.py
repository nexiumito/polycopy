"""Client async pour la Polymarket Data API (`GET /activity`)."""

import logging
from datetime import datetime
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

from polycopy.watcher.dtos import TradeActivity

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)


class DataApiClient:
    """Client REST pour `https://data-api.polymarket.com/activity`, filtré sur `type=TRADE`."""

    BASE_URL = "https://data-api.polymarket.com"
    DEFAULT_TIMEOUT = 10.0

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def get_trades(
        self,
        wallet: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[TradeActivity]:
        """Retourne tous les trades d'un wallet depuis `since` (inclus). Paginé.

        L'API ordonne les résultats en ASC par timestamp pour permettre une reprise
        propre depuis le dernier `timestamp` connu en DB.
        """
        wallet_lower = wallet.lower()
        offset = 0
        collected: list[TradeActivity] = []
        while True:
            page = await self._fetch_page(
                wallet_lower,
                since=since,
                limit=limit,
                offset=offset,
            )
            for raw in page:
                if raw.get("type") != "TRADE":
                    continue
                collected.append(TradeActivity.model_validate(raw))
            if len(page) < limit:
                break
            offset += limit
        return collected

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch_page(
        self,
        wallet: str,
        *,
        since: datetime | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "user": wallet,
            "type": "TRADE",
            "limit": limit,
            "offset": offset,
            "sortDirection": "ASC",
        }
        if since is not None:
            params["start"] = int(since.timestamp())
        response = await self._http.get(
            f"{self.BASE_URL}/activity",
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
        return data
