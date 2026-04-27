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


# ---------------------------------------------------------------------------
# MH.2 — format_size_precise 4-tier filter
# ---------------------------------------------------------------------------


class TestMh2FormatSizePrecise:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "—"),
            (0, "0"),
            (0.0, "0"),
            (0.00001, "1.00e-05"),
            (0.0005, "0.0005"),
            (0.0234, "0.023"),
            (0.5, "0.500"),
            (1.5, "1.50"),
            (123.456, "123.46"),
            (-0.0023, "-0.0023"),
            (-1.5, "-1.50"),
        ],
    )
    def test_4_tiers(self, value: float | None, expected: str) -> None:
        assert jf.format_size_precise(value) == expected

    def test_filter_registered(self) -> None:
        assert "format_size_precise" in jf.all_filters()

    def test_legacy_format_size_still_present(self) -> None:
        # MH.2 garde format_size pour rétrocompat.
        assert "format_size" in jf.all_filters()


# ---------------------------------------------------------------------------
# MH.3 — strategy_approve_rate 24h sliding window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mh3_strategy_approve_rate_uses_24h_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import timedelta

    from polycopy.storage.models import StrategyDecision

    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        # 4 récentes : 3 APPROVED + 1 REJECTED → 75% sur 24h
        for i in range(3):
            session.add(
                StrategyDecision(
                    detected_trade_id=i + 1,
                    tx_hash=f"0xrecent{i}",
                    decision="APPROVED",
                    decided_at=now - timedelta(hours=1),
                    pipeline_state={},
                ),
            )
        session.add(
            StrategyDecision(
                detected_trade_id=99,
                tx_hash="0xrecentR",
                decision="REJECTED",
                decided_at=now - timedelta(hours=2),
                pipeline_state={},
            ),
        )
        # 5 anciennes (REJECTED) hors fenêtre — ne doivent PAS biaiser le ratio.
        for i in range(5):
            session.add(
                StrategyDecision(
                    detected_trade_id=200 + i,
                    tx_hash=f"0xold{i}",
                    decision="REJECTED",
                    decided_at=now - timedelta(hours=30),
                    pipeline_state={},
                ),
            )
        await session.commit()

    stats = await queries.get_home_alltime_stats(session_factory)
    assert stats.strategy_approve_rate_pct == pytest.approx(75.0, abs=0.1)
    assert stats.approve_rate_window_hours == 24


@pytest.mark.asyncio
async def test_mh3_label_shows_window_hours(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        target_wallets=[],
        dry_run=True,
    )
    app = build_app(session_factory, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/home")
        assert res.status_code == 200
        assert "Approve stratégie (24h)" in res.text


# ---------------------------------------------------------------------------
# MH.4 — Tooltips explicatifs KPI cards /home
# ---------------------------------------------------------------------------


def test_mh4_kpi_card_supports_tooltip_field() -> None:
    from polycopy.dashboard.dtos import KpiCard

    assert "tooltip" in KpiCard.model_fields


def test_mh4_kpi_card_macro_renders_tooltip_attribute() -> None:
    from polycopy.dashboard.dtos import KpiCard

    env = _macros_env()
    tpl = env.from_string(
        "{% from 'macros.html' import kpi_card %}{{ kpi_card(card) }}",
    )
    card = KpiCard(
        title="Total USDC",
        value="$1,006.50",
        value_raw=1006.5,
        delta=None,
        delta_sign=None,
        sparkline_points=[],
        icon="dollar-sign",
        tooltip="Décomposition: total = initial + realized + latent.",
    )
    out = tpl.render(card=card)
    assert 'title="Décomposition: total = initial + realized + latent."' in out
    assert "info-icon" in out


def test_mh4_stat_card_macro_renders_tooltip_attribute() -> None:
    env = _macros_env()
    tpl = env.from_string(
        "{% from 'macros.html' import stat_card %}"
        "{{ stat_card('PnL', '+$1.50', 'dollar-sign', subtext='abc', tooltip='explanation here') }}",
    )
    out = tpl.render()
    assert 'title="explanation here"' in out
    assert "info-icon" in out


# ---------------------------------------------------------------------------
# MH.6 — Win rate exposes break-even count separately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mh6_win_rate_excludes_break_even_from_denominator(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import timedelta

    from polycopy.storage.models import MyPosition

    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        # 1W + 0L + 5BE → win_rate=100%, breakeven_count=5.
        for i, pnl in enumerate([1.0, 0.0, 0.0, 0.0, 0.0, 0.0]):
            session.add(
                MyPosition(
                    condition_id=f"0xc{i}",
                    asset_id=f"a{i}",
                    size=1.0,
                    avg_price=0.5,
                    opened_at=now - timedelta(hours=1),
                    closed_at=now,
                    simulated=True,
                    realized_pnl=pnl,
                ),
            )
        await session.commit()

    stats = await queries.get_home_alltime_stats(session_factory)
    assert stats.win_rate_pct == pytest.approx(100.0)
    assert stats.wins == 1
    assert stats.losses == 0
    assert stats.breakeven_count == 5


@pytest.mark.asyncio
async def test_mh6_home_label_shows_break_even_count(
    session_factory: async_sessionmaker[AsyncSession],
    m19_client: AsyncClient,
) -> None:
    from datetime import timedelta

    from polycopy.storage.models import MyPosition

    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        for i, pnl in enumerate([1.0, 0.0, 0.0]):
            session.add(
                MyPosition(
                    condition_id=f"0xclb{i}",
                    asset_id=f"alb{i}",
                    size=1.0,
                    avg_price=0.5,
                    opened_at=now - timedelta(hours=1),
                    closed_at=now,
                    simulated=True,
                    realized_pnl=pnl,
                ),
            )
        await session.commit()
    res = await m19_client.get("/home")
    assert res.status_code == 200
    # Subtext win rate = "1W / 0L / 2 break-even"
    assert "1W / 0L / 2 break-even" in res.text


@pytest.mark.asyncio
async def test_mh4_home_renders_six_tooltips(
    m19_client: AsyncClient,
) -> None:
    res = await m19_client.get("/home")
    assert res.status_code == 200
    body = res.text
    # Les 6 tooltips KPI cards listés spec §2.1 MH.4 doivent apparaître.
    assert "Gains/pertes cristallisés" in body  # PnL réalisé
    assert "Mark-to-market positions ouvertes" in body  # PnL latent
    assert "Capital engagé dans les positions" in body  # Exposition
    assert "side-aware" in body  # Gain max latent
    assert "Chute depuis le plus haut" in body  # Drawdown
    assert "Break-even (= 0) exclus" in body  # Win rate


# ---------------------------------------------------------------------------
# MH.9 — Spearman rangs locaux intersection v1∩v2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mh9_scoring_comparison_row_exposes_local_ranks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from polycopy.storage.models import TraderScore

    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        # 1 wallet dans v1∩v2 (intersection N=1) + 1 v1-only + 1 v2-only.
        session.add(
            TraderScore(
                target_trader_id=1,
                wallet_address="0xboth",
                score=0.5,
                scoring_version="v1",
                cycle_at=now,
                metrics_snapshot={},
            ),
        )
        session.add(
            TraderScore(
                target_trader_id=1,
                wallet_address="0xboth",
                score=0.6,
                scoring_version="v2.1",
                cycle_at=now,
                metrics_snapshot={},
            ),
        )
        session.add(
            TraderScore(
                target_trader_id=2,
                wallet_address="0xv1only",
                score=0.4,
                scoring_version="v1",
                cycle_at=now,
                metrics_snapshot={},
            ),
        )
        session.add(
            TraderScore(
                target_trader_id=3,
                wallet_address="0xv2only",
                score=0.3,
                scoring_version="v2.1",
                cycle_at=now,
                metrics_snapshot={},
            ),
        )
        await session.commit()

    rows = await queries.list_scoring_comparison(session_factory)
    by_wallet = {r.wallet_address: r for r in rows}
    # Seul 0xboth est dans l'intersection (N=1) → rang local = 1.
    both = by_wallet["0xboth"]
    assert both.rank_v1_local == 1
    assert both.rank_v2_local == 1
    # Pool reste pool : 0xboth est 1 sur 2 v1 (0.5 > 0.4).
    assert both.rank_v1_pool == 1
    assert both.rank_v2_pool == 1
    # Hors intersection → rangs locaux None.
    assert by_wallet["0xv1only"].rank_v1_local is None
    assert by_wallet["0xv1only"].rank_v2_local is None
    assert by_wallet["0xv2only"].rank_v1_local is None
    assert by_wallet["0xv2only"].rank_v2_local is None


@pytest.mark.asyncio
async def test_mh9_scoring_template_renders_local_ranks_and_tooltip(
    session_factory: async_sessionmaker[AsyncSession],
    m19_client: AsyncClient,
) -> None:
    from polycopy.storage.models import TargetTrader, TraderScore

    now = datetime.now(tz=UTC)
    async with session_factory() as session:
        for i in range(3):
            session.add(
                TargetTrader(
                    wallet_address=f"0xw{i}",
                    label=f"trader{i}",
                    status="active",
                ),
            )
            session.add(
                TraderScore(
                    target_trader_id=i + 1,
                    wallet_address=f"0xw{i}",
                    score=0.3 + 0.1 * i,
                    scoring_version="v1",
                    cycle_at=now,
                    metrics_snapshot={},
                ),
            )
            session.add(
                TraderScore(
                    target_trader_id=i + 1,
                    wallet_address=f"0xw{i}",
                    score=0.5 + 0.1 * i,
                    scoring_version="v2.1",
                    cycle_at=now,
                    metrics_snapshot={},
                ),
            )
        await session.commit()

    res = await m19_client.get("/traders/scoring")
    assert res.status_code == 200
    assert "intersection v1∩v2" in res.text  # tooltip text
