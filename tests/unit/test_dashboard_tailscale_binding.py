"""Tests ``DashboardOrchestrator`` avec ``DASHBOARD_BIND_TAILSCALE`` (M12_bis §4.7)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard.orchestrator import DashboardOrchestrator
from polycopy.remote_control import RemoteControlBootError


def _fake_sf() -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", MagicMock())


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


# ===========================================================================
# Happy path : flag off → binding inchangé (backward compat M4.5/M6)
# ===========================================================================


def test_flag_off_uses_dashboard_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default `dashboard_bind_tailscale=False` → bind sur 127.0.0.1.
    subprocess NE doit PAS être appelé (pas de résolution Tailscale).
    """

    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run ne doit pas être appelé avec flag off")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    settings = _settings(dashboard_enabled=True)
    orch = DashboardOrchestrator(_fake_sf(), settings)
    assert orch._host == "127.0.0.1"  # noqa: SLF001


def test_flag_off_respects_custom_dashboard_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run ne doit pas être appelé avec flag off")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    settings = _settings(dashboard_enabled=True, dashboard_host="0.0.0.0")
    orch = DashboardOrchestrator(_fake_sf(), settings)
    assert orch._host == "0.0.0.0"  # noqa: SLF001


# ===========================================================================
# Flag on + override → bind sur Tailscale IP
# ===========================================================================


def test_flag_on_with_override_resolves_tailscale_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bind_tailscale=true` + override IP → utilise l'override, pas de subprocess."""

    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run ne doit pas être appelé avec override")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    settings = _settings(
        dashboard_enabled=True,
        dashboard_bind_tailscale=True,
        remote_control_tailscale_ip_override="100.64.0.1",
    )
    orch = DashboardOrchestrator(_fake_sf(), settings)
    assert orch._host == "100.64.0.1"  # noqa: SLF001


# ===========================================================================
# Fatal si Tailscale absent
# ===========================================================================


def test_flag_on_without_tailscale_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance §5 Phase E : `bind_tailscale=true` + Tailscale absent → boot fatal."""

    def _raise_not_found(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr("subprocess.run", _raise_not_found)
    settings = _settings(dashboard_enabled=True, dashboard_bind_tailscale=True)
    with pytest.raises(RemoteControlBootError) as exc:
        DashboardOrchestrator(_fake_sf(), settings)
    assert "tailscale_not_installed" in str(exc.value)


def test_flag_on_without_tailscale_stdout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Machine non-enrôlée tailnet (stdout vide) → crash boot clair."""
    import subprocess

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(
            args=["tailscale", "ip", "-4"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    settings = _settings(dashboard_enabled=True, dashboard_bind_tailscale=True)
    with pytest.raises(RemoteControlBootError) as exc:
        DashboardOrchestrator(_fake_sf(), settings)
    assert "tailscale_no_ipv4" in str(exc.value)


# ===========================================================================
# Warnings de cohabitation (§4.7)
# ===========================================================================


def test_warning_when_bind_tailscale_with_dashboard_disabled() -> None:
    """Si `bind_tailscale=true` mais `dashboard_enabled=false` → warning informatif."""
    settings = _settings(
        dashboard_enabled=False,  # off
        dashboard_bind_tailscale=True,
        remote_control_tailscale_ip_override="100.64.0.1",
    )
    with structlog.testing.capture_logs() as events:
        DashboardOrchestrator(_fake_sf(), settings)
    warning_events = [
        e for e in events if e.get("event") == "dashboard_bind_tailscale_without_enabled_noop"
    ]
    assert len(warning_events) == 1


def test_warning_when_dashboard_host_overridden() -> None:
    """Si `bind_tailscale=true` + `DASHBOARD_HOST` explicite → warning + priorité Tailscale."""
    settings = _settings(
        dashboard_enabled=True,
        dashboard_bind_tailscale=True,
        dashboard_host="0.0.0.0",
        remote_control_tailscale_ip_override="100.64.0.1",
    )
    with structlog.testing.capture_logs() as events:
        orch = DashboardOrchestrator(_fake_sf(), settings)
    warning_events = [
        e for e in events if e.get("event") == "dashboard_host_overridden_by_tailscale_bind"
    ]
    assert len(warning_events) == 1
    assert warning_events[0].get("tailscale_host") == "100.64.0.1"
    assert warning_events[0].get("ignored_dashboard_host") == "0.0.0.0"
    # Le host retenu est bien l'IP Tailscale, pas 0.0.0.0.
    assert orch._host == "100.64.0.1"  # noqa: SLF001


def test_no_warnings_when_flag_off() -> None:
    """Backward compat : aucun warning émis avec `bind_tailscale=false`."""
    settings = _settings(dashboard_enabled=True)
    with structlog.testing.capture_logs() as events:
        DashboardOrchestrator(_fake_sf(), settings)
    warning_events = [e for e in events if "tailscale" in str(e.get("event", "")).lower()]
    assert warning_events == []


# ===========================================================================
# Non-régression sécurité M4.5/M6 : routes GET only, aucun secret
# ===========================================================================


def test_security_grep_routes_still_get_only() -> None:
    """Non-régression §4.7 : `DashboardOrchestrator` n'altère pas les routes.
    Le dashboard reste read-only — les tests M4.5/M6 existants couvrent
    les routes, ici on vérifie juste qu'instancier avec le flag n'injecte
    pas de middleware ou route supplémentaire.
    """
    from polycopy.dashboard.routes import build_app

    settings = _settings(
        dashboard_enabled=True,
        dashboard_bind_tailscale=True,
        remote_control_tailscale_ip_override="100.64.0.1",
    )
    # L'orchestrateur n'instancie PAS l'app à l'init — seul `build_app` le fait.
    # On re-build l'app avec les mêmes settings pour vérifier.
    app = build_app(_fake_sf(), settings)
    get_methods = {
        method for route in app.routes for method in getattr(route, "methods", set()) or set()
    }
    # Aucune méthode mutante (POST/PUT/DELETE/PATCH) ne doit apparaître.
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert method not in get_methods, f"Dashboard expose une route {method} (interdit M4.5/M6)"
