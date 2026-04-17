"""Client async read-only pour l'orderbook CLOB Polymarket (`GET /midpoint`)."""

import logging

import httpx
import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)


class ClobReadClient:
    """Read-only wrapper sur `https://clob.polymarket.com/midpoint`.

    Pas de cache : prix temps réel.
    Pas d'auth : endpoint public.
    """

    BASE_URL = "https://clob.polymarket.com"
    DEFAULT_TIMEOUT = 5.0

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def get_midpoint(self, token_id: str) -> float | None:
        """Retourne le midpoint courant ou `None` si l'orderbook n'existe pas (404)."""
        try:
            payload = await self._fetch(token_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                log.debug("clob_no_orderbook", token_id=token_id)
                return None
            raise
        # La doc OpenAPI annonce `mid_price`, mais la réponse réelle est `mid` (string).
        raw = payload.get("mid")
        if raw is None:
            return None
        return float(raw)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch(self, token_id: str) -> dict[str, str]:
        response = await self._http.get(
            f"{self.BASE_URL}/midpoint",
            params={"token_id": token_id},
            timeout=self.DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        return data
