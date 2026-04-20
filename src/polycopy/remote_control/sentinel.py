"""Sentinel file ``~/.polycopy/halt.flag`` — pilote le mode ``paused`` (M12_bis §5.2).

Sémantique :
- ``exists()`` — sondage non-destructif au boot par ``cli/runner.py``
  (Phase D) pour bifurquer en mode paused.
- ``touch(reason)`` — posé par ``AutoLockdown`` (Phase C, §4.4.5) +
  ``PnlSnapshotWriter`` sur kill switch (Phase D, §4.6) + handler
  ``/stop`` (Phase C, §4.3.4).
- ``clear()`` — supprimé par ``/resume`` (Phase C) + ``--force-resume``
  CLI (Phase D).
- ``reason()`` — lit le contenu (raison du halt) pour enrichir l'alerte
  ``paused_mode_entered`` au respawn Phase D.

Permissions strict : 0o600 (fichier) + 0o700 (parent). Opération
atomique via ``write_text`` — acceptable car fichier single-line (pas
d'état partiellement écrit lisible).

Phase C scope : consommé uniquement par ``AutoLockdown`` et le handler
``/stop``. L'intégration ``runner.py`` + ``pnl_writer.py`` arrive
Phase D (cf. spec §5 Phase plan).
"""

from __future__ import annotations

from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_PARENT_MODE: int = 0o700
_FILE_MODE: int = 0o600


class SentinelFile:
    """Wrap du halt.flag file avec permissions + idempotence.

    Non thread-safe — unique instance partagée dans le process asyncio
    suffit. Pas de cache : chaque appel interroge le filesystem.
    """

    def __init__(self, path: str | Path) -> None:
        self._path: Path = Path(path).expanduser()

    @property
    def path(self) -> Path:
        """Chemin absolu résolu (après `expanduser`)."""
        return self._path

    def exists(self) -> bool:
        """Retourne True si le sentinel est posé."""
        return self._path.exists()

    def touch(self, reason: str) -> None:
        """Pose le sentinel avec ``reason`` en contenu.

        Crée le parent avec mode 0o700 si absent. Idempotent : une
        ré-invocation overwrite la raison précédente. Emet
        ``remote_control_sentinel_touched`` pour audit.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Chmod parent à chaque appel : défensif si le dossier a été créé
        # par un autre process avec des permissions plus ouvertes.
        self._path.parent.chmod(_PARENT_MODE)
        self._path.write_text(reason + "\n")
        self._path.chmod(_FILE_MODE)
        log.info("remote_control_sentinel_touched", reason=reason, path=str(self._path))

    def clear(self) -> None:
        """Supprime le sentinel (no-op si absent)."""
        if self._path.exists():
            self._path.unlink()
            log.info("remote_control_sentinel_cleared", path=str(self._path))

    def reason(self) -> str | None:
        """Lit la raison du halt (None si le sentinel n'existe pas)."""
        if not self._path.exists():
            return None
        try:
            return self._path.read_text().strip()
        except OSError:
            log.warning("remote_control_sentinel_unreadable", path=str(self._path))
            return None
