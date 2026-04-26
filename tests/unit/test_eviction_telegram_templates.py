"""Tests de rendu + grep-secret pour les 6 templates Telegram M5_bis Phase D.

Vérifie :
- Les 6 templates existent physiquement dans
  ``src/polycopy/monitoring/templates/``.
- Chaque template référence ``{{ mode_badge }}``, ``{{ machine_emoji }}``,
  ``{{ machine_id }}``, ``{{ body }}`` (invariants M10 + M12_bis).
- Le rendu d'un Alert fake produit du MarkdownV2 valide (pas de variable
  unescaped, pas de secret injecté).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import Alert

_TEMPLATES_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "polycopy" / "monitoring" / "templates"
)

_M5_BIS_TEMPLATES = [
    "trader_eviction_started.md.j2",
    "trader_eviction_aborted.md.j2",
    "trader_eviction_completed_to_shadow.md.j2",
    "trader_eviction_completed_to_active_via_rebound.md.j2",
    "trader_blacklisted.md.j2",
    "trader_blacklist_removed.md.j2",
]


@pytest.mark.parametrize("template_name", _M5_BIS_TEMPLATES)
def test_template_file_exists(template_name: str) -> None:
    """Chaque template M5_bis est présent sur disque."""
    target = _TEMPLATES_DIR / template_name
    assert target.is_file(), f"template manquant : {template_name}"
    assert target.stat().st_size > 0


@pytest.mark.parametrize("template_name", _M5_BIS_TEMPLATES)
def test_template_references_mode_badge_and_machine_id(template_name: str) -> None:
    """Invariant M10 + M12_bis : chaque template inclut mode_badge + machine_id."""
    content = (_TEMPLATES_DIR / template_name).read_text(encoding="utf-8")
    assert "mode_badge" in content
    assert "machine_id" in content
    assert "machine_emoji" in content
    assert "body" in content
    assert "dashboard_url" in content  # M12_bis Phase G lien cliquable


@pytest.mark.parametrize("template_name", _M5_BIS_TEMPLATES)
def test_template_no_raw_private_key_leak(template_name: str) -> None:
    """Aucun secret hardcodé dans les sources templates (grep défensif)."""
    content = (_TEMPLATES_DIR / template_name).read_text(encoding="utf-8")
    # Les secrets qu'on veut absolument pas voir apparaître.
    forbidden = [
        "polymarket_private_key",
        "telegram_bot_token",
        "POLYMARKET_PRIVATE_KEY",
        "TELEGRAM_BOT_TOKEN",
        "0x" + "0" * 62,  # forme typique de clé privée raw (64 hex)
        "api_secret",
        "api_passphrase",
    ]
    for token in forbidden:
        assert token not in content, f"{template_name}: secret {token!r} leaked"


def test_alert_renderer_renders_eviction_started() -> None:
    """Smoke test : renderer produit du MarkdownV2 valide pour l'event."""
    renderer = AlertRenderer(
        project_root=Path(__file__).resolve().parents[2],
        mode="dry_run",
        machine_id="TESTPC",
        machine_emoji="🖥️",
    )
    alert = Alert(
        level="INFO",
        event="trader_eviction_started",
        body=(
            "Candidat : 0x1234…abcd (score 0.91, shadow)\nÉvincé : 0x5678…cdef (active → sell_only)"
        ),
        cooldown_key="trader_eviction_started",
    )
    rendered = renderer.render_alert(alert)
    # Assertions structure attendue.
    assert "🟣" in rendered
    assert "*TESTPC*" in rendered  # machine_id échappé + bold markdown
    assert "trader" in rendered  # event name présent (escape ne supprime pas 'trader')
    assert "Candidat" in rendered
    assert "shadow" in rendered


def test_alert_renderer_escapes_machine_id_special_chars() -> None:
    """MACHINE_ID='PC-1_A' doit être rendu 'PC\\-1\\_A' en MarkdownV2."""
    renderer = AlertRenderer(
        project_root=Path(__file__).resolve().parents[2],
        mode="dry_run",
        machine_id="PC-1_A",
        machine_emoji="💻",
    )
    alert = Alert(
        level="INFO",
        event="trader_blacklisted",
        body="0xbad exclu.",
        cooldown_key="trader_blacklisted",
    )
    rendered = renderer.render_alert(alert)
    # MarkdownV2 échappe - et _
    assert r"PC\-1\_A" in rendered


# --- M15 MB.8 : trader_auto_blacklisted template (1 test §9.8) -------------


def test_template_auto_blacklisted_renders_safely() -> None:
    """MB.8 §9.8 #28 — render trader_auto_blacklisted, assert MarkdownV2
    propre + aucun secret leaké."""
    template = _TEMPLATES_DIR / "trader_auto_blacklisted.md.j2"
    assert template.is_file()
    content = template.read_text(encoding="utf-8")
    # Référence aux variables M10 + M12_bis.
    assert "mode_badge" in content
    assert "machine_id" in content
    assert "machine_emoji" in content
    assert "body" in content
    assert "dashboard_url" in content
    # Pas de secret marker hardcodé.
    forbidden = [
        "polymarket_private_key",
        "telegram_bot_token",
        "POLYMARKET_PRIVATE_KEY",
        "TELEGRAM_BOT_TOKEN",
        "api_secret",
        "api_passphrase",
        "0x" + "0" * 62,
    ]
    for token in forbidden:
        assert token not in content

    # Render avec un Alert fake — pas de crash, sortie MarkdownV2 cohérente.
    renderer = AlertRenderer(
        project_root=Path(__file__).resolve().parents[2],
        mode="dry_run",
        machine_id="TESTPC",
        machine_emoji="🖥️",
    )
    alert = Alert(
        level="WARNING",
        event="trader_auto_blacklisted",
        body=(
            "Wallet : `0x21ffd2b7…0d71`. Raison : pnl_threshold "
            "(PnL observé -0.55 USD sur 30j, win-rate 19.0% sur 52 "
            "positions décidées). Status : active → blacklisted."
        ),
        cooldown_key="auto_blacklist_0x21ffd2b7d6a40d71",
    )
    rendered = renderer.render_alert(alert)
    assert "🚫" in rendered
    assert "*TESTPC*" in rendered
    assert "trader" in rendered  # 'trader\\_auto\\_blacklisted' tag
    assert "active" in rendered
    assert "blacklisted" in rendered
    # Pas de secret leak dans le rendu final.
    for token in (
        "POLYMARKET_PRIVATE_KEY",
        "TELEGRAM_BOT_TOKEN",
        "polymarket_private_key",
        "api_secret",
    ):
        assert token not in rendered
