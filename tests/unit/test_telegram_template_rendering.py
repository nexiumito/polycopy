"""Test de non-régression : les 15 templates rendent tous sans raise (M7 §9.11)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import (
    Alert,
    DailySummaryContext,
    DigestContext,
    HeartbeatContext,
    ModuleStatus,
    ShutdownContext,
    StartupContext,
    TopWalletEntry,
)


@pytest.fixture
def renderer() -> AlertRenderer:
    return AlertRenderer()


_LEVEL_BY_EVENT: dict[str, str] = {
    "kill_switch_triggered": "CRITICAL",
    "executor_auth_fatal": "CRITICAL",
    "executor_error": "ERROR",
    "pnl_snapshot_drawdown": "WARNING",
    "order_filled_large": "INFO",
    "trader_promoted": "INFO",
    "trader_demoted": "WARNING",
    "discovery_cap_reached": "WARNING",
    "discovery_cycle_failed": "ERROR",
}


@pytest.mark.parametrize("event", list(_LEVEL_BY_EVENT.keys()))
def test_alert_template_renders_with_tricky_body(event: str, renderer: AlertRenderer) -> None:
    # body avec tous les caractères spéciaux MarkdownV2
    body = (
        f"Event {event} — wallet 0xabc_def (-3.2%). "
        "drawdown 30.00% >= seuil 20.00%. url http://127.0.0.1:8787"
    )
    out = renderer.render_alert(
        Alert(level=_LEVEL_BY_EVENT[event], event=event, body=body),  # type: ignore[arg-type]
    )
    assert out
    assert len(out) <= 4096
    # les caractères sensibles doivent être échappés dans le body rendu
    assert "\\." in out


def test_fallback_template_renders(renderer: AlertRenderer) -> None:
    out = renderer.render_alert(
        Alert(level="INFO", event="unknown_future_event_v2", body="(a) - b."),
    )
    # M10 : header badge en 1re ligne, emoji préservé en 2e ligne.
    assert out.startswith("_\\[")
    assert "🟢 *\\[unknown\\_future\\_event\\_v2\\]*" in out
    assert "unknown\\_future\\_event\\_v2" in out


def test_startup_template_minimal(renderer: AlertRenderer) -> None:
    ctx = StartupContext(
        version="0.0.0",
        mode="dry_run",
        boot_at=datetime(2026, 4, 18, 14, 30, tzinfo=UTC),
        pinned_wallets=[],
        modules=[ModuleStatus(name="Watcher", enabled=True, detail="0 wallets")],
        dashboard_url=None,
    )
    out = renderer.render_startup(ctx)
    assert out
    assert len(out) <= 4096


def test_shutdown_template(renderer: AlertRenderer) -> None:
    out = renderer.render_shutdown(
        ShutdownContext(duration_human="1 h 05 min", version="0.1.0"),
    )
    assert "arrêté" in out


def test_heartbeat_template(renderer: AlertRenderer) -> None:
    out = renderer.render_heartbeat(
        HeartbeatContext(
            uptime_human="12 h 03 min",
            heartbeat_index=5,
            watcher_count=3,
            positions_open=1,
            critical_alerts_in_window=0,
        ),
    )
    assert "#5" in out


def test_digest_template(renderer: AlertRenderer) -> None:
    out = renderer.render_digest(
        DigestContext(
            event_type="order_filled_large",
            count=7,
            window_minutes=60,
            level="INFO",
            sample_lines=["a"],
            truncated_count=6,
            dashboard_url=None,
        ),
    )
    assert "Digest" in out


def test_daily_summary_full(renderer: AlertRenderer) -> None:
    ctx = DailySummaryContext(
        date_human="2026-04-18",
        trades_24h=12,
        top_wallets=[TopWalletEntry(wallet_short="0xabc…def", label=None, trade_count=2)],
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
    assert "résumé" in out
    assert len(out) <= 4096


# --- Aucun secret ne doit apparaître dans les sources templates -------------


def test_no_secret_value_in_template_sources() -> None:
    """Grep statique : aucun template ne référence une *variable Jinja* liée à un secret.

    Les env var names cités en clair (ex: "vérifier POLYMARKET_PRIVATE_KEY") sont
    autorisés — ce sont des instructions user, pas des valeurs de token.
    La règle vise à empêcher ``{{ settings.telegram_bot_token }}`` ou équivalent.
    """
    import pathlib
    import re

    banned_var_patterns = [
        re.compile(r"\{\{[^}]*telegram_bot_token[^}]*\}\}", re.IGNORECASE),
        re.compile(r"\{\{[^}]*telegram_chat_id[^}]*\}\}", re.IGNORECASE),
        re.compile(r"\{\{[^}]*polymarket_private_key[^}]*\}\}", re.IGNORECASE),
        re.compile(r"\{\{[^}]*polymarket_funder[^}]*\}\}", re.IGNORECASE),
        re.compile(r"\{\{[^}]*api_secret[^}]*\}\}", re.IGNORECASE),
        re.compile(r"\{\{[^}]*api_passphrase[^}]*\}\}", re.IGNORECASE),
    ]
    root = pathlib.Path(__file__).parents[2]
    templates_dir = root / "src" / "polycopy" / "monitoring" / "templates"
    for path in templates_dir.rglob("*.md.j2"):
        content = path.read_text()
        for pattern in banned_var_patterns:
            assert not pattern.search(content), (
                f"{path} references a secret Jinja variable ({pattern.pattern})"
            )
