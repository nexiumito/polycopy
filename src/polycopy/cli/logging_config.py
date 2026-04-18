"""Configuration du logging M9 : RotatingFileHandler + stream conditionnel.

**Invariant strict** : les processors structlog M1..M8 sont préservés
identiques (`add_log_level`, `TimeStamper iso UTC`, `StackInfoRenderer`,
`format_exc_info`, `JSONRenderer`). M9 ajoute uniquement un handler
fichier rotatif (toujours actif) et un handler stdout conditionnel.

Permissions : parent `0o700`, fichier `0o600` (cf. spec §0.6 / §2.2).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


def configure_logging(
    *,
    level: str,
    log_file: Path,
    max_bytes: int,
    backup_count: int,
    silent: bool,
) -> None:
    """Configure root logger + structlog pour M9.

    - File handler `RotatingFileHandler` toujours attaché (chemin créé avec
      mode 0o700 si absent ; fichier `chmod 0o600` après premier write).
    - Stream handler stdout attaché uniquement si `silent=False` (mode
      `--verbose` ou `CLI_SILENT=false`).
    - Structlog reconfiguré aux mêmes processors qu'en M1..M8 (zéro régression).

    Idempotent : appels successifs réinitialisent les handlers du root logger.
    """
    level_int = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level_int)
    # Idempotence : on retire les handlers précédents (basicConfig M1..M8 ou
    # configure_logging précédent) avant d'attacher les nôtres.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # --- File handler (toujours actif) ---------------------------------
    log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir(mode=) est ignoré si le dossier existe déjà → on force l'umask
    # uniquement à la création réelle (best-effort sur les anciens dossiers).
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    file_handler.setLevel(level_int)
    root.addHandler(file_handler)

    # `RotatingFileHandler` ne crée le fichier qu'à la 1re écriture. On
    # touche pour fixer les permissions immédiatement.
    if not log_file.exists():
        log_file.touch(mode=0o600)
    else:
        # Best-effort : Windows NTFS / FS exotique peut refuser → on n'avorte pas.
        with contextlib.suppress(OSError, PermissionError):
            os.chmod(log_file, 0o600)

    # --- Stream handler stdout (conditionnel) ---------------------------
    if not silent:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter("%(message)s"))
        stream_handler.setLevel(level_int)
        root.addHandler(stream_handler)

    # --- Structlog : MÊMES processors que M1..M8 ------------------------
    # Note M9 : on route structlog → stdlib logging via `LoggerFactory()`
    # pour que les handlers root (file + stream conditionnel) reçoivent les
    # JSON. Le default `PrintLoggerFactory(stdout)` de M1..M8 court-circuitait
    # stdlib et empêchait toute rotation fichier — bug latent levé par M9.
    # Les processors restent IDENTIQUES (contrainte spec §0.6 / §2.2).
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
