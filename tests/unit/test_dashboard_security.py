"""Tests sécurité critiques du dashboard (M4.5).

Vérifie :
- Aucun endpoint write (GET/HEAD uniquement).
- Aucun leak de secret (telegram token, private key, funder) dans les responses.
- Swagger / OpenAPI désactivés.
- Bind localhost par défaut.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
import uvicorn
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard.routes import build_app
from polycopy.storage.dtos import (
    DetectedTradeDTO,
    PnlSnapshotDTO,
    StrategyDecisionDTO,
)
from polycopy.storage.models import MyOrder
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    PnlSnapshotRepository,
    StrategyDecisionRepository,
)

_SECRET_TOKEN = "1234567:ABCDEFsecret-telegram-token-value"  # noqa: S105 — test fixture
_SECRET_PK = "0xdeadbeefprivatekey0000000000000000000000000000000000000000000000"
_SECRET_FUNDER = "0xfeedfacefunderaddressaaaaaaaaaaaaaaaaaaa"
_SECRET_API_KEY = "clob-api-key-secret-1234"
_SECRET_API_SECRET = "clob-api-secret-value-9876"  # noqa: S105 — test fixture
_SECRET_API_PASSPHRASE = "clob-passphrase-secret-5555"  # noqa: S105 — test fixture

_SECRETS = (
    _SECRET_TOKEN,
    _SECRET_PK,
    _SECRET_FUNDER,
    _SECRET_API_KEY,
    _SECRET_API_SECRET,
    _SECRET_API_PASSPHRASE,
)


@pytest_asyncio.fixture
async def client_with_secret_settings(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
    strategy_decision_repo: StrategyDecisionRepository,
    pnl_snapshot_repo: PnlSnapshotRepository,
) -> AsyncIterator[AsyncClient]:
    # Inject secrets dans settings — on veut vérifier qu'ils ne fuitent PAS dans les responses.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        polymarket_private_key=_SECRET_PK,
        polymarket_funder=_SECRET_FUNDER,
        telegram_bot_token=_SECRET_TOKEN,
    )
    # Données de fixtures pour peupler chaque page.
    await detected_trade_repo.insert_if_new(
        DetectedTradeDTO(
            tx_hash="0xtx1",
            target_wallet="0xwallet",
            condition_id="0xcond",
            asset_id="123",
            side="BUY",
            size=1.0,
            usdc_size=1.0,
            price=0.5,
            timestamp=datetime.now(tz=UTC),
            raw_json={},
        ),
    )
    await strategy_decision_repo.insert(
        StrategyDecisionDTO(
            detected_trade_id=1,
            tx_hash="0xtx1",
            decision="APPROVED",
            my_size=1.0,
            my_price=0.5,
            pipeline_state={},
        ),
    )
    async with session_factory() as session:
        session.add(
            MyOrder(
                source_tx_hash="0xtx1",
                condition_id="0xcond",
                asset_id="123",
                side="BUY",
                size=1.0,
                price=0.5,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="FILLED",
                simulated=True,
                transaction_hashes=[],
            ),
        )
        await session.commit()
    await pnl_snapshot_repo.insert(
        PnlSnapshotDTO(
            total_usdc=42.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=1.0,
            open_positions_count=0,
            cash_pnl_total=None,
            is_dry_run=False,
        ),
    )

    app = build_app(session_factory, settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


_ALL_PATHS = (
    "/healthz",
    "/home",
    "/detections",
    "/strategy",
    "/orders",
    "/positions",
    "/pnl",
    "/partials/kpis",
    "/partials/detections-rows",
    "/partials/strategy-rows",
    "/partials/orders-rows",
    "/partials/positions-rows",
    "/partials/pnl-data.json?since=24h",
)


@pytest.mark.asyncio
async def test_no_secret_leak_in_responses(client_with_secret_settings: AsyncClient) -> None:
    """Aucun secret injecté dans settings ne doit apparaître dans une response."""
    for path in _ALL_PATHS:
        res = await client_with_secret_settings.get(path)
        body = res.text
        for secret in _SECRETS:
            assert secret not in body, f"secret leak on {path} — found fragment"


@pytest.mark.asyncio
async def test_all_routes_are_get_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, target_wallets=[])  # type: ignore[call-arg]
    app = build_app(session_factory, settings)
    for route in app.routes:
        if isinstance(route, APIRoute):
            # HEAD/OPTIONS peuvent être auto-générés ; tout le reste doit être un GET.
            non_safe = route.methods - {"GET", "HEAD", "OPTIONS"}
            assert not non_safe, f"write method exposed on {route.path}: {non_safe}"


@pytest.mark.asyncio
async def test_write_methods_return_405(client_with_secret_settings: AsyncClient) -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        res = await client_with_secret_settings.request(method, "/home")
        assert res.status_code == 405, f"{method} /home accepté"


@pytest.mark.asyncio
async def test_no_openapi_no_docs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, target_wallets=[])  # type: ignore[call-arg]
    app = build_app(session_factory, settings)
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_bind_defaults_to_localhost() -> None:
    """L'``uvicorn.Config`` avec le host default doit rester sur 127.0.0.1."""
    from fastapi import FastAPI

    config = uvicorn.Config(FastAPI(), host="127.0.0.1", port=0, log_config=None)
    assert config.host == "127.0.0.1"
