"""Helpers d'échappement et de formatage pour Markdown v2 Telegram (M7).

Markdown v2 est strict : les caractères ``_*[]()~`>#+-=|{}.!`` doivent être
échappés avec ``\\`` dans toute valeur injectée dans un template. Sinon, l'API
Telegram répond ``400 Bad Request`` et le message n'est jamais délivré.

Référence : https://core.telegram.org/bots/api#markdownv2-style
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Ordre : le ``\\`` est traité implicitement par la boucle (pas dans le set).
_ESCAPE_CHARS: frozenset[str] = frozenset("_*[]()~`>#+-=|{}.!")


def telegram_md_escape(value: Any) -> str:
    """Échappe les caractères Markdown v2 dans une valeur scalaire.

    ``None`` devient chaîne vide. Tout autre type est ``str()``-ifié.
    """
    if value is None:
        return ""
    source = str(value)
    out: list[str] = []
    for char in source:
        if char in _ESCAPE_CHARS:
            out.append("\\")
        out.append(char)
    return "".join(out)


def wallet_short(wallet: str | None, width: int = 4) -> str:
    """Retourne une adresse EVM raccourcie ``0xabcd…cdef`` (non échappée).

    Si ``wallet`` est vide, ``None`` ou trop court, retourne la string brute
    (éventuellement vide). L'appelant doit échapper le résultat si nécessaire.
    """
    if not wallet:
        return ""
    if len(wallet) < 2 * width + 2:
        return wallet
    return f"{wallet[: 2 + width]}…{wallet[-width:]}"


def format_usd_tg(value: float | int | None) -> str:
    """Formate un montant USD pour un template Markdown v2 (``$`` déjà échappé).

    - ``None`` → ``"—"`` (tiret cadratin, pas de ``$``).
    - ``|v| >= 1000`` → ``"\\$1\\.2k"``.
    - Sinon → ``"\\$0\\.45"`` (2 décimales).
    """
    if value is None:
        return "—"
    amount = float(value)
    text = f"${amount / 1000:.1f}k" if abs(amount) >= 1000 else f"${amount:.2f}"
    return telegram_md_escape(text)


def humanize_dt_tg(dt: datetime | None) -> str:
    """Formate un datetime en ``YYYY-MM-DD HH:MM UTC`` échappé Markdown v2."""
    if dt is None:
        return "—"
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    text = aware.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return telegram_md_escape(text)


def humanize_duration(seconds: float) -> str:
    """Retourne un duration human-readable court, non échappé.

    Exemples : ``30 s``, ``5 min``, ``2 h 14 min``, ``3 j 4 h``.
    """
    total = int(max(0, seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    if days > 0:
        return f"{days} j {hours} h"
    if hours > 0:
        return f"{hours} h {minutes:02d} min"
    if minutes > 0:
        return f"{minutes} min"
    return f"{sec} s"
