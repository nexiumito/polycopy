"""Tests du ``AlertRenderer`` (rendu Jinja2 + cascade user-land)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from jinja2 import UndefinedError

from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import (
    Alert,
    DailySummaryContext,
    DigestContext,
    HeartbeatContext,
    ModuleStatus,
    PinnedWallet,
    ShutdownContext,
    StartupContext,
    TopWalletEntry,
)


def _renderer() -> AlertRenderer:
    return AlertRenderer()


# --- Fallback template ------------------------------------------------------


def test_render_unknown_event_uses_fallback() -> None:
    renderer = _renderer()
    out = renderer.render_alert(
        Alert(level="WARNING", event="future_event", body="ça bouge (vite)."),
    )
    assert "future\\_event" in out
    assert "ça bouge \\(vite\\)\\." in out
    # M10 : le header badge précède le emoji historique (conservation de la
    # shape M4 : emoji + *[event]* en 2e ligne).
    assert out.startswith("_\\[")
    assert "🟡 *\\[future\\_event\\]*" in out


def test_render_known_event_uses_dedicated_template() -> None:
    renderer = _renderer()
    out = renderer.render_alert(
        Alert(
            level="CRITICAL",
            event="kill_switch_triggered",
            body="Kill switch — drawdown 30.00%.",
        ),
    )
    assert "kill\\_switch\\_triggered" in out
    assert "Action requise" in out
    assert "drawdown 30\\.00%" in out


# --- All known templates render fixture -------------------------------------


_KNOWN_EVENTS = [
    "kill_switch_triggered",
    "executor_auth_fatal",
    "executor_error",
    "pnl_snapshot_drawdown",
    "order_filled_large",
    "trader_promoted",
    "trader_demoted",
    "discovery_cap_reached",
    "discovery_cycle_failed",
]


@pytest.mark.parametrize("event", _KNOWN_EVENTS)
def test_each_known_template_renders(event: str) -> None:
    renderer = _renderer()
    out = renderer.render_alert(
        Alert(level="INFO", event=event, body="Body content with (parens) and -3.2%."),
    )
    assert out
    assert len(out) <= 4096


# --- Template cascade user-land override ------------------------------------


def test_user_override_wins_over_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Create a user override
    user_dir = tmp_path / "assets" / "telegram"
    user_dir.mkdir(parents=True)
    (user_dir / "kill_switch_triggered.md.j2").write_text(
        "OVERRIDE {{ body | telegram_md_escape }}\n",
    )

    renderer = AlertRenderer(project_root=tmp_path)
    out = renderer.render_alert(
        Alert(level="CRITICAL", event="kill_switch_triggered", body="test"),
    )
    assert out.startswith("OVERRIDE")


# --- StrictUndefined --------------------------------------------------------


def test_strict_undefined_raises_on_missing_variable(tmp_path: Path) -> None:
    user_dir = tmp_path / "assets" / "telegram"
    user_dir.mkdir(parents=True)
    (user_dir / "order_filled_large.md.j2").write_text("Missing: {{ this_does_not_exist }}\n")
    renderer = AlertRenderer(project_root=tmp_path)
    with pytest.raises(UndefinedError):
        renderer.render_alert(
            Alert(level="INFO", event="order_filled_large", body="x"),
        )


# --- Structured message renders ---------------------------------------------


def test_render_startup_full() -> None:
    renderer = _renderer()
    ctx = StartupContext(
        version="0.1.0 (abc12345)",
        mode="dry_run",
        boot_at=datetime(2026, 4, 18, 14, 30, tzinfo=UTC),
        pinned_wallets=[
            PinnedWallet(wallet_short="0xabcd…cdef", label="Smart Money"),
            PinnedWallet(wallet_short="0x1111…2222", label=None),
        ],
        modules=[
            ModuleStatus(name="Watcher", enabled=True, detail="3 wallets"),
            ModuleStatus(name="Discovery", enabled=False, detail="désactivé"),
        ],
        dashboard_url="http://127.0.0.1:8787/",
    )
    out = renderer.render_startup(ctx)
    assert "polycopy démarré" in out
    assert "Smart Money" in out
    # M12_bis Phase G : format du lien dashboard devient ``[📊 Dashboard](url)``
    # — l'URL entre parenthèses n'est plus échappée (syntaxe link MarkdownV2).
    assert "[📊 Dashboard](http://127.0.0.1:8787/)" in out
    assert "⏸️ Discovery" in out


def test_render_startup_no_dashboard_no_pinned() -> None:
    renderer = _renderer()
    ctx = StartupContext(
        version="0.0.0",
        mode="live",
        boot_at=datetime(2026, 4, 18, 14, 30, tzinfo=UTC),
        pinned_wallets=[],
        modules=[ModuleStatus(name="Watcher", enabled=True, detail="0 wallets")],
        dashboard_url=None,
    )
    out = renderer.render_startup(ctx)
    assert "Aucun wallet pinned" in out
    assert "Dashboard" not in out


def test_render_shutdown() -> None:
    renderer = _renderer()
    ctx = ShutdownContext(duration_human="2 h 30 min", version="0.1.0")
    out = renderer.render_shutdown(ctx)
    assert "polycopy arrêté" in out
    assert "2 h 30 min" in out


def test_render_heartbeat() -> None:
    renderer = _renderer()
    ctx = HeartbeatContext(
        uptime_human="12 h 03 min",
        heartbeat_index=5,
        watcher_count=3,
        positions_open=1,
        critical_alerts_in_window=0,
    )
    out = renderer.render_heartbeat(ctx)
    assert "polycopy actif" in out
    assert "#5" in out


def test_render_digest() -> None:
    renderer = _renderer()
    ctx = DigestContext(
        event_type="order_filled_large",
        count=7,
        window_minutes=60,
        level="INFO",
        sample_lines=["wallet A — $120", "wallet B — $85", "wallet C — $95", "wallet D — $110"],
        truncated_count=3,
        dashboard_url="http://127.0.0.1:8787/",
    )
    out = renderer.render_digest(ctx)
    assert "7 alertes" in out
    assert "order\\_filled\\_large" in out
    assert "et 3 autres" in out


def test_render_daily_summary_minimal_no_discovery() -> None:
    renderer = _renderer()
    ctx = DailySummaryContext(
        date_human="2026-04-18",
        trades_24h=0,
        top_wallets=[],
        decisions_approved=0,
        decisions_rejected=0,
        top_reject_reason=None,
        orders_sent=0,
        orders_filled=0,
        orders_rejected=0,
        volume_executed_usd=0.0,
        total_usdc=None,
        delta_24h_pct=None,
        drawdown_24h_pct=None,
        positions_open=0,
        positions_value_usd=0.0,
        discovery_enabled=False,
        discovery_cycles_24h=0,
        discovery_promotions_24h=0,
        discovery_demotions_24h=0,
        discovery_cap_reached_24h=0,
        alerts_total_24h=0,
        alerts_by_type_compact="",
        dashboard_url=None,
    )
    out = renderer.render_daily_summary(ctx)
    assert "polycopy — résumé" in out
    assert "Discovery" not in out  # section masquée
    assert len(out) <= 4096


def test_render_daily_summary_with_all_sections() -> None:
    renderer = _renderer()
    ctx = DailySummaryContext(
        date_human="2026-04-18",
        trades_24h=12,
        top_wallets=[TopWalletEntry(wallet_short="0xabc…def", label="SM", trade_count=5)],
        decisions_approved=8,
        decisions_rejected=4,
        top_reject_reason="slippage",
        orders_sent=8,
        orders_filled=7,
        orders_rejected=1,
        volume_executed_usd=1234.56,
        total_usdc=1500.0,
        delta_24h_pct=2.3,
        drawdown_24h_pct=1.2,
        positions_open=3,
        positions_value_usd=450.0,
        discovery_enabled=True,
        discovery_cycles_24h=4,
        discovery_promotions_24h=1,
        discovery_demotions_24h=0,
        discovery_cap_reached_24h=0,
        alerts_total_24h=5,
        alerts_by_type_compact="filled:3",
        dashboard_url="http://127.0.0.1:8787/",
    )
    out = renderer.render_daily_summary(ctx)
    assert "Discovery" in out
    assert "slippage" in out
    assert "filled:3" in out


# --- Troncature 4096 chars --------------------------------------------------


def test_renderer_truncates_oversize_message(tmp_path: Path) -> None:
    user_dir = tmp_path / "assets" / "telegram"
    user_dir.mkdir(parents=True)
    (user_dir / "order_filled_large.md.j2").write_text("X" * 5000)
    renderer = AlertRenderer(project_root=tmp_path)
    out = renderer.render_alert(Alert(level="INFO", event="order_filled_large", body="x"))
    assert len(out) == 4096
    assert out.endswith("…")


# --- M12_bis : injection machine_id / machine_emoji -------------------------


def test_renderer_stores_machine_bindings() -> None:
    renderer = AlertRenderer(mode="live", machine_id="PC-FIXE", machine_emoji="🖥️")
    assert renderer._machine_id == "PC-FIXE"
    assert renderer._machine_emoji == "🖥️"


def test_renderer_defaults_machine_bindings() -> None:
    """Sans kwargs explicites : fallback ``"UNKNOWN"`` + ``"🖥️"``."""
    renderer = AlertRenderer()
    assert renderer._machine_id == "UNKNOWN"
    assert renderer._machine_emoji == "🖥️"


def test_inject_mode_adds_machine_context_and_preserves_mode_badge() -> None:
    """Non-régression M10 : ``mode_badge`` toujours présent + M12_bis machine."""
    renderer = AlertRenderer(mode="dry_run", machine_id="MACBOOK", machine_emoji="💻")
    ctx = renderer._inject_mode({"event_type": "test"})
    assert ctx["machine_id"] == "MACBOOK"
    assert ctx["machine_emoji"] == "💻"
    assert ctx["mode_badge"] == "🟢 DRY-RUN"
    assert ctx["mode"] == "dry_run"


def test_inject_mode_does_not_override_caller_machine() -> None:
    """``setdefault`` : si le caller a déjà ``machine_id``, on ne l'écrase pas."""
    renderer = AlertRenderer(machine_id="PC-FIXE", machine_emoji="🖥️")
    ctx = renderer._inject_mode({"machine_id": "OVERRIDE"})
    assert ctx["machine_id"] == "OVERRIDE"
    assert ctx["machine_emoji"] == "🖥️"


def test_startup_vars_adds_machine_context() -> None:
    ctx_obj = StartupContext(
        version="1.0.0",
        mode="live",
        boot_at=datetime(2026, 4, 20, tzinfo=UTC),
        pinned_wallets=[],
        modules=[],
    )
    renderer = AlertRenderer(mode="live", machine_id="UNI-DEBIAN", machine_emoji="🏫")
    data = renderer._startup_vars(ctx_obj)
    assert data["machine_id"] == "UNI-DEBIAN"
    assert data["machine_emoji"] == "🏫"
    assert data["mode_badge"] == "🔴 LIVE"


# --- M12_bis Phase G : dashboard_url injection via AlertRenderer ------------


def test_renderer_stores_dashboard_url() -> None:
    renderer = AlertRenderer(dashboard_url="http://pc-fixe.taila157fd.ts.net:8787/")
    assert renderer._dashboard_url == "http://pc-fixe.taila157fd.ts.net:8787/"


def test_renderer_dashboard_url_default_none() -> None:
    renderer = AlertRenderer()
    assert renderer._dashboard_url is None


def test_inject_mode_injects_dashboard_url() -> None:
    renderer = AlertRenderer(
        machine_id="PC-FIXE",
        dashboard_url="http://pc-fixe.taila157fd.ts.net:8787/",
    )
    ctx = renderer._inject_mode({"event_type": "test"})
    assert ctx["dashboard_url"] == "http://pc-fixe.taila157fd.ts.net:8787/"


def test_inject_mode_does_not_override_existing_dashboard_url() -> None:
    """``setdefault`` : si un caller a déjà setté ``dashboard_url``, on respecte."""
    renderer = AlertRenderer(dashboard_url="http://renderer.default/")
    ctx = renderer._inject_mode({"dashboard_url": "http://caller.override/"})
    assert ctx["dashboard_url"] == "http://caller.override/"


def test_startup_vars_fills_dashboard_url_when_dto_has_none() -> None:
    """Le DTO ``StartupContext.dashboard_url=None`` → fallback sur renderer."""
    ctx_obj = StartupContext(
        version="1.0.0",
        mode="dry_run",
        boot_at=datetime(2026, 4, 20, tzinfo=UTC),
        pinned_wallets=[],
        modules=[],
        dashboard_url=None,
    )
    renderer = AlertRenderer(dashboard_url="http://fallback.ts.net:8787/")
    data = renderer._startup_vars(ctx_obj)
    assert data["dashboard_url"] == "http://fallback.ts.net:8787/"


def test_startup_vars_preserves_dto_dashboard_url() -> None:
    """Le DTO ``StartupContext.dashboard_url="http://..."`` gagne sur le renderer."""
    ctx_obj = StartupContext(
        version="1.0.0",
        mode="dry_run",
        boot_at=datetime(2026, 4, 20, tzinfo=UTC),
        pinned_wallets=[],
        modules=[],
        dashboard_url="http://dto.value/",
    )
    renderer = AlertRenderer(dashboard_url="http://renderer.value/")
    data = renderer._startup_vars(ctx_obj)
    assert data["dashboard_url"] == "http://dto.value/"


def test_alert_template_rendered_contains_dashboard_link() -> None:
    """Render d'une alerte event-based : le footer ``[📊 Dashboard](url)`` apparaît."""
    renderer = AlertRenderer(
        mode="live",
        machine_id="PC-FIXE",
        dashboard_url="http://pc-fixe.taila157fd.ts.net:8787/",
    )
    out = renderer.render_alert(
        Alert(level="CRITICAL", event="kill_switch_triggered", body="Drawdown 30%."),
    )
    assert "[📊 Dashboard](http://pc-fixe.taila157fd.ts.net:8787/)" in out


def test_alert_template_without_dashboard_url_hides_link() -> None:
    """Render sans ``dashboard_url`` : aucun footer ajouté."""
    renderer = AlertRenderer(mode="live", machine_id="PC-FIXE", dashboard_url=None)
    out = renderer.render_alert(
        Alert(level="INFO", event="order_filled_large", body="x."),
    )
    assert "Dashboard" not in out
    assert "📊" not in out


def test_digest_template_via_renderer_url_fallback() -> None:
    """Digest : ``DigestContext.dashboard_url=None`` + renderer URL → link présent."""
    renderer = AlertRenderer(dashboard_url="http://fallback.ts.net:8787/")
    ctx = DigestContext(
        event_type="order_filled_large",
        count=3,
        window_minutes=10,
        level="INFO",
        sample_lines=["a"],
        truncated_count=0,
        dashboard_url=None,
    )
    out = renderer.render_digest(ctx)
    assert "[📊 Dashboard](http://fallback.ts.net:8787/)" in out
