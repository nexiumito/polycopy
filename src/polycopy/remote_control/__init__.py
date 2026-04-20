"""Package ``remote_control`` — contrôle à distance via Tailscale (M12_bis).

API publique :
- ``RemoteControlBootError`` — levée au boot si Tailscale indisponible.
- ``resolve_tailscale_ipv4`` — résout l'IPv4 bindée (override ou
  ``tailscale ip -4`` runtime).

L'orchestrateur et le serveur FastAPI sont exposés au fur et à mesure des
phases B/C/D. Cf. spec ``docs/specs/M12_bis_multi_machine_remote_control_spec.md``.
"""

from __future__ import annotations

from polycopy.remote_control.orchestrator import RemoteControlOrchestrator
from polycopy.remote_control.server import build_app
from polycopy.remote_control.tailscale import (
    RemoteControlBootError,
    resolve_tailscale_ipv4,
)

__all__ = [
    "RemoteControlBootError",
    "RemoteControlOrchestrator",
    "build_app",
    "resolve_tailscale_ipv4",
]
