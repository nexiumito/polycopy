"""Résolution de l'IPv4 Tailscale pour le bind FastAPI (M12_bis §4.4.1).

Appelée une unique fois au boot du ``RemoteControlOrchestrator``. Deux
chemins :

1. ``settings.remote_control_tailscale_ip_override`` set → utilisé tel quel
   (déjà validé par Pydantic : IPv4 non-loopback, non-unspecified).
2. Sinon → ``subprocess.run(["tailscale", "ip", "-4"], timeout=5s)``, parse
   la 1ʳᵉ IPv4, vérifie qu'elle est dans la plage CGNAT Tailscale
   ``100.64.0.0/10``.

Tout échec (binaire absent, daemon down, timeout, stdout vide, IP hors
plage) lève ``RemoteControlBootError`` → le runner crashe avec un message
clair plutôt que de démarrer sur ``0.0.0.0`` par erreur.

Ne dépend d'aucun autre module runtime (juste ``config`` via TYPE_CHECKING).
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_TAILSCALE_CMD_TIMEOUT_SECONDS: float = 5.0
_TAILSCALE_CGNAT_RANGE: ipaddress.IPv4Network = ipaddress.IPv4Network("100.64.0.0/10")


class RemoteControlBootError(RuntimeError):
    """Levée au boot si l'environnement remote_control est inutilisable.

    Cas couverts :
    - Tailscale non installé (binaire absent dans PATH).
    - Daemon ``tailscaled`` down (timeout ``tailscale ip -4``).
    - Machine non enrôlée dans un tailnet (stdout vide, returncode≠0).
    - Sortie non parseable ou hors plage CGNAT Tailscale.
    - Override IP fourni mais invalide (normalement attrapé par le
      validator Pydantic, mais re-vérifié ici par défense en profondeur).
    """


def resolve_tailscale_ipv4(settings: Settings) -> str:
    """Retourne l'IPv4 à utiliser pour ``uvicorn.Config(host=...)``.

    Args:
        settings: Pydantic ``Settings`` — consomme
            ``remote_control_tailscale_ip_override``.

    Returns:
        IPv4 sous forme dotted-quad (ex. ``"100.64.0.1"``).

    Raises:
        RemoteControlBootError: voir docstring classe.
    """
    override = settings.remote_control_tailscale_ip_override
    if override is not None:
        log.info("remote_control_tailscale_ip_override_used", ip=override)
        return override

    try:
        result = subprocess.run(  # noqa: S603 — argv list hardcoded, pas de shell=True
            ["tailscale", "ip", "-4"],  # noqa: S607 — binary lookup via PATH (install standard /usr/bin)
            capture_output=True,
            text=True,
            timeout=_TAILSCALE_CMD_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RemoteControlBootError(
            "tailscale_not_installed: le binaire `tailscale` est introuvable "
            "dans PATH. Installer via https://tailscale.com/download ou "
            "définir REMOTE_CONTROL_TAILSCALE_IP_OVERRIDE pour les tests.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RemoteControlBootError(
            f"tailscale_timeout: `tailscale ip -4` sans réponse en "
            f"{_TAILSCALE_CMD_TIMEOUT_SECONDS}s. Le daemon `tailscaled` "
            "est-il démarré ? (`systemctl status tailscaled`)",
        ) from exc

    if result.returncode != 0:
        raise RemoteControlBootError(
            f"tailscale_command_failed: returncode={result.returncode}, "
            f"stderr={result.stderr.strip()!r}. Machine enrôlée "
            "dans un tailnet ? (`tailscale up` requis)",
        )

    stdout_stripped = result.stdout.strip()
    if not stdout_stripped:
        raise RemoteControlBootError(
            "tailscale_no_ipv4: `tailscale ip -4` a retourné stdout vide. "
            "Machine non enrôlée dans un tailnet ? Lancer `tailscale up` "
            "et vérifier `tailscale status`.",
        )

    first_line = stdout_stripped.splitlines()[0].strip()
    try:
        ip = ipaddress.ip_address(first_line)
    except ValueError as exc:
        raise RemoteControlBootError(
            f"tailscale_invalid_ipv4: sortie non parseable comme IP : {first_line!r}.",
        ) from exc

    if not isinstance(ip, ipaddress.IPv4Address):
        raise RemoteControlBootError(
            f"tailscale_not_ipv4: {first_line!r} n'est pas une IPv4.",
        )

    if ip not in _TAILSCALE_CGNAT_RANGE:
        raise RemoteControlBootError(
            f"tailscale_not_in_cgnat_range: {first_line!r} hors plage "
            f"{_TAILSCALE_CGNAT_RANGE} — Tailscale utilise systématiquement "
            "cette plage pour les IPs de tailnet. Résultat suspect, refuse "
            "de bind.",
        )

    log.info("remote_control_tailscale_ip_resolved", ip=first_line)
    return first_line


def resolve_tailnet_name(settings: Settings) -> str | None:
    """Best-effort : retourne le tailnet MagicDNS suffix ou ``None`` (M12_bis Phase G).

    Contrairement à ``resolve_tailscale_ipv4``, cette fonction **ne lève
    JAMAIS** — tout échec (binaire absent, daemon down, stdout non-JSON,
    MagicDNS désactivé, override invalide) est loggé en WARNING et
    retourne ``None``. L'appelant (``compute_dashboard_url``) est
    responsable du fallback localhost.

    Args:
        settings: Pydantic ``Settings`` — consomme ``tailnet_name``
            (déjà validé regex + lowercase par Pydantic).

    Returns:
        Tailnet suffix sous forme ``<nom>.ts.net`` (ex. ``"taila157fd.ts.net"``)
        ou ``None`` si non résoluble.
    """
    if settings.tailnet_name is not None:
        log.info("tailnet_name_override_used", tailnet=settings.tailnet_name)
        return settings.tailnet_name

    try:
        result = subprocess.run(  # noqa: S603 — argv list hardcoded, pas de shell=True
            ["tailscale", "status", "--json"],  # noqa: S607 — binary lookup via PATH
            capture_output=True,
            text=True,
            timeout=_TAILSCALE_CMD_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        log.warning("tailnet_name_resolution_failed", reason="tailscale_not_installed")
        return None
    except subprocess.TimeoutExpired:
        log.warning("tailnet_name_resolution_failed", reason="tailscale_timeout")
        return None

    if result.returncode != 0:
        log.warning(
            "tailnet_name_resolution_failed",
            reason="cmd_failed",
            stderr=result.stderr.strip()[:200],
        )
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("tailnet_name_resolution_failed", reason="json_decode_error")
        return None

    if not isinstance(data, dict):
        log.warning("tailnet_name_resolution_failed", reason="json_not_object")
        return None

    current = data.get("CurrentTailnet")
    if not isinstance(current, dict):
        log.warning("tailnet_name_resolution_failed", reason="no_current_tailnet")
        return None

    suffix = current.get("MagicDNSSuffix")
    if not isinstance(suffix, str) or not suffix.strip():
        log.warning(
            "tailnet_name_resolution_failed",
            reason="empty_or_magicdns_disabled",
        )
        return None

    normalized = suffix.strip().lower()
    log.info("tailnet_name_resolved", tailnet=normalized, source="tailscale_status")
    return normalized
