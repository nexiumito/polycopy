"""Configuration du logging M9 / M10 : RotatingFileHandler + filter noisy.

**Invariant M9** : les processors structlog M1..M8 sont préservés identiques
(``add_log_level``, ``TimeStamper iso UTC``, ``StackInfoRenderer``,
``format_exc_info``, ``JSONRenderer``).

**M10** ajoute un processor en TÊTE de chaîne : ``filter_noisy_endpoints``
drop les ``dashboard_request`` 2xx/3xx des paths polling haute fréquence
**avant** formatage JSON (économie CPU + fichier log 30× moins gros).

Permissions : parent ``0o700``, fichier ``0o600`` (cf. spec §0.6 / §2.2).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
from collections.abc import Iterable, Mapping, MutableMapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

# M10 : paths dashboard polling haute fréquence filtrés par défaut.
# Les patterns sont compilés une fois à l'import et consommés par
# ``make_filter_noisy_endpoints``. Surcharge utilisateur via l'env var
# ``DASHBOARD_LOG_SKIP_PATHS`` (additif, pas de remplacement).
_DEFAULT_NOISY_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/health-external$"),
    re.compile(r"^/partials/.*$"),
    re.compile(r"^/api/version$"),
)


def make_filter_noisy_endpoints(
    extra_patterns: Iterable[str] | None = None,
) -> structlog.types.Processor:
    """Factory : retourne un processor qui drop les dashboard_request noisy.

    Logique (cf. spec M10 §4.1) :
    - Laisse passer tout event qui n'est pas ``dashboard_request``.
    - Laisse passer les statuts 4xx/5xx (observabilité errors préservée).
    - Drop (``raise structlog.DropEvent``) les 2xx/3xx dont le ``path`` match
      la whitelist (defaults + patterns utilisateur).

    La whitelist default est intentionnellement courte (3 patterns) — couvre
    ~95% du volume sur un Home actif (mesure synthèse §2.3).
    """
    compiled: list[re.Pattern[str]] = list(_DEFAULT_NOISY_PATH_PATTERNS)
    if extra_patterns:
        for pat in extra_patterns:
            compiled.append(re.compile(pat))

    def _processor(
        _logger: Any,
        _method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> Mapping[str, Any]:
        if event_dict.get("event") != "dashboard_request":
            return event_dict
        status = event_dict.get("status", 200)
        if isinstance(status, int) and status >= 400:
            return event_dict  # errors always pass
        path = event_dict.get("path", "")
        if isinstance(path, str):
            for pattern in compiled:
                if pattern.match(path):
                    raise structlog.DropEvent
        return event_dict

    return _processor


def configure_logging(
    *,
    level: str,
    log_file: Path,
    max_bytes: int,
    backup_count: int,
    silent: bool,
    skip_paths: Iterable[str] | None = None,
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
    # M10 : insérer ``filter_noisy_endpoints`` EN PREMIER (avant TimeStamper
    # + JSONRenderer) pour économiser le coût de formatage sur les events
    # droppés. Test d'ordre : ``tests/unit/test_logging_config.py``.
    structlog.configure(
        processors=[
            make_filter_noisy_endpoints(skip_paths),
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
