"""Tests config ``REMOTE_CONTROL_*`` (M12_bis Phase B).

Couvre :
- defaults opt-in strict (``REMOTE_CONTROL_ENABLED=False``, port 8765).
- validation range port (1024-65535).
- validator ``_validate_remote_control_ip_override`` : IPv4 only, refus
  loopback + unspecified + string non-IP.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from polycopy.config import Settings


def _make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_remote_control_defaults() -> None:
    s = _make()
    assert s.remote_control_enabled is False
    assert s.remote_control_port == 8765
    assert s.remote_control_tailscale_ip_override is None


def test_remote_control_enabled_override() -> None:
    s = _make(remote_control_enabled=True)
    assert s.remote_control_enabled is True


# ---------------------------------------------------------------------------
# Port range
# ---------------------------------------------------------------------------


def test_remote_control_port_valid_range() -> None:
    s = _make(remote_control_port=9876)
    assert s.remote_control_port == 9876


def test_remote_control_port_below_privileged_range_rejected() -> None:
    """Ports privilégiés (<1024) refusés (pas root needed pour bind)."""
    with pytest.raises(ValidationError):
        _make(remote_control_port=80)


def test_remote_control_port_over_max_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(remote_control_port=70000)


# ---------------------------------------------------------------------------
# Tailscale IP override validator
# ---------------------------------------------------------------------------


def test_ip_override_valid_tailscale_cgnat_accepted() -> None:
    """IP dans la plage Tailscale CGNAT 100.64.0.0/10."""
    s = _make(remote_control_tailscale_ip_override="100.64.0.1")
    assert s.remote_control_tailscale_ip_override == "100.64.0.1"


def test_ip_override_valid_private_accepted_for_tests() -> None:
    """Autre IPv4 privée OK (le validator ne restreint pas à CGNAT — la
    vérification CGNAT vit dans ``resolve_tailscale_ipv4`` runtime)."""
    s = _make(remote_control_tailscale_ip_override="192.168.1.42")
    assert s.remote_control_tailscale_ip_override == "192.168.1.42"


def test_ip_override_loopback_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _make(remote_control_tailscale_ip_override="127.0.0.1")
    assert "loopback" in str(exc_info.value).lower()


def test_ip_override_127_any_loopback_rejected() -> None:
    """Toute la plage 127.x.x.x est loopback, pas seulement 127.0.0.1."""
    with pytest.raises(ValidationError):
        _make(remote_control_tailscale_ip_override="127.5.0.1")


def test_ip_override_unspecified_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _make(remote_control_tailscale_ip_override="0.0.0.0")
    assert "0.0.0.0" in str(exc_info.value)


def test_ip_override_ipv6_rejected() -> None:
    """IPv6 rejeté (Tailscale MagicDNS résout en IPv4 dans notre setup)."""
    with pytest.raises(ValidationError) as exc_info:
        _make(remote_control_tailscale_ip_override="::1")
    assert "ipv4" in str(exc_info.value).lower()


def test_ip_override_not_an_ip_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(remote_control_tailscale_ip_override="not-an-ip")


def test_ip_override_empty_treated_as_none() -> None:
    """Env var set à string vide → normalisé en None (pas un crash)."""
    s = _make(remote_control_tailscale_ip_override="")
    assert s.remote_control_tailscale_ip_override is None


def test_ip_override_whitespace_treated_as_none() -> None:
    s = _make(remote_control_tailscale_ip_override="   ")
    assert s.remote_control_tailscale_ip_override is None
