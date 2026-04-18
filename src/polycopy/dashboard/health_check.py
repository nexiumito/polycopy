"""Health check léger Gamma + Data API pour le footer dashboard (M6 §4).

Singleton-like : 1 ``ExternalHealthChecker`` par app FastAPI, qui détient un
``httpx.AsyncClient`` partagé + un ``asyncio.Lock`` pour sérialiser les refreshs
(évite la fuite de sockets si 100 footer-pings arrivent en simultané).

Cache TTL 30s : 4 calls/min max au pire — négligeable vs ~100 req/min documenté.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Final, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger(__name__)

GAMMA_URL: Final[str] = "https://gamma-api.polymarket.com/markets?limit=1"
DATA_API_URL: Final[str] = "https://data-api.polymarket.com/trades?limit=1"

CACHE_TTL_SECONDS: Final[float] = 30.0
TIMEOUT_SECONDS: Final[float] = 3.0

HealthStatus = Literal["ok", "degraded", "unknown"]


class ExternalHealthSnapshot(BaseModel):
    """Snapshot immuable du dernier ping Gamma + Data API."""

    model_config = ConfigDict(frozen=True)

    gamma_status: HealthStatus
    gamma_latency_ms: int | None
    data_api_status: HealthStatus
    data_api_latency_ms: int | None
    checked_at: datetime

    @classmethod
    def unknown(cls) -> ExternalHealthSnapshot:
        """Snapshot 'unknown' utilisé tant que le 1er ping n'a pas tourné."""
        return cls(
            gamma_status="unknown",
            gamma_latency_ms=None,
            data_api_status="unknown",
            data_api_latency_ms=None,
            checked_at=datetime.now(tz=UTC),
        )


class ExternalHealthChecker:
    """Ping Gamma + Data API, cache TTL 30s, 1 client httpx partagé.

    Pattern :
    - ``check()`` retourne le cache s'il est frais (< TTL).
    - Sinon prend le ``Lock``, refresh, met à jour le cache, libère le lock.
    - Si plusieurs ``check()`` arrivent pendant un refresh : les suivants
      attendent le lock puis retrouvent le cache fresh — pas de stampede.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        cache_ttl_seconds: float = CACHE_TTL_SECONDS,
        timeout_seconds: float = TIMEOUT_SECONDS,
    ) -> None:
        self._client = http_client
        self._cache_ttl = cache_ttl_seconds
        self._timeout = timeout_seconds
        self._lock = asyncio.Lock()
        self._snapshot: ExternalHealthSnapshot = ExternalHealthSnapshot.unknown()
        # ``-inf`` force le premier ``check()`` à refresh.
        self._last_refresh_monotonic: float = float("-inf")

    async def check(self) -> ExternalHealthSnapshot:
        """Retourne le snapshot courant (refresh si TTL expiré)."""
        if self._is_fresh():
            return self._snapshot
        async with self._lock:
            # Re-check sous le lock : un autre coroutine a peut-être déjà refresh.
            if self._is_fresh():
                return self._snapshot
            self._snapshot = await self._refresh()
            self._last_refresh_monotonic = time.monotonic()
            return self._snapshot

    def _is_fresh(self) -> bool:
        return (time.monotonic() - self._last_refresh_monotonic) < self._cache_ttl

    async def _refresh(self) -> ExternalHealthSnapshot:
        """Lance les 2 pings en parallèle, agrège dans un snapshot frozen."""
        gamma_task = asyncio.create_task(self._ping(GAMMA_URL))
        data_api_task = asyncio.create_task(self._ping(DATA_API_URL))
        gamma_status, gamma_latency = await gamma_task
        data_api_status, data_api_latency = await data_api_task
        return ExternalHealthSnapshot(
            gamma_status=gamma_status,
            gamma_latency_ms=gamma_latency,
            data_api_status=data_api_status,
            data_api_latency_ms=data_api_latency,
            checked_at=datetime.now(tz=UTC),
        )

    async def _ping(self, url: str) -> tuple[HealthStatus, int | None]:
        """Ping HEAD avec fallback GET — Polymarket peut refuser HEAD selon endpoint.

        Renvoie ``(status, latency_ms)``. ``latency_ms`` ``None`` si timeout/erreur.
        """
        start = time.perf_counter()
        try:
            response = await self._client.request(
                "HEAD",
                url,
                timeout=self._timeout,
            )
            if response.status_code == 405:
                # Méthode non autorisée : fallback GET.
                response = await self._client.get(url, timeout=self._timeout)
            latency_ms = int((time.perf_counter() - start) * 1000)
            if 200 <= response.status_code < 400:
                return "ok", latency_ms
            return "degraded", latency_ms
        except httpx.TimeoutException:
            log.debug("external_health_timeout", url=url, timeout_s=self._timeout)
            return "degraded", None
        except httpx.HTTPError as exc:
            log.debug("external_health_error", url=url, error=str(exc))
            return "degraded", None
