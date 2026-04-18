"""Tests des routes M9 du dashboard : /logs + /partials/logs-tail.

Utilise `ASGITransport` pour ne jamais ouvrir de port. Fichier log fourni
via `tmp_path` à chaque test (isolation totale).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard.routes import build_app


def _write_log_file(path: Path, *, n_info: int = 5, n_error: int = 2) -> None:
    """Génère un petit fichier de logs JSON pour les tests."""
    lines: list[str] = []
    for i in range(n_info):
        lines.append(
            json.dumps(
                {
                    "event": "trade_detected",
                    "level": "info",
                    "wallet": f"0xinfo{i:04x}",
                    "timestamp": f"2026-04-18T10:0{i}:00Z",
                }
            )
        )
    for i in range(n_error):
        lines.append(
            json.dumps(
                {
                    "event": "executor_error",
                    "level": "error",
                    "reason": "test_reason",
                    "timestamp": f"2026-04-18T11:0{i}:00Z",
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")


@pytest_asyncio.fixture
async def client_with_logs(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, Path]]:
    log_file = tmp_path / "polycopy.log"
    _write_log_file(log_file)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        log_file=log_file,
        dashboard_logs_enabled=True,
        dashboard_logs_tail_lines=500,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client, log_file


@pytest_asyncio.fixture
async def client_logs_disabled(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        log_file=tmp_path / "polycopy.log",
        dashboard_logs_enabled=False,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_logs_page_renders_entries(
    client_with_logs: tuple[AsyncClient, Path],
) -> None:
    client, _ = client_with_logs
    res = await client.get("/logs")
    assert res.status_code == 200
    assert "trade_detected" in res.text
    # Préserve l'invariant test_dashboard_security_m6 (assert "M9" in res.text).
    assert "M9" in res.text


@pytest.mark.asyncio
async def test_logs_filter_by_level(
    client_with_logs: tuple[AsyncClient, Path],
) -> None:
    client, _ = client_with_logs
    res = await client.get("/logs?levels=ERROR")
    assert res.status_code == 200
    # Le filtre ne garde que les errors → wallet 0xinfo* (info) doit disparaître.
    # On évite de matcher sur "trade_detected" qui apparaît aussi dans le placeholder
    # du form de filtre. On match sur la valeur unique du wallet info.
    assert "executor_error" in res.text
    assert "0xinfo0001" not in res.text


@pytest.mark.asyncio
async def test_logs_filter_by_q_substring(
    client_with_logs: tuple[AsyncClient, Path],
) -> None:
    client, _ = client_with_logs
    res = await client.get("/logs?q=0xinfo0001")
    assert res.status_code == 200
    assert "0xinfo0001" in res.text
    # Les autres wallets info ne devraient pas apparaître dans les details.
    assert "0xinfo0003" not in res.text


@pytest.mark.asyncio
async def test_logs_filter_invalid_level_returns_400(
    client_with_logs: tuple[AsyncClient, Path],
) -> None:
    client, _ = client_with_logs
    res = await client.get("/logs?levels=BOGUS")
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_logs_filter_q_too_long_returns_422(
    client_with_logs: tuple[AsyncClient, Path],
) -> None:
    client, _ = client_with_logs
    res = await client.get("/logs?q=" + "x" * 201)
    assert res.status_code == 422  # Pydantic max_length


@pytest.mark.asyncio
async def test_logs_filter_too_many_events_returns_400(
    client_with_logs: tuple[AsyncClient, Path],
) -> None:
    client, _ = client_with_logs
    many = ",".join(f"e_{i}" for i in range(25))
    res = await client.get(f"/logs?events={many}")
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_logs_disabled_renders_stub(
    client_logs_disabled: AsyncClient,
) -> None:
    res = await client_logs_disabled.get("/logs")
    assert res.status_code == 200
    assert "désactivé" in res.text
    assert "DASHBOARD_LOGS_ENABLED=false" in res.text


@pytest.mark.asyncio
async def test_partials_logs_tail_returns_fragment(
    client_with_logs: tuple[AsyncClient, Path],
) -> None:
    client, _ = client_with_logs
    res = await client.get("/partials/logs-tail")
    assert res.status_code == 200
    # Fragment ≠ page complète → pas de <html> mais bien les details log.
    assert "trade_detected" in res.text


@pytest.mark.asyncio
async def test_partials_logs_tail_disabled_returns_empty_fragment(
    client_logs_disabled: AsyncClient,
) -> None:
    res = await client_logs_disabled.get("/partials/logs-tail")
    assert res.status_code == 200
    # Aucun event log réel ne doit apparaître (placeholder form n'est pas
    # rendu dans ce fragment).
    assert "0xinfo" not in res.text
    assert "Aucun log trouvé" in res.text


@pytest.mark.asyncio
async def test_logs_no_log_file_renders_empty_state(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        log_file=tmp_path / "missing.log",
        dashboard_logs_enabled=True,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        res = await client.get("/logs")
        assert res.status_code == 200
        assert "Aucun log trouvé" in res.text
