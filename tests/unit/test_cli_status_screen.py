"""Tests `polycopy.cli.status_screen` — rendu rich + statuts modules."""

from __future__ import annotations

import io

from rich.console import Console

from polycopy.cli.status_screen import (
    ModuleStatus,
    build_initial_module_status,
    render_crash_message,
    render_shutdown_message,
    render_status_screen,
)
from polycopy.config import Settings


def _make_settings(**overrides: object) -> Settings:
    base = {
        "_env_file": None,
        "target_wallets": ["0xaa", "0xbb"],
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _render_to_string(settings: Settings, modules: list[ModuleStatus]) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    render_status_screen(settings, modules, version="9.9.9-test", console=console)
    return buf.getvalue()


def test_build_initial_module_status_six_lines() -> None:
    settings = _make_settings()
    mods = build_initial_module_status(settings)
    names = [m.name for m in mods]
    assert names == [
        "Watcher",
        "Strategy",
        "Executor",
        "Monitoring",
        "Dashboard",
        "Discovery",
    ]


def test_build_initial_module_status_dashboard_disabled_default() -> None:
    settings = _make_settings()
    mods = build_initial_module_status(settings)
    dashboard = next(m for m in mods if m.name == "Dashboard")
    assert dashboard.enabled is False
    assert "désactivé" in dashboard.detail


def test_build_initial_module_status_dashboard_enabled() -> None:
    settings = _make_settings(dashboard_enabled=True)
    mods = build_initial_module_status(settings)
    dashboard = next(m for m in mods if m.name == "Dashboard")
    assert dashboard.enabled is True
    assert "127.0.0.1:8787" in dashboard.detail


def test_build_initial_module_status_executor_dry_run_realistic() -> None:
    settings = _make_settings(dry_run=True, dry_run_realistic_fill=True)
    mods = build_initial_module_status(settings)
    executor = next(m for m in mods if m.name == "Executor")
    assert "réaliste" in executor.detail


def test_build_initial_module_status_executor_live() -> None:
    settings = _make_settings(dry_run=False)
    mods = build_initial_module_status(settings)
    executor = next(m for m in mods if m.name == "Executor")
    assert executor.detail == "LIVE"


def test_render_status_screen_dry_run_contains_modules() -> None:
    settings = _make_settings(dashboard_enabled=True)
    mods = build_initial_module_status(settings)
    out = _render_to_string(settings, mods)
    assert "polycopy v9.9.9-test" in out
    assert "dry-run" in out
    assert "Watcher" in out
    assert "Dashboard" in out
    assert "127.0.0.1:8787" in out
    assert "Logs JSON" in out
    assert "Ctrl+C" in out


def test_render_status_screen_live_label() -> None:
    settings = _make_settings(
        dry_run=False,
        polymarket_private_key="0x" + "a" * 64,
        polymarket_funder="0x" + "b" * 40,
    )
    mods = build_initial_module_status(settings)
    out = _render_to_string(settings, mods)
    assert "LIVE" in out


def test_render_status_screen_disabled_module_paused_emoji() -> None:
    settings = _make_settings()  # discovery_enabled default False
    mods = build_initial_module_status(settings)
    out = _render_to_string(settings, mods)
    assert "Discovery" in out
    assert "désactivé" in out


def test_render_shutdown_message() -> None:
    settings = _make_settings()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    render_shutdown_message(settings, console=console)
    out = buf.getvalue()
    assert "polycopy arrêté" in out


def test_render_crash_message_includes_exception() -> None:
    settings = _make_settings()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    render_crash_message(settings, RuntimeError("boom xyz"), console=console)
    out = buf.getvalue()
    assert "crashé" in out
    assert "RuntimeError" in out
    assert "boom xyz" in out
    assert "Traceback complet" in out
