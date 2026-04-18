"""Tests `polycopy.cli.runner` — parsing args + main exit codes.

Pas d'asyncio.run réel : on monkeypatch `_async_main` pour isoler.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from polycopy.cli import runner


@pytest.fixture(autouse=True)
def _isolate_root_logger() -> None:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def test_parse_args_default() -> None:
    args = runner._parse_args([])
    assert args.dry_run is False
    assert args.verbose is False
    assert args.no_cli is False
    assert args.log_level is None


def test_parse_args_all_flags() -> None:
    args = runner._parse_args(["--dry-run", "--verbose", "--no-cli", "--log-level", "DEBUG"])
    assert args.dry_run is True
    assert args.verbose is True
    assert args.no_cli is True
    assert args.log_level == "DEBUG"


def test_parse_args_invalid_log_level_exits() -> None:
    with pytest.raises(SystemExit):
        runner._parse_args(["--log-level", "TRACE"])


def test_main_dry_run_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`main(['--dry-run'])` → silent + exit 0 (logique async stubée)."""
    log_file = tmp_path / "logs" / "polycopy.log"
    monkeypatch.setattr(runner.settings, "log_file", log_file)
    monkeypatch.setattr(runner.settings, "cli_silent", True)
    monkeypatch.setattr(runner.settings, "dashboard_enabled", False)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(runner, "_async_main", _noop)
    code = runner.main(["--dry-run"])
    assert code == 0
    # File handler attached.
    handlers = logging.getLogger().handlers
    assert any(isinstance(h, RotatingFileHandler) for h in handlers)


def test_main_verbose_attaches_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "polycopy.log"
    monkeypatch.setattr(runner.settings, "log_file", log_file)
    monkeypatch.setattr(runner.settings, "cli_silent", True)
    monkeypatch.setattr(runner.settings, "dashboard_enabled", False)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(runner, "_async_main", _noop)
    code = runner.main(["--dry-run", "--verbose"])
    assert code == 0
    handlers = logging.getLogger().handlers
    stream_only = [
        h
        for h in handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    ]
    assert len(stream_only) == 1


def test_main_no_cli_no_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "polycopy.log"
    monkeypatch.setattr(runner.settings, "log_file", log_file)
    monkeypatch.setattr(runner.settings, "cli_silent", False)
    monkeypatch.setattr(runner.settings, "dashboard_enabled", False)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(runner, "_async_main", _noop)
    # --no-cli = silent, même si CLI_SILENT=false
    code = runner.main(["--dry-run", "--no-cli"])
    assert code == 0
    handlers = logging.getLogger().handlers
    stream_only = [
        h
        for h in handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    ]
    assert stream_only == []


def test_main_keyboard_interrupt_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "polycopy.log"
    monkeypatch.setattr(runner.settings, "log_file", log_file)
    monkeypatch.setattr(runner.settings, "cli_silent", True)
    monkeypatch.setattr(runner.settings, "dashboard_enabled", False)

    async def _kb() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_async_main", _kb)
    code = runner.main(["--dry-run"])
    assert code == 0


def test_main_unexpected_exception_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "polycopy.log"
    monkeypatch.setattr(runner.settings, "log_file", log_file)
    monkeypatch.setattr(runner.settings, "cli_silent", True)
    monkeypatch.setattr(runner.settings, "dashboard_enabled", False)

    async def _boom() -> None:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(runner, "_async_main", _boom)
    code = runner.main(["--dry-run"])
    assert code == 1


def test_install_signal_handlers_registers_callbacks() -> None:
    """Vérifie que les handlers SIGINT/SIGTERM sont bien attachés et set stop_event."""
    import asyncio
    import signal as _signal

    captured: dict[int, object] = {}

    class _FakeLoop:
        def add_signal_handler(self, sig: int, callback: object) -> None:
            captured[sig] = callback

    stop = asyncio.Event()
    runner._install_signal_handlers(_FakeLoop(), stop)
    assert _signal.SIGINT in captured
    assert _signal.SIGTERM in captured
    # Invoque le callback : doit set stop_event sans erreur.
    cb = captured[_signal.SIGINT]
    assert callable(cb)
    cb()  # type: ignore[operator]
    assert stop.is_set()
    # Second appel : pas d'erreur, no-op idempotent.
    cb()  # type: ignore[operator]


def test_install_signal_handlers_handles_unsupported_platform() -> None:
    """`add_signal_handler` raise NotImplementedError sous Windows ProactorLoop — bypass propre."""
    import asyncio

    class _NotImplLoop:
        def add_signal_handler(self, sig: int, callback: object) -> None:
            raise NotImplementedError("simulated Windows")

    stop = asyncio.Event()
    # Ne doit PAS raise.
    runner._install_signal_handlers(_NotImplLoop(), stop)
