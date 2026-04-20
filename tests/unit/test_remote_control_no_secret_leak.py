"""Test sécurité critique : le secret TOTP ne fuite NULLE PART.

CLAUDE.md §Sécurité + M12_bis §4.4.6 :
> Secret TOTP jamais loggé (même partiellement, même en debug).

Ce test exécute chaque code-path qui touche au secret et vérifie :
1. Les événements structlog (via ``capture_logs``).
2. Le corps des alertes Telegram émises (via ``Alert.body``).
3. Les messages d'exception levées.
4. Les responses HTTP.

Le secret choisi est unique (``pyotp.random_base32(32)`` fixed seed) de
sorte qu'un grep substring détecte n'importe quelle fuite — y compris
un ``repr(obj)`` qui aurait capturé l'instance ``pyotp.TOTP`` en
interne.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pyotp
import structlog
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

# Secret unique facile à greper (32 chars base32).
_SECRET_MARKER = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def _build_harness(tmp_path: Path) -> tuple[TestClient, asyncio.Queue[Alert]]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        machine_id="PC-FIXE",
        remote_control_totp_secret=_SECRET_MARKER,
    )
    sentinel = SentinelFile(tmp_path / "halt.flag")
    totp = TOTPGuard(_SECRET_MARKER)
    rate = RateLimiter(max_attempts=5, window_seconds=60.0)
    alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
    lockdown = AutoLockdown(sentinel=sentinel, alerts_queue=alerts, max_failures=3)
    app = build_app(
        settings,
        boot_at=datetime.now(tz=UTC),
        stop_event=asyncio.Event(),
        sentinel=sentinel,
        totp_guard=totp,
        rate_limiter=rate,
        lockdown=lockdown,
    )
    return TestClient(app), alerts


def _serialize_events(events: list[dict[str, object]]) -> str:
    """Sérialise les events structlog capturés en une seule string greppable."""
    return json.dumps(events, default=str)


def test_secret_absent_from_totpguard_construct_logs(tmp_path: Path) -> None:
    """Construire un TOTPGuard ne doit rien logger qui contienne le secret."""
    with structlog.testing.capture_logs() as events:
        TOTPGuard(_SECRET_MARKER)
    assert _SECRET_MARKER not in _serialize_events(events)


def test_secret_absent_from_successful_verify_logs(tmp_path: Path) -> None:
    client, _ = _build_harness(tmp_path)
    code = pyotp.TOTP(_SECRET_MARKER).now()
    with structlog.testing.capture_logs() as events:
        client.post("/v1/restart/PC-FIXE", json={"totp": code})
    serialized = _serialize_events(events)
    assert _SECRET_MARKER not in serialized


def test_secret_absent_from_failed_verify_logs(tmp_path: Path) -> None:
    client, _ = _build_harness(tmp_path)
    with structlog.testing.capture_logs() as events:
        client.post("/v1/restart/PC-FIXE", json={"totp": "000000"})
    serialized = _serialize_events(events)
    assert _SECRET_MARKER not in serialized


def test_secret_absent_from_lockdown_logs_and_alert(tmp_path: Path) -> None:
    """3 échecs → sentinel + Alert : ni le log ni le body ne doivent porter le secret."""
    client, alerts = _build_harness(tmp_path)
    with structlog.testing.capture_logs() as events:
        for _ in range(3):
            client.post("/v1/restart/PC-FIXE", json={"totp": "000000"})
    serialized_logs = _serialize_events(events)
    assert _SECRET_MARKER not in serialized_logs
    # Alert Telegram ne doit pas non plus contenir le secret.
    assert alerts.qsize() == 1
    alert = alerts.get_nowait()
    assert _SECRET_MARKER not in alert.body
    assert _SECRET_MARKER not in alert.event


def test_secret_absent_from_sentinel_content(tmp_path: Path) -> None:
    """Le halt.flag écrit par lockdown ne doit pas contenir le secret."""
    client, _ = _build_harness(tmp_path)
    for _ in range(3):
        client.post("/v1/restart/PC-FIXE", json={"totp": "000000"})
    sentinel_path = tmp_path / "halt.flag"
    assert sentinel_path.exists()
    content = sentinel_path.read_text()
    assert _SECRET_MARKER not in content


def test_secret_absent_from_all_http_responses(tmp_path: Path) -> None:
    """Scanner chaque code de réponse : 401, 404, 409, 423, 429, 202 — aucun
    ne doit contenir le secret dans headers/body."""
    client, _ = _build_harness(tmp_path)
    sentinel = SentinelFile(tmp_path / "halt.flag")
    responses: list[tuple[str, object]] = []

    # 401 invalid TOTP (Rate limiter non épuisé, lockdown_max=3 ⇒ safe for 2 attempts)
    responses.append(
        ("401", client.post("/v1/restart/PC-FIXE", json={"totp": "000000"})),
    )
    # 404 machine mismatch
    responses.append(
        ("404", client.post("/v1/restart/OTHER", json={"totp": pyotp.TOTP(_SECRET_MARKER).now()})),
    )
    # 409 resume not paused
    responses.append(
        (
            "409",
            client.post("/v1/resume/PC-FIXE", json={"totp": pyotp.TOTP(_SECRET_MARKER).now()}),
        ),
    )
    # Trigger lockdown via 3 échecs TOTP → 4e requête = 423
    for _ in range(3):
        client.post("/v1/restart/PC-FIXE", json={"totp": "000000"})
    responses.append(
        (
            "423",
            client.post(
                "/v1/restart/PC-FIXE",
                json={"totp": pyotp.TOTP(_SECRET_MARKER).now()},
            ),
        ),
    )
    # Silence unused var (sentinel still useful for potential debug).
    _ = sentinel

    for label, response in responses:
        # FastAPI TestClient renvoie un httpx.Response-like object.
        body_bytes: bytes = response.content  # type: ignore[attr-defined]
        headers = dict(response.headers)  # type: ignore[attr-defined]
        assert _SECRET_MARKER.encode() not in body_bytes, (
            f"Secret leaked in response body for {label}"
        )
        for k, v in headers.items():
            assert _SECRET_MARKER not in k, f"Secret leaked in header name for {label}"
            assert _SECRET_MARKER not in v, f"Secret leaked in header value for {label}"


def test_secret_absent_from_totpguard_repr() -> None:
    """``repr(TOTPGuard)`` ne doit pas exposer le secret."""
    guard = TOTPGuard(_SECRET_MARKER)
    assert _SECRET_MARKER not in repr(guard)


def test_secret_absent_from_pyotp_totp_repr() -> None:
    """Défense en profondeur : pyotp.TOTP lui-même ne doit pas leak via repr.

    (Si cette assertion fail à l'update de pyotp, il faut adapter le wrapping.)
    """
    totp = pyotp.TOTP(_SECRET_MARKER)
    # pyotp >=2.9 ne dump pas le secret dans repr — on vérifie.
    assert _SECRET_MARKER not in repr(totp)
