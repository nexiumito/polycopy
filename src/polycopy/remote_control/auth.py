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

import re
import time
from collections import deque
from collections.abc import Callable

import pyotp
import structlog

log = structlog.get_logger(__name__)

_TOTP_CODE_PATTERN: re.Pattern[str] = re.compile(r"^\d{6}$")
_TOTP_VALID_WINDOW: int = 1  # ±30s = 1 step de 30s avant/après

_DEFAULT_RATE_LIMIT_MAX_ATTEMPTS: int = 5
_DEFAULT_RATE_LIMIT_WINDOW_SECONDS: float = 60.0


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
