"""Tests end-to-end des POST handlers ``/restart /stop /resume`` (M12_bis §4.3).

Couvre le pipeline complet via ``TestClient`` :
- Auth flow : TOTP valide → 202, invalid → 401, triggering lockdown → 423.
- Rate limiter : 6e tentative sur 5/min → 429.
- Sentinel lifecycle : `/stop` pose, `/resume` enlève, `/resume` sans
  sentinel → 409.
- Side effects : `stop_event.set()` appelé.
- Machine mismatch → 404 body vide (silence strict).
- Lockdown préexistant → 423 sans consulter rate limiter ni TOTP.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyotp
from fastapi.testclient import TestClient

from polycopy.config import Settings
from polycopy.monitoring.dtos import Alert
from polycopy.remote_control import (
    AutoLockdown,
    RateLimiter,
    SentinelFile,
    TOTPGuard,
    build_app,
)

_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


@dataclass
class _Harness:
    client: TestClient
    sentinel: SentinelFile
    stop_event: asyncio.Event
    alerts: asyncio.Queue[Alert]
    rate_limiter: RateLimiter


def _harness(
    tmp_path: Path,
    *,
    machine_id: str = "PC-FIXE",
    rate_limit_max: int = 5,
    lockdown_max: int = 3,
) -> _Harness:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        machine_id=machine_id,
        remote_control_totp_secret=_TOTP_SECRET,
    )
    sentinel = SentinelFile(tmp_path / "halt.flag")
    totp = TOTPGuard(_TOTP_SECRET)
    rate = RateLimiter(max_attempts=rate_limit_max, window_seconds=60.0)
    alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
    lockdown = AutoLockdown(sentinel=sentinel, alerts_queue=alerts, max_failures=lockdown_max)
    stop_event = asyncio.Event()
    app = build_app(
        settings,
        boot_at=datetime.now(tz=UTC),
        stop_event=stop_event,
        sentinel=sentinel,
        totp_guard=totp,
        rate_limiter=rate,
        lockdown=lockdown,
    )
    return _Harness(TestClient(app), sentinel, stop_event, alerts, rate)


def _valid_totp() -> str:
    return pyotp.TOTP(_TOTP_SECRET).now()


# ===========================================================================
# /restart happy path
# ===========================================================================


def test_restart_valid_totp_returns_202_and_sets_stop_event(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    response = h.client.post("/v1/restart/PC-FIXE", json={"totp": _valid_totp()})
    assert response.status_code == 202
    body = response.json()
    assert body["ok"] is True
    assert body["action"] == "restart"
    assert body["respawn_mode"] == "running"
    assert h.stop_event.is_set() is True
    # Restart NE pose PAS le sentinel.
    assert h.sentinel.exists() is False


def test_restart_case_insensitive_machine_match(tmp_path: Path) -> None:
    h = _harness(tmp_path, machine_id="PC-FIXE")
    response = h.client.post("/v1/restart/pc-fixe", json={"totp": _valid_totp()})
    assert response.status_code == 202


# ===========================================================================
# /stop happy path
# ===========================================================================


def test_stop_posts_sentinel_and_sets_stop_event(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    response = h.client.post("/v1/stop/PC-FIXE", json={"totp": _valid_totp()})
    assert response.status_code == 202
    body = response.json()
    assert body["respawn_mode"] == "paused"
    assert h.sentinel.exists() is True
    assert h.sentinel.reason() == "manual_stop"
    assert h.stop_event.is_set() is True


# ===========================================================================
# /resume happy path + 409 not_paused
# ===========================================================================


def test_resume_without_sentinel_returns_409(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    response = h.client.post("/v1/resume/PC-FIXE", json={"totp": _valid_totp()})
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "not_paused"
    assert h.stop_event.is_set() is False


def test_resume_clears_sentinel_when_paused(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.sentinel.touch(reason="manual_stop")
    response = h.client.post("/v1/resume/PC-FIXE", json={"totp": _valid_totp()})
    assert response.status_code == 202
    body = response.json()
    assert body["respawn_mode"] == "running"
    assert h.sentinel.exists() is False
    assert h.stop_event.is_set() is True


# ===========================================================================
# TOTP invalide → 401 (sans triggering lockdown au 1er échec)
# ===========================================================================


def test_invalid_totp_returns_401(tmp_path: Path) -> None:
    h = _harness(tmp_path, lockdown_max=3)
    response = h.client.post("/v1/restart/PC-FIXE", json={"totp": "000000"})
    assert response.status_code == 401
    assert h.stop_event.is_set() is False


def test_malformed_totp_returns_401(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    response = h.client.post("/v1/restart/PC-FIXE", json={"totp": "not-a-code"})
    assert response.status_code == 401


def test_missing_totp_field_returns_422(tmp_path: Path) -> None:
    """Pydantic rejette un body sans le champ `totp` — FastAPI 422 par défaut."""
    h = _harness(tmp_path)
    response = h.client.post("/v1/restart/PC-FIXE", json={})
    assert response.status_code == 422


# ===========================================================================
# 3-strikes lockdown → 423 Locked + alerte émise
# ===========================================================================


def test_three_invalid_totps_trigger_lockdown(tmp_path: Path) -> None:
    h = _harness(tmp_path, rate_limit_max=5, lockdown_max=3)
    for _ in range(2):
        h.client.post("/v1/restart/PC-FIXE", json={"totp": "000000"})
    # Le 3e échec DÉCLENCHE le lockdown → réponse 423 directe.
    r3 = h.client.post("/v1/restart/PC-FIXE", json={"totp": "000000"})
    assert r3.status_code == 423
    assert h.sentinel.exists() is True
    assert h.sentinel.reason() == "auto_lockdown_brute_force"
    # Une alerte Telegram a été poussée.
    assert h.alerts.qsize() == 1
    alert = h.alerts.get_nowait()
    assert alert.event == "remote_control_brute_force_detected"
    assert alert.level == "CRITICAL"


def test_post_lockdown_further_requests_return_423(tmp_path: Path) -> None:
    """Après lockdown, TOUTES les requêtes destructives renvoient 423."""
    h = _harness(tmp_path, lockdown_max=1)
    h.client.post("/v1/restart/PC-FIXE", json={"totp": "000000"})  # triggers lockdown
    # Même un TOTP valide ne passe plus.
    response = h.client.post("/v1/restart/PC-FIXE", json={"totp": _valid_totp()})
    assert response.status_code == 423


def test_preexisting_sentinel_does_not_block_restart(tmp_path: Path) -> None:
    """Un sentinel préexistant (ex. kill switch passé) ne doit PAS bloquer.

    La sémantique "jusqu'à respawn" (§4.4.5) implique que chaque nouveau
    process redémarre avec ``_locked=False`` même si le sentinel est
    resté posé. Cela permet à ``/resume`` de servir dans un process
    fraîchement respawné en mode paused (Phase D).
    """
    h = _harness(tmp_path)
    h.sentinel.touch(reason="previous_run_kill_switch")
    response = h.client.post("/v1/restart/PC-FIXE", json={"totp": _valid_totp()})
    # 202 = le /restart est accepté ; pas 423.
    assert response.status_code == 202


# ===========================================================================
# Rate limiter → 429
# ===========================================================================


def test_rate_limit_exceeded_returns_429(tmp_path: Path) -> None:
    h = _harness(tmp_path, rate_limit_max=2, lockdown_max=100)
    # Épuise le quota avec TOTP valides (évite le lockdown au passage).
    h.client.post("/v1/restart/PC-FIXE", json={"totp": _valid_totp()})
    # stop_event est set dès le 1er /restart — le harness permet à la 2e
    # requête d'être servie car le server uvicorn n'est pas démarré (TestClient).
    # Le rate limiter trace toujours les tentatives indépendamment.
    h.client.post("/v1/restart/PC-FIXE", json={"totp": _valid_totp()})
    # 3e tentative : rate limited
    r3 = h.client.post("/v1/restart/PC-FIXE", json={"totp": _valid_totp()})
    assert r3.status_code == 429
    body = r3.json()
    assert body["error"] == "rate_limited"


# ===========================================================================
# Machine mismatch → 404 body vide (silence strict) AVANT auth
# ===========================================================================


def test_machine_mismatch_returns_404_empty(tmp_path: Path) -> None:
    h = _harness(tmp_path, machine_id="PC-FIXE")
    response = h.client.post("/v1/restart/OTHER-MACHINE", json={"totp": _valid_totp()})
    assert response.status_code == 404
    assert response.content == b""


def test_machine_mismatch_does_not_consume_rate_limit(tmp_path: Path) -> None:
    """404 de machine doit être AVANT le rate limiter (§4.3.3)."""
    h = _harness(tmp_path, machine_id="PC-FIXE", rate_limit_max=1)
    # Scanner 10 fois un machine_id inexistant ne doit PAS épuiser le
    # rate limiter de PC-FIXE.
    for _ in range(10):
        h.client.post("/v1/restart/SCANNED", json={"totp": _valid_totp()})
    # PC-FIXE dispose toujours de son quota.
    response = h.client.post("/v1/restart/PC-FIXE", json={"totp": _valid_totp()})
    assert response.status_code == 202


# ===========================================================================
# /status reflète le sentinel (paused + halt_reason)
# ===========================================================================


def test_status_reports_paused_when_sentinel_present(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.sentinel.touch(reason="manual_stop")
    response = h.client.get("/v1/status/PC-FIXE")
    body = response.json()
    assert body["mode"] == "paused"
    assert body["halt_reason"] == "manual_stop"


def test_status_reports_running_when_no_sentinel(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    response = h.client.get("/v1/status/PC-FIXE")
    body = response.json()
    assert body["mode"] == "running"
    assert body["halt_reason"] is None
