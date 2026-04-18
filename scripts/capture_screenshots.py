"""Capture les 3 screenshots dashboard via Playwright (opt-in, M9).

Pré-requis :
    pip install -e ".[docs]"
    playwright install chromium

Usage :
    # Terminal 1 — démarrer le bot avec données seedées :
    python scripts/seed_demo_db.py
    DASHBOARD_ENABLED=true python -m polycopy --dry-run &
    # Terminal 2 — capture :
    python scripts/capture_screenshots.py --output assets/screenshots/

Captures **manuelles** (non automatisables) à faire séparément :
    - terminal-silent-cli.png : `python -m polycopy --dry-run` puis screenshot
      manuel (ou via `asciinema rec` + conversion). 1280×400.
    - botfather-conversation.png : Telegram Desktop screenshot.
    - vscode-env-edit.png : VSCode ouvrant `.env` avec TARGET_WALLETS souligné.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


async def capture_all(
    output_dir: Path,
    *,
    base_url: str = "http://127.0.0.1:8787",
) -> None:
    """Lance Chromium headless et screenshote 3 pages dashboard."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit(
            'Playwright manquant. Installe via : pip install -e ".[docs]" '
            "&& playwright install chromium",
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — setup one-shot, OK ici
    targets = [
        ("/home", "dashboard-home.png"),
        ("/traders", "dashboard-traders.png"),
        ("/pnl", "dashboard-pnl.png"),
    ]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            color_scheme="dark",
        )
        page = await ctx.new_page()
        for path, fname in targets:
            url = f"{base_url}{path}"
            log.info("capturing", url=url, file=fname)
            await page.goto(url, wait_until="networkidle", timeout=10000)
            # Attente courte pour les sparklines + Chart.js (rendu JS).
            await page.wait_for_timeout(800)
            await page.screenshot(path=str(output_dir / fname), full_page=False)
        await browser.close()
    log.info("capture_done", output_dir=str(output_dir), n=len(targets))


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture 3 screenshots dashboard polycopy.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("assets/screenshots"),
        help="Dossier de sortie (créé si absent).",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8787",
        help="URL du dashboard (default: http://127.0.0.1:8787).",
    )
    args = parser.parse_args()
    asyncio.run(capture_all(args.output, base_url=args.base_url))


if __name__ == "__main__":
    main()
