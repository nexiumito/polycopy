"""Tests M10 §3.4 + §8.3 — badge visuel mode dans les templates Telegram.

Le badge ``mode_badge`` injecté par ``AlertRenderer`` apparait en première
ligne de chaque template. Il est escapé MarkdownV2 via ``telegram_md_escape``
(le tiret ``-`` de ``DRY-RUN`` devient ``\\-``).
"""

from __future__ import annotations

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
