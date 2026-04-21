"""Validation des liens internes du README + assets référencés (M9)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_README = _ROOT / "README.md"

# Pattern markdown : [text](path) — ignore les liens HTTPS.
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
# Pattern HTML <img src="..." />
_IMG_RE = re.compile(r'<img\s+src="([^"]+)"', re.IGNORECASE)


def _extract_links(text: str) -> set[str]:
    """Retourne tous les liens internes (relatifs) du README."""
    md = {m.group(2) for m in _LINK_RE.finditer(text)}
    img = {m.group(1) for m in _IMG_RE.finditer(text)}
    all_links = md | img
    # Filtre : ignore http(s) et anchors purs (#section).
    return {ln for ln in all_links if not ln.startswith(("http://", "https://", "#", "mailto:"))}


def test_readme_exists() -> None:
    assert _README.is_file()


def test_readme_internal_links_resolve() -> None:
    text = _README.read_text(encoding="utf-8")
    links = _extract_links(text)
    missing: list[str] = []
    for link in links:
        # Strip anchor (#section) et query (?foo).
        path_part = link.split("#", 1)[0].split("?", 1)[0]
        if not path_part:
            continue
        target = (_ROOT / path_part).resolve()
        # On ne valide que les liens qui restent dans le repo.
        try:
            target.relative_to(_ROOT)
        except ValueError:
            continue
        if not target.exists():
            missing.append(f"{link} → {target}")
    assert not missing, f"Liens README cassés : {missing}"


@pytest.mark.parametrize(
    "asset",
    [
        "assets/screenshots/logo.svg",
        "assets/screenshots/dashboard-home.png",
        "assets/screenshots/dashboard-traders.png",
        "assets/screenshots/dashboard-pnl.png",
        "assets/screenshots/terminal-silent-cli.png",
    ],
)
def test_required_asset_exists(asset: str) -> None:
    """Acceptance criteria M9 : assets présents dans ``assets/screenshots/``."""
    target = _ROOT / asset
    assert target.is_file(), f"asset manquant : {asset}"
    assert target.stat().st_size > 0


def test_readme_contains_test_phase_warning() -> None:
    """Le bandeau 'phase de test' doit être présent en haut du README."""
    text = _README.read_text(encoding="utf-8")
    head = text[:2000]
    assert "phase de test" in head.lower()
    assert "fortement déconseillé" in head.lower() or "déconseillé" in head.lower()


def test_readme_has_quickstart_section() -> None:
    text = _README.read_text(encoding="utf-8")
    assert "## Quickstart" in text


def test_readme_has_faq_section() -> None:
    text = _README.read_text(encoding="utf-8")
    assert "## FAQ" in text


def test_readme_has_comparison_table() -> None:
    text = _README.read_text(encoding="utf-8")
    assert "## Comparaison" in text or "Comparaison avec" in text


def test_readme_has_hall_of_fame() -> None:
    text = _README.read_text(encoding="utf-8")
    assert "Hall of Fame" in text


def test_readme_mentions_juriste_for_legal_question() -> None:
    text = _README.read_text(encoding="utf-8")
    assert "juriste" in text.lower()


def test_readme_lists_m9_env_vars() -> None:
    """Les 6 nouvelles env vars M9 doivent être documentées."""
    text = _README.read_text(encoding="utf-8")
    for var in (
        "CLI_SILENT",
        "LOG_FILE",
        "LOG_FILE_MAX_BYTES",
        "LOG_FILE_BACKUP_COUNT",
        "DASHBOARD_LOGS_ENABLED",
        "DASHBOARD_LOGS_TAIL_LINES",
    ):
        assert var in text, f"Env var M9 manquante du README : {var}"


def test_readme_no_secret_token_leaked() -> None:
    """Aucun vrai token Telegram / clé / chat_id ne doit apparaître."""
    text = _README.read_text(encoding="utf-8")
    # Tokens Telegram = `digits:base64`. Détecte un placeholder accidentel.
    bad = re.findall(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b", text)
    assert not bad, f"Possible token Telegram leaké : {bad}"
