"""Client async pour la Polymarket Data API (`GET /activity`)."""

import logging
from datetime import UTC, datetime
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

# La Data API Polymarket renvoie ``400 Bad Request`` sur des offsets
# profonds (observé empiriquement à partir d'~3100 sur ``/activity``). On
# bascule sur un cursor time-based dès qu'on approche cette limite : la
# page suivante repart de ``start = timestamp du dernier trade collecté``
# avec ``offset=0``. Garde-fou ``_MAX_CURSOR_RESETS`` pour éviter les
# boucles infinies si le wallet a des centaines de trades au même
# timestamp (edge case pathologique — la dédup insert_if_new côté repo
# évite les doublons, mais on cap quand même).
_MAX_SAFE_OFFSET = 2900
_MAX_CURSOR_RESETS = 50


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
        propre depuis le dernier `timestamp` connu en DB. Si la pagination atteint
        ``_MAX_SAFE_OFFSET`` (limite empirique Data API sur ``offset``), on bascule
        sur un cursor time-based : ``since = dernier_trade.timestamp`` et
        ``offset=0``. La dédup par ``tx_hash`` côté repo absorbe les éventuels
        doublons au timestamp charnière.
        """
        wallet_lower = wallet.lower()
        offset = 0
        cursor_resets = 0
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
            if offset >= _MAX_SAFE_OFFSET:
                # Aucun point de départ pour un cursor time-based si on n'a
                # encore rien collecté — on abandonne proprement.
                if not collected:
                    break
                cursor_resets += 1
                if cursor_resets > _MAX_CURSOR_RESETS:
                    log.warning(
                        "data_api_max_cursor_resets_hit",
                        wallet=wallet_lower,
                        collected=len(collected),
                    )
                    break
                since = datetime.fromtimestamp(collected[-1].timestamp, tz=UTC)
                offset = 0
                log.info(
                    "data_api_offset_cap_reset",
                    wallet=wallet_lower,
                    new_since=since.isoformat(),
                    collected_so_far=len(collected),
                )
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
