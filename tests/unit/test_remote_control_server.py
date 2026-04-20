"""Tests FastAPI smoke ``GET /v1/health`` + ``GET /v1/status/<machine>``.

Cf. spec M12_bis §4.3. Phase B minimal — tests plus larges
(régression schéma, rate limit) dans ``test_remote_control_phaseb_routes.py``
au commit #5.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from polycopy.config import Settings
from polycopy.remote_control import build_app


def _settings(machine_id: str = "PC-FIXE", **extra: object) -> Settings:
    return Settings(_env_file=None, machine_id=machine_id, **extra)  # type: ignore[call-arg]


def _client(machine_id: str = "PC-FIXE") -> TestClient:
    settings = _settings(machine_id=machine_id)
    app = build_app(settings, boot_at=datetime.now(tz=UTC))
    return TestClient(app)


# ---------------------------------------------------------------------------
# /v1/health
# ---------------------------------------------------------------------------


def test_health_returns_200_ok() -> None:
    response = _client().get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# /v1/status/<machine>
# ---------------------------------------------------------------------------


def test_status_matching_machine_returns_200() -> None:
    response = _client(machine_id="PC-FIXE").get("/v1/status/PC-FIXE")
    assert response.status_code == 200
    body = response.json()
    assert body["machine_id"] == "PC-FIXE"
    assert body["mode"] == "running"
    assert body["execution_mode"] in {"simulation", "dry_run", "live"}


def test_status_case_insensitive_match() -> None:
    response = _client(machine_id="PC-FIXE").get("/v1/status/pc-fixe")
    assert response.status_code == 200


def test_status_mismatch_returns_404_empty_body() -> None:
    response = _client(machine_id="PC-FIXE").get("/v1/status/OTHER-MACHINE")
    assert response.status_code == 404
    assert response.content == b""


def test_status_response_contains_required_fields() -> None:
    response = _client(machine_id="PC-FIXE").get("/v1/status/PC-FIXE")
    body = response.json()
    for field in ("machine_id", "mode", "uptime_seconds", "version", "execution_mode"):
        assert field in body


# ---------------------------------------------------------------------------
# OpenAPI / docs désactivés (invariant M4.5/M6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_endpoints_disabled(path: str) -> None:
    response = _client().get(path)
    assert response.status_code == 404
