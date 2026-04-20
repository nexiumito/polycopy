"""Tests M10 §3.4 + §8.3 — badge visuel mode dans les templates Telegram.

Le badge ``mode_badge`` injecté par ``AlertRenderer`` apparait en première
ligne de chaque template. Il est escapé MarkdownV2 via ``telegram_md_escape``
(le tiret ``-`` de ``DRY-RUN`` devient ``\\-``).

M12_bis §3.3 : en plus du ``mode_badge``, une 2e ligne
``{{ machine_emoji }} *{{ machine_id | telegram_md_escape }}*`` identifie la
machine source en setup multi-machine.
"""

from __future__ import annotations

import pytest

from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import Alert


def _alert() -> Alert:
    return Alert(
        level="CRITICAL",
        event="kill_switch_triggered",
        body="drawdown 30% >= seuil 20%.",
        cooldown_key="kill_switch",
    )


def test_telegram_alert_shows_mode_badge_simulation() -> None:
    out = AlertRenderer(mode="simulation").render_alert(_alert())
    assert "🟢 SIMULATION" in out


def test_telegram_alert_shows_mode_badge_dry_run() -> None:
    out = AlertRenderer(mode="dry_run").render_alert(_alert())
    # `-` escapé en `\-` ; le `*` emoji reste.
    assert "🟢 DRY\\-RUN" in out


def test_telegram_alert_shows_mode_badge_live() -> None:
    out = AlertRenderer(mode="live").render_alert(_alert())
    assert "🔴 LIVE" in out


def test_telegram_badge_escaped_in_markdown_v2() -> None:
    """Le tiret ``-`` dans ``DRY-RUN`` doit être escapé pour MarkdownV2."""
    out = AlertRenderer(mode="dry_run").render_alert(_alert())
    # Trouve la ligne du badge : "_\[🟢 DRY\-RUN\]_" -> `\-` présent, `-` brut absent.
    header = out.splitlines()[0]
    assert header.startswith("_\\[")
    assert "DRY\\-RUN" in header
    assert "DRY-RUN" not in header.replace("DRY\\-RUN", "")


# --- M12_bis : machine_id / machine_emoji dans la 2e ligne ------------------

_ALL_EVENTS: list[str] = [
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


@pytest.mark.parametrize("event", _ALL_EVENTS)
def test_machine_badge_present_on_second_line(event: str) -> None:
    """Pour chaque template connu, la 2e ligne = ``<emoji> *<MACHINE_ID>*``."""
    renderer = AlertRenderer(mode="live", machine_id="PC-FIXE", machine_emoji="🖥️")
    out = renderer.render_alert(
        Alert(
            level=_LEVEL_BY_EVENT[event],  # type: ignore[arg-type]
            event=event,
            body="sample body.",
        ),
    )
    lines = out.splitlines()
    assert lines[0].startswith("_\\[")  # M10 header inchangé
    assert lines[1] == "🖥️ *PC\\-FIXE*"  # M12_bis : emoji + id escaped


def test_machine_badge_present_on_fallback_template() -> None:
    """Event inconnu → fallback.md.j2 doit aussi porter le badge machine."""
    renderer = AlertRenderer(mode="live", machine_id="MACBOOK", machine_emoji="💻")
    out = renderer.render_alert(
        Alert(level="INFO", event="future_event_v3", body="hello."),
    )
    lines = out.splitlines()
    assert lines[1] == "💻 *MACBOOK*"


def test_machine_badge_escapes_underscore_and_dash() -> None:
    """``MACHINE_ID`` user-controlled doit être escapé (underscore + tiret)."""
    renderer = AlertRenderer(
        mode="dry_run",
        machine_id="UNI_DEBIAN-01",
        machine_emoji="🏫",
    )
    out = renderer.render_alert(_alert())
    lines = out.splitlines()
    assert lines[1] == "🏫 *UNI\\_DEBIAN\\-01*"


def test_machine_emoji_is_not_escaped() -> None:
    """L'emoji est hors charset MarkdownV2 → pas d'escape attendu."""
    renderer = AlertRenderer(mode="live", machine_id="PC-FIXE", machine_emoji="🖥️")
    out = renderer.render_alert(_alert())
    # L'emoji brut est préservé.
    assert "🖥️" in out


def test_machine_badge_appears_in_startup() -> None:
    """``StartupContext`` hérite aussi du badge (via ``_startup_vars``)."""
    from datetime import UTC, datetime

    from polycopy.monitoring.dtos import StartupContext

    ctx = StartupContext(
        version="1.0.0",
        mode="live",
        boot_at=datetime(2026, 4, 20, tzinfo=UTC),
        pinned_wallets=[],
        modules=[],
    )
    renderer = AlertRenderer(mode="live", machine_id="UNI-DEBIAN", machine_emoji="🏫")
    out = renderer.render_startup(ctx)
    lines = out.splitlines()
    assert lines[1] == "🏫 *UNI\\-DEBIAN*"


def test_machine_badge_appears_in_heartbeat() -> None:
    from polycopy.monitoring.dtos import HeartbeatContext

    ctx = HeartbeatContext(
        uptime_human="1h",
        heartbeat_index=1,
        watcher_count=2,
        positions_open=0,
        critical_alerts_in_window=0,
    )
    renderer = AlertRenderer(mode="live", machine_id="PC-FIXE", machine_emoji="🖥️")
    out = renderer.render_heartbeat(ctx)
    lines = out.splitlines()
    assert lines[1] == "🖥️ *PC\\-FIXE*"
