"""Client GraphQL Goldsky minimaliste (httpx direct, pas de dep ``gql``).

Opt-in via ``DISCOVERY_BACKEND ∈ {goldsky, hybrid}``. Par défaut (``data_api``)
ce module n'est **pas instancié** par l'orchestrator (zéro dep, zéro overhead).

⚠️ Divergence §14.5 #3 confirmée : la spec initiale pointait vers
``positions-subgraph/0.0.7`` qui n'expose **pas** de champ ``realizedPnl``.
L'entité utile s'appelle en réalité ``UserPosition`` dans ``pnl-subgraph/0.0.14``.
Le default ``GOLDSKY_POSITIONS_SUBGRAPH_URL`` a été ajusté en conséquence.
"""

from __future__ import annotations

import logging
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

from polycopy.discovery.dtos import GoldskyUserPosition

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)

# Query GraphQL pour top-N wallets par realizedPnl cumulé (USDC 10⁶ scale).
# Pas de fenêtre temporelle disponible sur `realizedPnl` (limitation §14.5 #3
# acceptée pour v1 — sortie "proxy moins précis" que Data API).
_TOP_BY_REALIZED_PNL_QUERY = """
query TopByRealizedPnl($first: Int = 100) {
  userPositions(
    first: $first
    orderBy: realizedPnl
    orderDirection: desc
    where: { realizedPnl_gt: "0" }
  ) {
    id
    user
    tokenId
    amount
    avgPrice
    realizedPnl
    totalBought
  }
}
""".strip()


class GoldskyError(RuntimeError):
    """Erreur levée quand le subgraph renvoie `{"errors": [...]}` ou un schéma inattendu."""


class GoldskyClient:
    """Query top-N wallets par realizedPnl cumulé sur le subgraph Polymarket."""

    DEFAULT_TIMEOUT = 20.0

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings: Settings,
    ) -> None:
        self._http = http_client
        self._settings = settings

    async def top_wallets_by_realized_pnl(
        self,
        *,
        first: int = 100,
    ) -> list[GoldskyUserPosition]:
        """Retourne le top-N des `UserPosition` par realizedPnl décroissant.

        Peut renvoyer moins que `first` si le subgraph a peu de données.
        Raise `GoldskyError` si le subgraph renvoie une erreur GraphQL.
        """
        url = self._settings.goldsky_positions_subgraph_url
        payload = {
            "query": _TOP_BY_REALIZED_PNL_QUERY,
            "variables": {"first": first},
        }
        data = await self._post_with_retry(url, payload)
        if "errors" in data and data["errors"]:
            raise GoldskyError(
                f"goldsky_query_errors: {data['errors']!r}",
            )
        positions_raw = data.get("data", {}).get("userPositions") or []
        if not isinstance(positions_raw, list):
            raise GoldskyError(
                f"unexpected userPositions type: {type(positions_raw).__name__}",
            )
        result: list[GoldskyUserPosition] = []
        for item in positions_raw:
            try:
                result.append(GoldskyUserPosition.model_validate(item))
            except Exception:  # noqa: BLE001 — skip schema drift entries
                log.warning("goldsky_position_parse_skipped")
        return result

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(4),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _post_with_retry(
        self,
        url: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._http.post(
            url,
            json=payload,
            timeout=self.DEFAULT_TIMEOUT,
            headers={"content-type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise GoldskyError(
                f"goldsky unexpected payload type: {type(data).__name__}",
            )
        return data
