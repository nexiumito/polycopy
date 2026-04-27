"""Tests M19 (Dashboard UX polish + consistency).

Couvre les 11 items MH.1 → MH.11. Cf. spec
[docs/specs/M19-dashboard-ux-polish.md](../../docs/specs/M19-dashboard-ux-polish.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard import jinja_filters as jf
from polycopy.dashboard import queries
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


# ---------------------------------------------------------------------------
# MH.7 — Unify format_usd, retire _format_card_usd
# ---------------------------------------------------------------------------


class TestMh7FormatUsdUnified:
    def test_format_card_usd_helper_removed(self) -> None:
        from polycopy.dashboard import queries as q

        assert not hasattr(q, "_format_card_usd"), (
            "_format_card_usd doit être retiré (MH.7)"
        )

    def test_kpi_card_value_raw_field_present(self) -> None:
        from polycopy.dashboard.dtos import KpiCard

        fields = KpiCard.model_fields
        assert "value_raw" in fields, "KpiCard.value_raw requis (MH.7)"

    def test_format_usd_under_1k_uses_2_decimals(self) -> None:
        # Régression : sous $1k on garde 2 décimales (cohérence /home ↔ /performance).
        assert jf.format_usd(0.45) == "$0.45"
        assert jf.format_usd(999.99) == "$999.99"

    @pytest.mark.asyncio
    async def test_get_home_kpi_cards_total_usdc_uses_format_usd(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from polycopy.storage.models import PnlSnapshot

        async with session_factory() as session:
            session.add(
                PnlSnapshot(
                    timestamp=datetime.now(tz=UTC),
                    total_usdc=1006.50,
                    realized_pnl=-0.54,
                    unrealized_pnl=7.04,
                    drawdown_pct=0.0,
                    open_positions_count=0,
                    is_dry_run=False,
                    execution_mode="live",
                ),
            )
            await session.commit()

        cards = await queries.get_home_kpi_cards(session_factory)
        total_card = next(c for c in cards if c.title == "Total USDC")
        # MH.7 : value_raw numérique exposé.
        assert total_card.value_raw == pytest.approx(1006.50)
        # MH.7 : value pré-formaté via format_usd (≥ 1k → "$1.0k") — pas l'ancien
        # entier "$1006" produit par _format_card_usd.
        assert total_card.value == "$1.0k"
        assert "1006" not in total_card.value
