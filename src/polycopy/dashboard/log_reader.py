"""Lecteur de fichier log JSONL pour l'onglet `/logs` du dashboard M9.

Read-only strict (jamais d'écriture, jamais de delete). Pas d'I/O DB.
Lecture par chunks depuis la fin du fichier (`SEEK_END`) pour limiter
la consommation RAM même sur un fichier 10 MB. Filtres en-mémoire.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class LogEntry(BaseModel):
    """Une ligne JSON structlog parsée best-effort.

    `extra="allow"` car structlog peut écrire n'importe quel binding
    (`wallet`, `tx_hash`, `asset_id`, ...). Frozen pour usage en cache.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    timestamp: datetime | None = Field(None)
    level: str = Field("INFO")
    event: str = "(no_event)"
    logger: str | None = None

    def all_fields(self) -> dict[str, Any]:
        """Retourne tous les champs (typés + extras) pour rendu template."""
        return self.model_dump(mode="json")


def read_log_tail(log_file: Path, max_lines: int) -> list[LogEntry]:
    """Lit les `max_lines` dernières lignes JSONL et parse en `LogEntry`.

    Utilise `SEEK_END` + chunks 64 KB pour éviter de charger le fichier en
    mémoire entière. Lignes mal formées (non-JSON, e.g. alembic plain text)
    silencieusement ignorées. Retour ordonné chronologiquement (ancien → récent).
    """
    if not log_file.exists():
        return []

    raw_lines = _read_last_lines_bytes(log_file, max_lines)
    entries: list[LogEntry] = []
    for raw in raw_lines:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            entries.append(LogEntry.model_validate(data))
        except ValidationError:
            continue
    return entries


def _read_last_lines_bytes(log_file: Path, max_lines: int) -> list[bytes]:
    """Read `max_lines` derniers segments non-vides séparés par `\\n` depuis la fin."""
    chunk_size = 64 * 1024
    try:
        with log_file.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            buf = b""
            non_empty_count = 0
            while size > 0 and non_empty_count <= max_lines:
                read_size = min(chunk_size, size)
                size -= read_size
                f.seek(size)
                chunk = f.read(read_size)
                buf = chunk + buf
                non_empty_count = sum(1 for line in buf.split(b"\n") if line)
            lines = buf.split(b"\n")
            # Premier segment peut être tronqué (chunk au milieu d'une ligne)
            # → on l'écarte si on n'est pas en début de fichier.
            if size > 0 and lines:
                lines = lines[1:]
            non_empty = [line for line in lines if line]
            return non_empty[-max_lines:]
    except (OSError, FileNotFoundError):
        return []


def filter_entries(
    entries: list[LogEntry],
    *,
    levels: set[str] | None = None,
    event_types: set[str] | None = None,
    q: str | None = None,
) -> list[LogEntry]:
    """Filtre la liste par niveau, event_type et recherche texte.

    - `levels` : intersection avec `entry.level.upper()` (case-insensitive).
    - `event_types` : match exact sur `entry.event`.
    - `q` : substring case-insensitive sur le JSON dump complet (events + extras).
    """
    result = entries
    if levels:
        wanted = {lvl.upper() for lvl in levels}
        result = [e for e in result if e.level.upper() in wanted]
    if event_types:
        result = [e for e in result if e.event in event_types]
    if q:
        q_lower = q.lower()
        result = [e for e in result if q_lower in json.dumps(e.all_fields()).lower()]
    return result
