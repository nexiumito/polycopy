"""Tests pour ``compute_dashboard_url`` + ``resolve_tailnet_name`` (M12_bis Phase G).

Couvre tous les chemins déterministes :

- dashboard désactivé → None
- bind tailscale OFF → URL localhost classique
- bind tailscale ON + tailnet override + machine_id → URL Tailscale
- bind tailscale ON + Tailscale absent → fallback localhost (best-effort)
- bind tailscale ON + JSON invalide → fallback localhost
- bind tailscale ON + MagicDNS désactivé → fallback localhost
- bind tailscale ON + machine_id None → fallback localhost
- tailnet_name invalide côté env → ValueError au boot Settings
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from polycopy.config import Settings
from polycopy.monitoring.dashboard_url import compute_dashboard_url
from polycopy.remote_control.tailscale import resolve_tailnet_name


def _settings(**overrides: Any) -> Settings:  # noqa: ANN401
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# compute_dashboard_url
# ---------------------------------------------------------------------------


def test_returns_none_when_dashboard_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``dashboard_enabled=False`` → pas d'URL, pas de subprocess."""

    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run ne doit pas être appelé avec dashboard disabled")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    settings = _settings(dashboard_enabled=False)
    assert compute_dashboard_url(settings) is None


def test_returns_localhost_when_bind_tailscale_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bind_tailscale=False`` → URL ``http://127.0.0.1:8787/`` (backward compat M7)."""

    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run ne doit pas être appelé")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    settings = _settings(dashboard_enabled=True)
    url = compute_dashboard_url(settings)
    assert url == "http://127.0.0.1:8787/"


def test_returns_localhost_with_custom_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le host/port par défaut DASHBOARD_HOST/DASHBOARD_PORT est honoré."""

    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run ne doit pas être appelé")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    settings = _settings(
        dashboard_enabled=True,
        dashboard_host="0.0.0.0",
        dashboard_port=9090,
    )
    assert compute_dashboard_url(settings) == "http://0.0.0.0:9090/"


def test_returns_tailscale_url_when_bind_tailscale_and_override_and_machine_id() -> None:
    """``bind_tailscale=True`` + override + ``machine_id`` → URL Tailscale cliquable.

    ``machine_id`` est normalisé en upper par Pydantic mais l'URL doit le
    lowercase (DNS n'est pas case-sensitive, et le convention Tailscale
    MagicDNS hostname est lowercase).
    """
    settings = _settings(
        dashboard_enabled=True,
        dashboard_bind_tailscale=True,
        machine_id="PC-FIXE",
        tailnet_name="taila157fd.ts.net",
    )
    url = compute_dashboard_url(settings)
    assert url == "http://pc-fixe.taila157fd.ts.net:8787/"


def test_returns_tailscale_url_honors_custom_port() -> None:
    settings = _settings(
        dashboard_enabled=True,
        dashboard_bind_tailscale=True,
        dashboard_port=9999,
        machine_id="MACBOOK",
        tailnet_name="alpha-beta.ts.net",
    )
    assert compute_dashboard_url(settings) == "http://macbook.alpha-beta.ts.net:9999/"


def test_fallback_localhost_when_tailscale_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort : `tailscale` absent + pas d'override → fallback localhost (ne lève pas)."""

    def _raise_not_found(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr("subprocess.run", _raise_not_found)
    settings = _settings(
        dashboard_enabled=True,
        dashboard_bind_tailscale=True,
        machine_id="PC-FIXE",
    )
    assert compute_dashboard_url(settings) == "http://127.0.0.1:8787/"


def test_fallback_localhost_when_machine_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bind_tailscale=True`` + tailnet résolu MAIS ``machine_id=None`` →
    fallback localhost (on ne sait pas construire l'hostname).
    """

    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run ne doit pas être appelé — override utilisé")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    # On utilise l'override pour éviter l'auto-résolution hostname qui
    # fournirait un fallback ``machine_id`` non-None.
    # Malheureusement ``_resolve_machine_id`` remplit toujours machine_id.
    # On simule donc via un settings custom où machine_id est explicitement
    # à None — impossible via env, donc on patch l'attribut après construction.
    settings = _settings(
        dashboard_enabled=True,
        dashboard_bind_tailscale=True,
        tailnet_name="taila157fd.ts.net",
    )
    # ``machine_id`` est résolu au boot ; on le force à None pour simuler
    # un edge case théorique.
    object.__setattr__(settings, "machine_id", None)
    url = compute_dashboard_url(settings)
    assert url == "http://127.0.0.1:8787/"


def test_invalid_tailnet_name_raises_validation_error() -> None:
    """Pydantic refuse un ``TAILNET_NAME`` mal formé au boot (fail-fast)."""
    with pytest.raises(ValidationError) as exc:
        _settings(tailnet_name="not_a_tailnet_name")
    assert "TAILNET_NAME" in str(exc.value)


def test_tailnet_name_empty_string_is_normalized_to_none() -> None:
    """Env var vide → None (cohérent avec env var non-settée)."""
    settings = _settings(tailnet_name="")
    assert settings.tailnet_name is None


def test_tailnet_name_uppercase_is_lowercased() -> None:
    """Pydantic normalise en lowercase avant validation regex."""
    settings = _settings(tailnet_name="Taila157FD.ts.net")
    assert settings.tailnet_name == "taila157fd.ts.net"


# ---------------------------------------------------------------------------
# resolve_tailnet_name (best-effort, ne lève jamais)
# ---------------------------------------------------------------------------


def test_resolve_returns_override_without_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _should_not_run(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("subprocess.run ne doit pas être appelé avec override")

    monkeypatch.setattr("subprocess.run", _should_not_run)
    settings = _settings(tailnet_name="my-tailnet.ts.net")
    assert resolve_tailnet_name(settings) == "my-tailnet.ts.net"


def test_resolve_parses_magic_dns_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"CurrentTailnet": {"MagicDNSSuffix": "taila157fd.ts.net"}}

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(
            args=["tailscale", "status", "--json"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    settings = _settings()
    assert resolve_tailnet_name(settings) == "taila157fd.ts.net"


def test_resolve_returns_none_on_filenotfound(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_not_found(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr("subprocess.run", _raise_not_found)
    assert resolve_tailnet_name(_settings()) is None


def test_resolve_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_timeout(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise subprocess.TimeoutExpired(cmd="tailscale", timeout=5)

    monkeypatch.setattr("subprocess.run", _raise_timeout)
    assert resolve_tailnet_name(_settings()) is None


def test_resolve_returns_none_on_nonzero_returncode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(
            args=["tailscale", "status", "--json"],
            returncode=1,
            stdout="",
            stderr="not logged in",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    assert resolve_tailnet_name(_settings()) is None


def test_resolve_returns_none_on_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(
            args=["tailscale", "status", "--json"],
            returncode=0,
            stdout="not{valid json",
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    assert resolve_tailnet_name(_settings()) is None


def test_resolve_returns_none_when_magicdns_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"CurrentTailnet": {"MagicDNSSuffix": ""}}

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(
            args=["tailscale", "status", "--json"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    assert resolve_tailnet_name(_settings()) is None


def test_resolve_returns_none_when_no_current_tailnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {"Self": {"HostName": "pc-fixe"}}

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(
            args=["tailscale", "status", "--json"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    assert resolve_tailnet_name(_settings()) is None


def test_resolve_never_raises_even_on_unusual_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test de robustesse : quoi qu'il arrive, la fonction retourne None."""

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        return subprocess.CompletedProcess(
            args=["tailscale", "status", "--json"],
            returncode=0,
            stdout="42",  # JSON valide mais pas un object
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    # JSON "42" parse en int, pas en dict — on attend None sans raise.
    assert resolve_tailnet_name(_settings()) is None
