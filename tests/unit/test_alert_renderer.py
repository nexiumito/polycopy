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
    assert out.startswith("🟡")


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
        mode="dry-run",
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
    assert "127\\.0\\.0\\.1:8787" in out
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
