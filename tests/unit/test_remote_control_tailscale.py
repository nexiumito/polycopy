"""Tests ``resolve_tailscale_ipv4`` + ``RemoteControlBootError`` (M12_bis §4.4.1)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from polycopy.config import Settings
from polycopy.remote_control import RemoteControlBootError, resolve_tailscale_ipv4


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def _fake_completed(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["tailscale", "ip", "-4"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Override path (court-circuite subprocess)
# ---------------------------------------------------------------------------


def test_override_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si ``override`` set, on ne doit PAS appeler ``subprocess.run``."""

    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run appelé alors que override set")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    s = _settings(remote_control_tailscale_ip_override="100.64.0.1")
    assert resolve_tailscale_ipv4(s) == "100.64.0.1"


# ---------------------------------------------------------------------------
# Happy path subprocess
# ---------------------------------------------------------------------------


def test_resolve_valid_tailscale_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(stdout="100.64.0.1\n"),
    )
    s = _settings()
    assert resolve_tailscale_ipv4(s) == "100.64.0.1"


def test_resolve_picks_first_line_when_multiple_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tailscale peut retourner plusieurs lignes (IPv4 + alias) — prend la 1ʳᵉ."""
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(stdout="100.64.0.1\n100.64.0.2\n"),
    )
    s = _settings()
    assert resolve_tailscale_ipv4(s) == "100.64.0.1"


# ---------------------------------------------------------------------------
# Cas d'erreur
# ---------------------------------------------------------------------------


def test_tailscale_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise FileNotFoundError("tailscale binary not found")

    monkeypatch.setattr("subprocess.run", _raise)
    s = _settings()
    with pytest.raises(RemoteControlBootError) as exc:
        resolve_tailscale_ipv4(s)
    assert "tailscale_not_installed" in str(exc.value)


def test_tailscale_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise subprocess.TimeoutExpired(cmd=["tailscale"], timeout=5.0)

    monkeypatch.setattr("subprocess.run", _raise)
    s = _settings()
    with pytest.raises(RemoteControlBootError) as exc:
        resolve_tailscale_ipv4(s)
    assert "tailscale_timeout" in str(exc.value)


def test_tailscale_returncode_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(returncode=1, stderr="not logged in"),
    )
    s = _settings()
    with pytest.raises(RemoteControlBootError) as exc:
        resolve_tailscale_ipv4(s)
    assert "tailscale_command_failed" in str(exc.value)
    assert "not logged in" in str(exc.value)


def test_tailscale_empty_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(stdout=""),
    )
    s = _settings()
    with pytest.raises(RemoteControlBootError) as exc:
        resolve_tailscale_ipv4(s)
    assert "tailscale_no_ipv4" in str(exc.value)


def test_tailscale_whitespace_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(stdout="   \n"),
    )
    s = _settings()
    with pytest.raises(RemoteControlBootError) as exc:
        resolve_tailscale_ipv4(s)
    assert "tailscale_no_ipv4" in str(exc.value)


def test_tailscale_unparseable_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(stdout="garbage-not-an-ip\n"),
    )
    s = _settings()
    with pytest.raises(RemoteControlBootError) as exc:
        resolve_tailscale_ipv4(s)
    assert "tailscale_invalid_ipv4" in str(exc.value)


def test_tailscale_returns_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tailscale ip -4` ne devrait pas retourner d'IPv6, mais défense."""
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(stdout="fd7a:115c::1\n"),
    )
    s = _settings()
    with pytest.raises(RemoteControlBootError) as exc:
        resolve_tailscale_ipv4(s)
    assert "tailscale_not_ipv4" in str(exc.value)


def test_tailscale_ip_outside_cgnat_range_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Une IP valide mais hors 100.64.0.0/10 est suspecte → refus."""
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(stdout="10.0.0.1\n"),
    )
    s = _settings()
    with pytest.raises(RemoteControlBootError) as exc:
        resolve_tailscale_ipv4(s)
    assert "tailscale_not_in_cgnat_range" in str(exc.value)


def test_tailscale_ip_at_cgnat_boundary_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bornes 100.64.0.0/10 : 100.64.0.0 à 100.127.255.255."""
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(stdout="100.127.255.254\n"),
    )
    s = _settings()
    assert resolve_tailscale_ipv4(s) == "100.127.255.254"


def test_tailscale_ip_just_outside_cgnat_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``100.128.0.0`` est juste hors plage CGNAT."""
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed(stdout="100.128.0.0\n"),
    )
    s = _settings()
    with pytest.raises(RemoteControlBootError):
        resolve_tailscale_ipv4(s)
