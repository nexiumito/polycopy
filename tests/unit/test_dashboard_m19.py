"""Tests M19 (Dashboard UX polish + consistency).

Couvre les 11 items MH.1 → MH.11. Cf. spec
[docs/specs/M19-dashboard-ux-polish.md](../../docs/specs/M19-dashboard-ux-polish.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard import jinja_filters as jf
from polycopy.dashboard.routes import build_app

_TEMPLATES_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "polycopy"
    / "dashboard"
    / "templates"
)


def _macros_env() -> Environment:
    """Environment Jinja minimal pour rendre une macro isolée."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=False,
        undefined=StrictUndefined,
    )
    env.filters.update(jf.all_filters())
    return env


@pytest_asyncio.fixture
async def m19_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        dry_run=True,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# MH.1 — render_address macro + clipboard copy button
# ---------------------------------------------------------------------------


class TestMh1RenderAddressMacro:
    def test_macro_truncates_and_keeps_full_value_in_title(self) -> None:
        env = _macros_env()
        tpl = env.from_string(
            "{% from 'macros.html' import render_address %}"
            "{{ render_address(addr, kind='wallet') }}",
        )
        out = tpl.render(addr="0xabcdef1234567890abcdef1234567890abcd0d71")
        assert "0xabcd…0d71" in out
        assert 'title="0xabcdef1234567890abcdef1234567890abcd0d71"' in out
        assert "copy-btn" in out
        assert "copyToClipboard" in out

    def test_macro_renders_dash_for_none(self) -> None:
        env = _macros_env()
        tpl = env.from_string(
            "{% from 'macros.html' import render_address %}{{ render_address(none) }}",
        )
        out = tpl.render(none=None)
        assert "—" in out
        assert "copy-btn" not in out

    def test_macro_uses_kind_class(self) -> None:
        env = _macros_env()
        tpl = env.from_string(
            "{% from 'macros.html' import render_address %}"
            "{{ render_address(v, kind='condition') }}",
        )
        out = tpl.render(v="0xcond1234")
        assert "condition-id" in out

    @pytest.mark.parametrize(
        "template_name",
        [
            "home.html",
            "traders_scoring.html",
            "partials/activity_rows.html",
            "partials/detections_rows.html",
            "partials/orders_rows.html",
            "partials/performance_rows.html",
            "partials/positions_rows.html",
            "partials/strategy_rows.html",
            "partials/traders_rows.html",
            "macros.html",
        ],
    )
    def test_render_address_used_on_every_wallet_view(self, template_name: str) -> None:
        path = _TEMPLATES_DIR / template_name
        body = path.read_text(encoding="utf-8")
        assert "render_address" in body, (
            f"{template_name} doit utiliser la macro render_address (MH.1)"
        )

    @pytest.mark.asyncio
    async def test_home_renders_copy_button(self, m19_client: AsyncClient) -> None:
        res = await m19_client.get("/home")
        assert res.status_code == 200
        # JS + style copy-btn présents (inline base.html)
        assert "copyToClipboard" in res.text
        assert "copy-btn" in res.text
