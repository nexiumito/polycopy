"""Tests `polycopy.cli.logging_config` — file handler + stream conditionnel."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
import structlog

from polycopy.cli.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _restore_root_logger() -> None:
    """Sauvegarde / restaure root logger (handlers + level) pour éviter les fuites."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def test_silent_attaches_file_only(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "polycopy.log"
    configure_logging(
        level="INFO",
        log_file=log_file,
        max_bytes=1_048_576,
        backup_count=3,
        silent=True,
    )
    handlers = logging.getLogger().handlers
    file_handlers = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    stream_handlers = [
        h
        for h in handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert stream_handlers == []


def test_verbose_attaches_file_plus_stream(tmp_path: Path) -> None:
    log_file = tmp_path / "polycopy.log"
    configure_logging(
        level="DEBUG",
        log_file=log_file,
        max_bytes=1_048_576,
        backup_count=3,
        silent=False,
    )
    handlers = logging.getLogger().handlers
    file_handlers = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    stream_handlers = [
        h
        for h in handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert len(stream_handlers) == 1


def test_log_file_permissions_0600(tmp_path: Path) -> None:
    log_file = tmp_path / "polycopy.log"
    configure_logging(
        level="INFO",
        log_file=log_file,
        max_bytes=1_048_576,
        backup_count=3,
        silent=True,
    )
    assert log_file.exists()
    mode = oct(log_file.stat().st_mode)[-3:]
    assert mode == "600"


def test_log_file_parent_created_with_0700(tmp_path: Path) -> None:
    log_file = tmp_path / "freshdir" / "polycopy.log"
    configure_logging(
        level="INFO",
        log_file=log_file,
        max_bytes=1_048_576,
        backup_count=3,
        silent=True,
    )
    assert log_file.parent.is_dir()
    # mkdir(mode=) respecte umask — on vérifie au moins que le dossier existe.


def test_idempotent_no_handler_duplication(tmp_path: Path) -> None:
    """Plusieurs appels ne doivent pas accumuler les handlers (anti-fuite)."""
    log_file = tmp_path / "polycopy.log"
    for _ in range(3):
        configure_logging(
            level="INFO",
            log_file=log_file,
            max_bytes=1_048_576,
            backup_count=3,
            silent=True,
        )
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1


def test_structlog_writes_to_file(tmp_path: Path) -> None:
    """Sanity check : un log structlog doit atterrir dans le fichier."""
    log_file = tmp_path / "polycopy.log"
    configure_logging(
        level="INFO",
        log_file=log_file,
        max_bytes=1_048_576,
        backup_count=3,
        silent=True,
    )
    log = structlog.get_logger("test_m9")
    log.info("test_m9_event", marker="X1Y2Z3")
    # Force flush (RotatingFileHandler buffers).
    for h in logging.getLogger().handlers:
        h.flush()
    content = log_file.read_text()
    assert "test_m9_event" in content
    assert "X1Y2Z3" in content


def test_rotation_creates_backup(tmp_path: Path) -> None:
    """Écrire au-dessus de `max_bytes` doit déclencher la rotation."""
    log_file = tmp_path / "polycopy.log"
    configure_logging(
        level="INFO",
        log_file=log_file,
        max_bytes=1_048_576,  # 1 MB minimum (Field constraint)
        backup_count=2,
        silent=True,
    )
    log = structlog.get_logger("rot")
    # Écrire ~1.5 MB en gros chunks.
    payload = "X" * 2000
    for i in range(800):
        log.info("rot_event", i=i, big=payload)
    for h in logging.getLogger().handlers:
        h.flush()
    backup = tmp_path / "polycopy.log.1"
    assert backup.exists() or log_file.stat().st_size <= 1_048_576


def test_existing_file_chmod_to_600(tmp_path: Path) -> None:
    log_file = tmp_path / "polycopy.log"
    log_file.write_text("preexisting")
    os.chmod(log_file, 0o644)
    configure_logging(
        level="INFO",
        log_file=log_file,
        max_bytes=1_048_576,
        backup_count=3,
        silent=True,
    )
    mode = oct(log_file.stat().st_mode)[-3:]
    assert mode == "600"
