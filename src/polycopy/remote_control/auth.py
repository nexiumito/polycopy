"""Authentification 2FA du ``remote_control`` — TOTP RFC 6238 (M12_bis §4.4).

Phase C — ``TOTPGuard`` : vérifie un code à 6 chiffres via ``pyotp.TOTP``
avec ``valid_window=1`` (±30s de tolérance clock skew). Consommé par
les routes destructives ``/v1/{restart,stop,resume}`` via FastAPI
``Depends``.

Discipline sécurité (CLAUDE.md) :
- Le secret ne quitte jamais la ``TOTPGuard`` instance — ni logs,
  ni responses HTTP, ni messages d'exception.
- Le code TOTP n'est pas loggé non plus (éphémère mais quand même).
- Seuls ``remote_control_totp_verify`` avec ``ok: bool`` + ``ip: str``
  sont émis en log audit.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING

import pyotp
import structlog

from polycopy.monitoring.dtos import Alert

if TYPE_CHECKING:
    from polycopy.remote_control.sentinel import SentinelFile

log = structlog.get_logger(__name__)

_TOTP_CODE_PATTERN: re.Pattern[str] = re.compile(r"^\d{6}$")
_TOTP_VALID_WINDOW: int = 1  # ±30s = 1 step de 30s avant/après

_DEFAULT_RATE_LIMIT_MAX_ATTEMPTS: int = 5
_DEFAULT_RATE_LIMIT_WINDOW_SECONDS: float = 60.0

_DEFAULT_LOCKDOWN_MAX_FAILURES: int = 3
_DEFAULT_LOCKDOWN_WINDOW_SECONDS: float = 60.0


class TOTPGuard:
    """Vérifie un code TOTP 6 chiffres via ``pyotp.TOTP``.

    L'instance est construite une fois au boot avec le secret base32
    (``settings.remote_control_totp_secret``, validé Pydantic). ``verify``
    accepte le code fourni par le client et retourne True/False sans
    jamais raise ni logger le secret.
    """

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("TOTPGuard requires a non-empty secret")
        self._totp: pyotp.TOTP = pyotp.TOTP(secret)

    def verify(self, code: str) -> bool:
        """Retourne True si ``code`` matche le TOTP courant (fenêtre ±30s).

        False dès que :
        - ``code`` n'est pas exactement 6 chiffres (malformed).
        - ``code`` ne matche pas le TOTP dans ``[T-1, T+1]`` steps (30s).
        - Le vérificateur ``pyotp`` retourne False pour toute autre raison.

        Ne raise jamais — la route appelante matérialise le refus en
        HTTP 401 via ``fastapi.HTTPException``.
        """
        if not isinstance(code, str) or not _TOTP_CODE_PATTERN.fullmatch(code):
            return False
        return bool(self._totp.verify(code, valid_window=_TOTP_VALID_WINDOW))


class RateLimiter:
    """Rate limiter sliding-window in-memory par IP (M12_bis §4.4.4).

    Fenêtre glissante via ``collections.deque[float]`` (timestamps
    monotoniques des tentatives). Chaque ``allow(ip)`` purge les entries
    hors fenêtre, puis vérifie si le quota est atteint.

    Not thread-safe — asyncio single-thread suffit pour l'usage FastAPI
    côté `remote_control`. Non persistant : reset au reboot process.

    Injection d'une horloge (``clock``) : permet aux tests de simuler
    le passage du temps sans ``freezegun`` ni monkeypatch global.
    """

    def __init__(
        self,
        *,
        max_attempts: int = _DEFAULT_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds: float = _DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max: int = max_attempts
        self._window: float = window_seconds
        self._clock: Callable[[], float] = clock
        self._history: dict[str, deque[float]] = {}

    def allow(self, ip: str) -> bool:
        """Enregistre une tentative pour ``ip`` et retourne True si autorisée.

        Retourne False si ``max_attempts`` déjà atteint dans la fenêtre
        glissante courante (et n'enregistre PAS la tentative refusée —
        évite de ``push`` le ``max_attempts+1`` qui rendrait la récupération
        plus lente).
        """
        now = self._clock()
        history = self._history.setdefault(ip, deque())
        cutoff = now - self._window
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= self._max:
            return False
        history.append(now)
        return True

    def reset(self, ip: str | None = None) -> None:
        """Purge l'historique pour ``ip`` (ou tout si ``None``).

        Utilitaire tests. Également appelé par ``AutoLockdown.record_success``
        (commit #4) pour effacer le compteur après un TOTP valide.
        """
        if ip is None:
            self._history.clear()
        else:
            self._history.pop(ip, None)


class AutoLockdown:
    """Bloque la machine après ``N`` échecs TOTP consécutifs (M12_bis §4.4.5).

    Trois comportements chaînés sur le 3e échec TOTP dans la fenêtre de 60s :
    1. ``SentinelFile.touch(reason="auto_lockdown_brute_force")`` — pose
       le halt.flag pour que le respawn superviseur entre en mode paused.
    2. ``alerts_queue.put_nowait`` d'une ``Alert`` CRITICAL
       ``remote_control_brute_force_detected`` → Telegram via M4 dispatcher.
    3. Flag ``is_locked`` positionné → toutes les routes destructives
       subséquentes renvoient HTTP 423 Locked (implémenté Phase C commit #5).

    La récupération n'est PAS automatique : un opérateur humain doit
    supprimer le sentinel + redémarrer le process (cf. ``--force-resume``
    Phase D). Coupler avec ``RateLimiter`` côté appelant évite le
    lockdown déclenché trop vite par un attaquant spam.
    """

    def __init__(
        self,
        *,
        sentinel: SentinelFile,
        alerts_queue: asyncio.Queue[Alert] | None = None,
        max_failures: int = _DEFAULT_LOCKDOWN_MAX_FAILURES,
        window_seconds: float = _DEFAULT_LOCKDOWN_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._sentinel: SentinelFile = sentinel
        self._alerts_queue: asyncio.Queue[Alert] | None = alerts_queue
        self._max: int = max_failures
        self._window: float = window_seconds
        self._clock: Callable[[], float] = clock
        self._failures: dict[str, deque[float]] = {}
        self._locked: bool = False

    @property
    def is_locked(self) -> bool:
        """True si lockdown brute-force déclenché dans CE process.

        Sémantique "jusqu'à respawn" (spec §4.4.5) : un respawn démarre
        une nouvelle instance ``AutoLockdown`` avec ``_locked=False``,
        même si le sentinel posé par le brute-force précédent existe
        toujours. Cela permet à ``/resume`` de nettoyer le sentinel
        après redémarrage manuel (``--force-resume`` ou `rm halt.flag`).
        Le sentinel seul n'implique pas lockdown — il peut venir d'un
        ``/stop`` utilisateur ou d'un kill switch normal.
        """
        return self._locked

    def record_failure(self, ip: str) -> bool:
        """Enregistre un échec TOTP pour ``ip``. Retourne True si lockdown s'active."""
        now = self._clock()
        failures = self._failures.setdefault(ip, deque())
        cutoff = now - self._window
        while failures and failures[0] < cutoff:
            failures.popleft()
        failures.append(now)
        if len(failures) >= self._max:
            self._trigger_lockdown(ip, len(failures))
            return True
        return False

    def record_success(self, ip: str) -> None:
        """Reset le compteur d'échecs pour ``ip`` après un TOTP valide."""
        self._failures.pop(ip, None)

    def _trigger_lockdown(self, ip: str, failure_count: int) -> None:
        self._sentinel.touch(reason="auto_lockdown_brute_force")
        self._locked = True
        log.critical(
            "remote_control_auto_lockdown",
            peer_ip=ip,
            failure_count=failure_count,
        )
        if self._alerts_queue is None:
            return
        alert = Alert(
            level="CRITICAL",
            event="remote_control_brute_force_detected",
            body=(
                f"{failure_count} TOTP failures consécutifs depuis {ip} en moins "
                f"de {int(self._window)}s. Machine en auto-lockdown : "
                "rm ~/.polycopy/halt.flag + relance manuelle requise."
            ),
            cooldown_key="remote_control_brute_force",
        )
        try:
            self._alerts_queue.put_nowait(alert)
        except asyncio.QueueFull:
            log.warning("alerts_queue_full_brute_force_alert_dropped")
