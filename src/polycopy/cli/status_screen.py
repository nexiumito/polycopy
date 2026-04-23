"""Écran de statut rich M9 — rendu statique au boot + re-render conditionnel.

Pas de `rich.live.Live` (cf. spec §2.1 : statique + observer simple).
Couleur sémantique : cyan en dry-run, rouge en LIVE (signal visuel
immédiat). Rich détecte automatiquement non-TTY et fallback ASCII —
pas de logique custom.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from polycopy.config import Settings


@dataclass(frozen=True)
class ModuleStatus:
    """Statut d'un module pour le rendu statique."""

    name: str
    enabled: bool
    detail: str


def _executor_detail_cli(settings: Settings) -> str:
    """Détail Executor pour l'écran rich — M10 (3 modes)."""
    mode = settings.execution_mode
    if mode == "live":
        return "LIVE"
    if mode == "simulation":
        return "SIMULATION"
    return "dry-run réaliste" if settings.dry_run_realistic_fill else "dry-run"


def _mode_label_and_color(execution_mode: str) -> tuple[str, str]:
    """Badge label + couleur rich pour le bandeau mode de l'écran boot."""
    if execution_mode == "live":
        return "LIVE", "red"
    if execution_mode == "simulation":
        return "SIMULATION", "cyan"
    return "dry-run", "cyan"


def build_initial_module_status(settings: Settings) -> list[ModuleStatus]:
    """Construit la liste des 6 modules à partir des settings au boot."""
    pnl_min = settings.pnl_snapshot_interval_seconds // 60
    discovery_h = settings.discovery_interval_seconds // 3600
    return [
        ModuleStatus(
            name="Watcher",
            enabled=True,
            detail=f"{len(settings.target_wallets)} wallet(s) pinned",
        ),
        ModuleStatus(name="Strategy", enabled=True, detail="6 filtres actifs"),
        ModuleStatus(
            name="Executor",
            enabled=True,
            detail=_executor_detail_cli(settings),
        ),
        ModuleStatus(
            name="Monitoring",
            enabled=True,
            detail=(
                f"Telegram {'ON' if settings.telegram_bot_token else 'OFF'}, PnL {pnl_min} min"
            ),
        ),
        ModuleStatus(
            name="Dashboard",
            enabled=settings.dashboard_enabled,
            detail=(
                f"http://{settings.dashboard_host}:{settings.dashboard_port}"
                if settings.dashboard_enabled
                else "désactivé"
            ),
        ),
        ModuleStatus(
            name="Discovery",
            enabled=settings.discovery_enabled,
            detail=(
                f"{discovery_h}h cycle, {settings.scoring_version}"
                if settings.discovery_enabled
                else "désactivé"
            ),
        ),
    ]


def render_status_screen(
    settings: Settings,
    modules: list[ModuleStatus],
    *,
    version: str,
    console: Console | None = None,
) -> None:
    """Affiche l'écran statique boot (logo + 6 lignes + dashboard + log file)."""
    cons = console or Console()

    body = Table.grid(padding=(0, 2))
    body.add_column(justify="left", style="bold")
    body.add_column(justify="left")
    for mod in modules:
        emoji = "✅" if mod.enabled else "⏸️ "
        body.add_row(emoji, f"{mod.name:<11} {mod.detail}")

    mode_label, color = _mode_label_and_color(settings.execution_mode)
    panel = Panel.fit(
        body,
        title=f"🤖 polycopy v{version}",
        subtitle=f"[bold {color}]{mode_label}[/bold {color}]",
        border_style=color,
    )
    cons.print(panel)

    if settings.dashboard_enabled:
        url = f"http://{settings.dashboard_host}:{settings.dashboard_port}"
        cons.print(f"Dashboard : [cyan]{url}[/cyan]")
    cons.print(f"Logs JSON : [cyan]{settings.log_file}[/cyan]")
    cons.print("Ctrl+C pour arrêter\n")


def render_shutdown_message(
    settings: Settings,
    *,
    console: Console | None = None,
) -> None:
    """Message d'arrêt propre (Ctrl+C ou SIGTERM)."""
    cons = console or Console()
    cons.print(
        f"\n[bold cyan]🛑 polycopy arrêté proprement[/bold cyan] — logs : {settings.log_file}"
    )


def render_crash_message(
    settings: Settings,
    error: BaseException,
    *,
    console: Console | None = None,
) -> None:
    """Message de crash (exception non-rattrapée)."""
    cons = console or Console()
    cons.print(f"\n[bold red]💥 polycopy a crashé[/bold red] : {type(error).__name__}: {error}")
    cons.print(f"Traceback complet dans : [cyan]{settings.log_file}[/cyan]")


def render_log_path_only(log_file: Path, *, console: Console | None = None) -> None:
    """Helper minimal pour le mode --no-cli : juste le chemin du fichier log.

    Pas utilisé en mode daemon strict (cf. spec §0.3 : `--no-cli` = zéro
    stdout) mais utile pour les tests.
    """
    cons = console or Console()
    cons.print(f"polycopy started — logs: {log_file}")
