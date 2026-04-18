"""Tests M10 §10.3 + §8.3 — cascade user override ``assets/telegram/``.

Un user peut surcharger un template polycopy dans ``<root>/assets/telegram/``.
M10 injecte ``mode_badge`` dans chaque context, mais ``StrictUndefined`` ne
crash que sur lookup d'une var absente, pas sur un binding **fourni mais non
utilisé**. Ce test vérifie qu'un user template qui ignore ``mode_badge`` rend
sans crash (UX dégradée mais non fatale).
"""

from __future__ import annotations

from pathlib import Path

from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import Alert


def test_user_template_without_mode_badge_renders_without_crash(
    tmp_path: Path,
) -> None:
    user_dir = tmp_path / "assets" / "telegram"
    user_dir.mkdir(parents=True)
    (user_dir / "executor_error.md.j2").write_text(
        "⚠️ custom template\n{{ body | telegram_md_escape }}\n",
    )
    renderer = AlertRenderer(project_root=tmp_path, mode="dry_run")
    out = renderer.render_alert(
        Alert(level="ERROR", event="executor_error", body="booked a cap."),
    )
    # Pas de badge header, pas de crash, body escape OK.
    assert out.startswith("⚠️ custom template")
    assert "booked a cap\\." in out
