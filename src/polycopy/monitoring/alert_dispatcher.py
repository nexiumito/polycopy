"""Consommateur de la queue ``alerts_queue`` (M4 + M7).

M4 : cooldown in-memory par ``cooldown_key`` + POST Telegram.

M7 étend M4 par *composition* (pas de refactor) :
- ``AlertRenderer`` injecté : chaque alerte est rendue via le template
  ``{event_type}.md.j2`` (ou ``fallback.md.j2``) avant envoi.
- ``AlertDigestWindow`` injectée : ≥ N alertes du même event_type en fenêtre
  → batch en un seul message ``digest.md.j2``.
- Buffer in-memory des bodies récents par event_type → remplit les
  ``sample_lines`` du digest.
- Compteur total 24h par event_type (pour daily summary) + timestamp de la
  dernière alerte CRITICAL (pour que le heartbeat se taise).

Invariants M4 préservés :
- ``cooldown_key`` : le comportement reste identique (2e alerte même key dans
  la fenêtre → throttlée).
- ``telegram_client.enabled is False`` → log + retour sans raise.
- Pas d'écriture DB.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from polycopy.monitoring.alert_digest import AlertDigestWindow
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import Alert, DigestContext
from polycopy.monitoring.telegram_client import TelegramClient

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_QUEUE_GET_TIMEOUT_SECONDS: float = 1.0
_DIGEST_SAMPLE_MAX: int = 4
_DIGEST_BUFFER_MAX: int = 50
_CRITICAL_LEVELS: frozenset[str] = frozenset({"CRITICAL", "ERROR"})


class AlertDispatcher:
    """Drain la queue d'alertes vers Telegram (cooldown + digest + rendu)."""

    def __init__(
        self,
        queue: asyncio.Queue[Alert],
        telegram_client: TelegramClient,
        settings: Settings,
        renderer: AlertRenderer | None = None,
        digest: AlertDigestWindow | None = None,
    ) -> None:
        self._queue = queue
        self._telegram = telegram_client
        self._settings = settings
        self._cooldown_seconds = settings.alert_cooldown_seconds
        self._last_sent: dict[str, datetime] = {}
        self._renderer = renderer if renderer is not None else AlertRenderer()
        self._digest = (
            digest
            if digest is not None
            else AlertDigestWindow(
                window_seconds=settings.telegram_digest_window_minutes * 60,
                threshold=settings.telegram_digest_threshold,
            )
        )
        self._digest_buffer: dict[str, deque[Alert]] = defaultdict(
            lambda: deque(maxlen=_DIGEST_BUFFER_MAX),
        )
        self._counts_since_boot: dict[str, int] = defaultdict(int)
        self._last_critical_at: datetime | None = None

    # ------------------------------------------------------------------
    # API publique (hooks consommés par HeartbeatScheduler / daily queries)
    # ------------------------------------------------------------------

    @property
    def counts_since_boot(self) -> dict[str, int]:
        """Compteur in-memory des alertes reçues (par event_type)."""
        return dict(self._counts_since_boot)

    def has_recent_critical(self, window: timedelta) -> bool:
        """True si une alerte CRITICAL/ERROR a été reçue dans ``window``."""
        if self._last_critical_at is None:
            return False
        return self._now() - self._last_critical_at <= window

    # ------------------------------------------------------------------
    # Boucle principale
    # ------------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Drain la queue jusqu'à ``stop_event.set()``."""
        log.info("alert_dispatcher_started", telegram_enabled=self._telegram.enabled)
        while not stop_event.is_set():
            try:
                alert = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=_QUEUE_GET_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            await self._handle(alert)
        log.info("alert_dispatcher_stopped")

    # ------------------------------------------------------------------
    # Pipeline interne
    # ------------------------------------------------------------------

    async def _handle(self, alert: Alert) -> None:
        """Applique cooldown → digest → rendu → envoi Telegram."""
        self._counts_since_boot[alert.event] += 1
        if alert.level in _CRITICAL_LEVELS:
            self._last_critical_at = self._now()

        if alert.cooldown_key is not None:
            now = self._now()
            last = self._last_sent.get(alert.cooldown_key)
            if last is not None and (now - last).total_seconds() < self._cooldown_seconds:
                log.debug(
                    "alert_throttled",
                    cooldown_key=alert.cooldown_key,
                    alert_event=alert.event,
                )
                return
            self._last_sent[alert.cooldown_key] = now

        decision = self._digest.register(alert, self._now())

        # Buffer les bodies pour remplir les sample_lines du digest
        self._digest_buffer[alert.event].append(alert)

        if decision.action == "emit_digest":
            formatted = self._build_digest_message(alert, decision.count)
            # Reset du buffer après émission — cohérent avec reset du compteur.
            self._digest_buffer[alert.event].clear()
        else:
            formatted = self._renderer.render_alert(alert)

        sent = await self._telegram.send(formatted)
        if sent:
            log.info(
                "alert_sent",
                alert_event=alert.event,
                level=alert.level,
                digest_action=decision.action,
                digest_count=decision.count,
            )
        else:
            log.warning(
                "alert_send_failed",
                alert_event=alert.event,
                level=alert.level,
                digest_action=decision.action,
            )

    def _build_digest_message(self, representative: Alert, count: int) -> str:
        buffered = list(self._digest_buffer[representative.event])
        sample = [a.body for a in buffered[-_DIGEST_SAMPLE_MAX:]]
        truncated = max(0, count - len(sample))
        dashboard_url = self._dashboard_url_or_none()
        ctx = DigestContext(
            event_type=representative.event,
            count=count,
            window_minutes=self._settings.telegram_digest_window_minutes,
            level=representative.level,
            sample_lines=sample,
            truncated_count=truncated,
            dashboard_url=dashboard_url,
        )
        return self._renderer.render_digest(ctx)

    def _dashboard_url_or_none(self) -> str | None:
        if not self._settings.dashboard_enabled:
            return None
        return f"http://{self._settings.dashboard_host}:{self._settings.dashboard_port}/"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)
