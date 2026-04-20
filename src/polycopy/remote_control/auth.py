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

import pyotp
import structlog

log = structlog.get_logger(__name__)

_TOTP_CODE_PATTERN: re.Pattern[str] = re.compile(r"^\d{6}$")
_TOTP_VALID_WINDOW: int = 1  # ±30s = 1 step de 30s avant/après


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
