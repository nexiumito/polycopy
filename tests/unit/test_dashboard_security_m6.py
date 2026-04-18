"""Tests sécurité spécifiques M6 (renforcement de M4.5).

Vérifie :
- Toutes les nouvelles routes M6 sont GET-only.
- Aucun secret dans les templates source (grep automatisé).
- Aucun secret dans les responses HTML/JSON M6.
- Aucun stockage localStorage hors clé `polycopy.theme`.
- Le client JS ne référence pas d'endpoint externe inattendu.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard.routes import build_app

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src/polycopy/dashboard/templates"
_STATIC_DIR = Path(__file__).resolve().parents[2] / "src/polycopy/dashboard/static"

_SECRET_TOKEN = "1234567:ABCDEFsecret-telegram-token-value"  # noqa: S105
_SECRET_PK = "0xdeadbeefprivatekey0000000000000000000000000000000000000000000000"
_SECRET_FUNDER = "0xfeedfacefunderaddressaaaaaaaaaaaaaaaaaaa"

# Mots-clés sensibles qui ne doivent JAMAIS apparaître dans les templates source
# (M6 §0.6 — grep automatisé). On exclut le commentaire spec qui les liste,
# donc le grep cherche exactement la valeur du secret, pas le mot.
_SECRET_KEYWORDS = (
    "private_key",
    "polymarket_funder",
    "telegram_bot_token",
    "api_secret",
    "api_passphrase",
    "goldsky_api_key",
)


@pytest_asyncio.fixture
async def m6_client_with_secrets(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        polymarket_private_key=_SECRET_PK,
        polymarket_funder=_SECRET_FUNDER,
        telegram_bot_token=_SECRET_TOKEN,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


_M6_PATHS = (
    "/healthz",
    "/home",
    "/detections",
    "/strategy",
    "/orders",
    "/positions",
    "/pnl",
    "/traders",
    "/backtest",
    "/logs",
    "/partials/kpis",
    "/partials/discovery-summary",
    "/partials/detections-rows",
    "/partials/strategy-rows",
    "/partials/orders-rows",
    "/partials/positions-rows",
    "/partials/traders-rows",
    "/api/version",
)


@pytest.mark.asyncio
async def test_no_secret_leak_in_m6_responses(m6_client_with_secrets: AsyncClient) -> None:
    for path in _M6_PATHS:
        res = await m6_client_with_secrets.get(path)
        body = res.text
        for secret in (_SECRET_TOKEN, _SECRET_PK, _SECRET_FUNDER):
            assert secret not in body, f"secret leak on {path}"


@pytest.mark.asyncio
async def test_all_m6_routes_are_get_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, target_wallets=[])  # type: ignore[call-arg]
    app = build_app(session_factory, settings)
    for route in app.routes:
        if isinstance(route, APIRoute):
            non_safe = route.methods - {"GET", "HEAD", "OPTIONS"}
            assert not non_safe, f"write method exposed on {route.path}: {non_safe}"


@pytest.mark.asyncio
async def test_no_secret_keyword_in_template_sources() -> None:
    """Grep automatisé sur les templates : aucun nom de variable sensible."""
    for tpl in _TEMPLATES_DIR.rglob("*.html"):
        text = tpl.read_text(encoding="utf-8").lower()
        for keyword in _SECRET_KEYWORDS:
            assert keyword not in text, (
                f"sensitive keyword '{keyword}' found in {tpl.relative_to(_TEMPLATES_DIR)}"
            )


def test_dashboard_js_only_references_local_endpoints() -> None:
    """``dashboard.js`` ne doit pas faire d'appel HTTP externe."""
    js = (_STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    # Match http://... ou https://... — cherche des URLs absolues.
    urls = re.findall(r"https?://[^\s\"')]+", js)
    assert urls == [], f"dashboard.js contient des URLs absolues : {urls}"


def test_dashboard_js_localstorage_only_theme_key() -> None:
    """``localStorage.{getItem,setItem}`` n'est utilisé qu'avec la clé thème."""
    js = (_STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert "polycopy.theme" in js
    # On rejette toute autre clé suspecte préfixée polycopy.
    forbidden = (
        "polycopy.token",
        "polycopy.creds",
        "polycopy.session",
        "polycopy.private",
        "polycopy.api",
    )
    for k in forbidden:
        assert k not in js, f"clé localStorage interdite trouvée : {k}"


@pytest.mark.asyncio
async def test_logs_route_returns_stub(m6_client_with_secrets: AsyncClient) -> None:
    res = await m6_client_with_secrets.get("/logs")
    assert res.status_code == 200
    assert "M9" in res.text


@pytest.mark.asyncio
async def test_api_version_returns_json(m6_client_with_secrets: AsyncClient) -> None:
    res = await m6_client_with_secrets.get("/api/version")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    payload = res.json()
    assert "version" in payload
    assert payload["version"].startswith("0.6.0-")


@pytest.mark.asyncio
async def test_docs_still_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, target_wallets=[])  # type: ignore[call-arg]
    app = build_app(session_factory, settings)
    assert app.docs_url is None
    assert app.openapi_url is None
    assert app.redoc_url is None
