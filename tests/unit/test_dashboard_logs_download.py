"""Tests `/logs/download` — GET only, filename hardcodé, 404/403 corrects."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard.routes import build_app

_LOG_CONTENT = (
    '{"event":"polycopy_starting","level":"info","timestamp":"2026-04-18T10:00:00Z"}\n'
    '{"event":"order_filled","level":"info","timestamp":"2026-04-18T10:01:00Z"}\n'
)


@pytest_asyncio.fixture
async def client_with_log(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> AsyncIterator[AsyncClient]:
    log_file = tmp_path / "polycopy.log"
    log_file.write_text(_LOG_CONTENT)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        log_file=log_file,
        dashboard_logs_enabled=True,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_download_serves_file_contents(client_with_log: AsyncClient) -> None:
    res = await client_with_log.get("/logs/download")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert res.text == _LOG_CONTENT


@pytest.mark.asyncio
async def test_download_filename_is_hardcoded(client_with_log: AsyncClient) -> None:
    res = await client_with_log.get("/logs/download")
    cd = res.headers.get("content-disposition", "")
    assert "polycopy.log" in cd


@pytest.mark.asyncio
async def test_download_404_when_log_file_missing(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        log_file=tmp_path / "nope.log",
        dashboard_logs_enabled=True,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.get("/logs/download")
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_download_403_when_disabled(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        log_file=tmp_path / "polycopy.log",
        dashboard_logs_enabled=False,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.get("/logs/download")
        assert res.status_code == 403


@pytest.mark.asyncio
async def test_download_post_not_allowed(client_with_log: AsyncClient) -> None:
    """Toutes les routes M9 sont GET — POST sur /logs/download = 405."""
    res = await client_with_log.post("/logs/download")
    assert res.status_code == 405
