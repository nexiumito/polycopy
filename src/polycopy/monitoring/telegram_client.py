"""Client Telegram Bot API (httpx direct, pas python-telegram-bot).

Sécurité :
- Le ``TELEGRAM_BOT_TOKEN`` figure dans l'URL des appels ``sendMessage``. On ne
  logge JAMAIS cette URL en clair (ni le token directement, ni partiellement).
- Si le token ou le chat_id est absent → le client est en mode *disabled* :
  aucun POST réseau, ``send()`` retourne ``False`` sans raise.

Voir ``specs/M4-monitoring.md`` §3.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
import structlog
from tenacity import (
    RetryError,
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


class _RetryableHttpError(Exception):
    """Wrapper interne pour distinguer 4xx (non retry) de 429/5xx (retry)."""


class TelegramClient:
    """Client minimal pour ``sendMessage`` Telegram Bot API."""

    BASE_URL = "https://api.telegram.org"
    DEFAULT_TIMEOUT = 5.0

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http_client
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._enabled = bool(self._token) and bool(self._chat_id)

    @property
    def enabled(self) -> bool:
        """Retourne ``True`` si token ET chat_id sont configurés."""
        return self._enabled

    async def send(self, text: str) -> bool:
        """POST ``sendMessage`` avec retry. Retourne ``True`` si succès.

        Ne raise jamais : en mode disabled → ``False`` sans réseau. En cas
        d'erreur HTTP (400 ou 5xx après retries) → ``False`` + log sans token.
        """
        if not self._enabled:
            log.debug("telegram_send_skipped_disabled")
            return False
        try:
            return await self._send_with_retry(text)
        except RetryError:
            log.warning("telegram_error", reason="retries_exhausted")
            return False
        except _RetryableHttpError:
            # Jamais atteint : tenacity wrap en RetryError après stop_after_attempt.
            return False
        except httpx.HTTPStatusError as exc:
            log.warning("telegram_error", status_code=exc.response.status_code)
            return False

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, _RetryableHttpError)),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        stop=stop_after_attempt(3),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _send_with_retry(self, text: str) -> bool:
        assert self._token is not None and self._chat_id is not None  # _enabled guard
        url = f"{self.BASE_URL}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        response = await self._http.post(url, json=payload, timeout=self.DEFAULT_TIMEOUT)
        if response.status_code == 200:
            return True
        if response.status_code == 429 or response.status_code >= 500:
            raise _RetryableHttpError(f"telegram_http_{response.status_code}")
        # 4xx autre que 429 → bad request, ne pas retry. Pas de body dans le log
        # pour éviter toute fuite accidentelle.
        log.warning("telegram_error", status_code=response.status_code)
        return False
