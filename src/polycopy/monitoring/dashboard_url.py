"""Calcule l'URL dashboard injectée dans les messages Telegram (M12_bis Phase G).

Centralise la logique qui vivait dispersée dans ``alert_dispatcher``,
``startup_notifier`` et ``daily_summary_queries`` (M7). L'URL est calculée
**une fois au boot** du ``MonitoringOrchestrator`` puis injectée dans chaque
contexte template via ``AlertRenderer._inject_mode``.

Priorité :

1. Dashboard désactivé (``DASHBOARD_ENABLED=false``) → ``None``.
2. ``DASHBOARD_BIND_TAILSCALE=true`` + tailnet résolu + ``MACHINE_ID`` set →
   ``http://{machine_id_lower}.{tailnet}:{port}/`` — lien cliquable depuis
   n'importe quelle device du tailnet (téléphone, laptop distant).
3. Fallback (single-machine ou tailnet non résolu) →
   ``http://{dashboard_host}:{dashboard_port}/`` — utile en contexte
   navigateur desktop où ``127.0.0.1`` est accessible.

La fonction ne lève jamais : si Tailscale est demandé mais indisponible,
elle retombe silencieusement sur le format localhost (logué WARNING).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from polycopy.remote_control.tailscale import resolve_tailnet_name

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


def compute_dashboard_url(settings: Settings) -> str | None:
    """Retourne l'URL dashboard à injecter dans les templates Telegram.

    Args:
        settings: Pydantic ``Settings`` — consomme ``dashboard_enabled``,
            ``dashboard_bind_tailscale``, ``dashboard_host``,
            ``dashboard_port``, ``machine_id``, ``tailnet_name``.

    Returns:
        URL ``http://.../`` ou ``None`` si le dashboard est désactivé.
    """
    if not settings.dashboard_enabled:
        return None

    if settings.dashboard_bind_tailscale:
        tailnet = resolve_tailnet_name(settings)
        machine_id = settings.machine_id
        if tailnet and machine_id:
            url = f"http://{machine_id.lower()}.{tailnet}:{settings.dashboard_port}/"
            log.info("dashboard_url_resolved", url=url, source="tailscale")
            return url
        log.warning(
            "dashboard_url_tailscale_unavailable_fallback_localhost",
            tailnet_resolved=bool(tailnet),
            machine_id_set=bool(machine_id),
        )

    url = f"http://{settings.dashboard_host}:{settings.dashboard_port}/"
    log.info("dashboard_url_resolved", url=url, source="dashboard_host")
    return url
