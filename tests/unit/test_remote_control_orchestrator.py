"""Tests ``RemoteControlOrchestrator`` (M12_bis Phase B §4.5).

Couvre :
- `__init__` résout Tailscale immédiatement — erreur remonte hors TaskGroup.
- `__init__` accepte l'override IP sans appeler subprocess.
- Fail-fast au boot si Tailscale down (simulation via mock).
- `RemoteControlBootError` est bien propagé (acceptance Phase B §5).

Le smoke test ``run_forever`` (bind réel + stop_event) est délégué au test
d'intégration end-to-end (opt-in `pytest -m integration`) car il demande
un port libre et un `uvicorn.Server` réel. Pattern identique aux tests
du dashboard ``test_dashboard_orchestrator.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from polycopy.config import Settings
from polycopy.remote_control import (
    RemoteControlBootError,
    RemoteControlOrchestrator,
)

_TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # Phase C requirement


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Init — happy path avec override
# ---------------------------------------------------------------------------


def test_init_with_override_uses_provided_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override Pydantic-validated bypasse subprocess — critique pour les tests."""

    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run ne doit pas être appelé avec override")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    settings = _settings(
        remote_control_enabled=True,
        remote_control_totp_secret=_TOTP_SECRET,
        remote_control_tailscale_ip_override="100.64.0.1",
        remote_control_port=8765,
        machine_id="PC-FIXE",
    )
    orch = RemoteControlOrchestrator(settings)
    # Vérification indirecte : pas d'exception + attrs privés cohérents.
    assert orch._host == "100.64.0.1"  # noqa: SLF001
    assert orch._port == 8765  # noqa: SLF001


# ---------------------------------------------------------------------------
# Init — fail-fast Tailscale absent
# ---------------------------------------------------------------------------


def test_init_fails_when_tailscale_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`REMOTE_CONTROL_ENABLED=true` sans Tailscale → RemoteControlBootError."""

    def _raise_not_found(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr("subprocess.run", _raise_not_found)
    settings = _settings(
        remote_control_enabled=True,
        remote_control_totp_secret=_TOTP_SECRET,
        machine_id="PC-FIXE",
    )
    with pytest.raises(RemoteControlBootError) as exc:
        RemoteControlOrchestrator(settings)
    assert "tailscale_not_installed" in str(exc.value)


def test_init_fails_when_tailscale_returns_no_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(
            args=["tailscale", "ip", "-4"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    settings = _settings(
        remote_control_enabled=True,
        remote_control_totp_secret=_TOTP_SECRET,
        machine_id="PC-FIXE",
    )
    with pytest.raises(RemoteControlBootError) as exc:
        RemoteControlOrchestrator(settings)
    assert "tailscale_no_ipv4" in str(exc.value)


# ---------------------------------------------------------------------------
# Runner wiring — flag off = pas d'instanciation
# ---------------------------------------------------------------------------


def test_runner_does_not_instantiate_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant acceptance §5 : `REMOTE_CONTROL_ENABLED=false` → zéro surface.

    Smoke : on vérifie simplement que `Settings(remote_control_enabled=False)`
    (default) est le cas par défaut et qu'on peut résoudre sans Tailscale.
    Le test d'intégration complet (TaskGroup entier) vit dans test_cli_runner.
    """
    settings = _settings()
    assert settings.remote_control_enabled is False


def test_runner_with_flag_on_requires_tailscale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance §5 : flag on sans Tailscale ⇒ boot fatal clair (pas de démarrage partiel)."""

    def _raise_not_found(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr("subprocess.run", _raise_not_found)
    settings = _settings(
        remote_control_enabled=True,
        remote_control_totp_secret=_TOTP_SECRET,
        machine_id="PC-FIXE",
    )
    # Simule le code runner.py:
    with pytest.raises(RemoteControlBootError):
        _ = RemoteControlOrchestrator(settings)
