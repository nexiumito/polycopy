"""Filtres Jinja cosmétiques pour le dashboard M6.

Tous les filtres sont **purement de formatage** : aucune query DB, aucune logique
métier. Ils sont enregistrés au boot par ``routes.build_app`` via
``templates.env.filters.update(...)``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Final

# Empty placeholder — utilisé partout pour les valeurs ``None``.
_EMPTY: Final[str] = "—"

# Circonférence par défaut (cohérent avec la jauge SVG /traders, r=54 → 2πr≈339.292).
_DEFAULT_GAUGE_CIRCUMFERENCE: Final[float] = 339.292


def format_usd(value: float | None) -> str:
    """Formate un montant USD pour l'affichage humain.

    Règles :
    - ``None`` → ``"—"``.
    - ``|x| ≥ 1_000_000`` → ``"$1.2M"``.
    - ``|x| ≥ 1_000`` → ``"$1.2k"``.
    - ``|x| ≥ 1`` → ``"$12.34"``.
    - ``|x| < 1`` → ``"$0.45"`` (2 décimales).
    """
    if value is None:
        return _EMPTY
    abs_value = abs(value)
    sign = "-" if value < 0 else ""
    if abs_value >= 1_000_000:
        return f"{sign}${abs_value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{sign}${abs_value / 1_000:.1f}k"
    if abs_value >= 1:
        return f"{sign}${abs_value:.2f}"
    return f"{sign}${abs_value:.2f}"


def format_size(value: float | None) -> str:
    """Formate une quantité d'outcome tokens (2 décimales fixes)."""
    if value is None:
        return _EMPTY
    return f"{value:.2f}"


def format_pct(value: float | None, with_sign: bool = True) -> str:
    """Formate un pourcentage déjà en unité ``%`` (pas une fraction).

    ``format_pct(3.92)`` → ``"+3.9%"``. Pour une fraction (0.0392), multiplier
    avant de passer à ce filtre — c'est intentionnel pour rester explicite.
    """
    if value is None:
        return _EMPTY
    if with_sign and value > 0:
        return f"+{value:.1f}%"
    return f"{value:.1f}%"


def humanize_dt(dt: datetime | None) -> str:
    """Distance humaine entre maintenant et ``dt`` (UTC).

    - ``None`` → ``"—"``.
    - < 60 s → ``"il y a Xs"``.
    - < 60 min → ``"il y a Xmin"``.
    - < 24 h → ``"il y a Xh"``.
    - < 30 j → ``"il y a Xj"``.
    - ≥ 30 j → ISO date (``"2026-04-18"``).
    """
    if dt is None:
        return _EMPTY
    now = datetime.now(tz=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "à l'instant"
    if seconds < 60:
        return f"il y a {seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"il y a {minutes}min"
    hours = minutes // 60
    if hours < 24:
        return f"il y a {hours}h"
    days = hours // 24
    if days < 30:
        return f"il y a {days}j"
    return dt.date().isoformat()


def short_hash(value: str | None, width: int = 4) -> str:
    """Tronque un hash hex pour affichage compact : ``"0xabcd…7890"``.

    - ``None`` ou vide → ``"—"``.
    - longueur ≤ 2 + 2*width + 1 → renvoyé tel quel.
    """
    if not value:
        return _EMPTY
    width = max(width, 1)
    if value.startswith("0x"):
        prefix = "0x"
        body = value[2:]
    else:
        prefix = ""
        body = value
    if len(body) <= 2 * width + 1:
        return value
    return f"{prefix}{body[:width]}…{body[-width:]}"


def wallet_label(trader: Any) -> str:
    """Affiche le label si défini, sinon ``short_hash(wallet_address)``.

    Tolère les types ``TraderRow`` / ``TargetTrader`` / dict — tous exposent
    ``label`` et ``wallet_address``.
    """
    label = getattr(trader, "label", None) or (
        trader.get("label") if isinstance(trader, dict) else None
    )
    if label:
        return str(label)
    address = getattr(trader, "wallet_address", None) or (
        trader.get("wallet_address") if isinstance(trader, dict) else None
    )
    return short_hash(address)


def score_to_dasharray(
    score: float | None,
    circumference: float = _DEFAULT_GAUGE_CIRCUMFERENCE,
) -> str:
    """Convertit ``score ∈ [0, 1]`` en attribut ``stroke-dasharray`` SVG.

    Format : ``"<filled> <empty>"``. Les deux sommés = ``circumference``.
    Score ``None`` → cercle complètement vide.
    """
    if score is None or score <= 0:
        return f"0 {circumference:.3f}"
    clamped = min(max(score, 0.0), 1.0)
    filled = clamped * circumference
    empty = circumference - filled
    return f"{filled:.3f} {empty:.3f}"


def side_icon(side: str | None) -> str:
    """Renvoie le nom Lucide pour un ``side`` BUY/SELL."""
    if side and side.upper() == "SELL":
        return "arrow-down-circle"
    return "arrow-up-circle"


def status_badge_class(status: str | None) -> str:
    """Renvoie la classe CSS du badge selon le ``status`` (M3/M5)."""
    if not status:
        return "badge badge-neutral"
    upper = status.upper()
    if upper in {"FILLED", "APPROVED", "ACTIVE"}:
        return "badge badge-ok"
    if upper in {"REJECTED", "FAILED"}:
        return "badge badge-error"
    if upper in {"SIMULATED"}:
        return "badge badge-info"
    if upper in {"SHADOW"}:
        return "badge badge-info"
    if upper in {"PAUSED", "PARTIALLY_FILLED", "SENT"}:
        return "badge badge-warning"
    if upper in {"PINNED"}:
        return "badge badge-pinned"
    return "badge badge-neutral"


def sparkline_svg(
    points: list[tuple[datetime, float]] | None,
    width: int = 240,
    height: int = 32,
    stroke: str = "currentColor",
) -> str:
    """Sparkline SVG inline. Renvoie une chaîne HTML prête à inclure (``| safe``).

    Choix : zéro JS, zéro lib. ``points`` peut être ``None``/vide (renvoie un
    espace réservé visuel discret). Calcule un polyline normalisé sur ``[0, width]``
    horizontal et ``[0, height]`` vertical (inversé Y).
    """
    if not points or len(points) < 2:
        return (
            f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img" aria-label="sparkline indisponible" '
            f'class="sparkline sparkline-empty"></svg>'
        )
    values = [v for _, v in points]
    v_min = min(values)
    v_max = max(values)
    span = v_max - v_min
    if span <= 0 or math.isclose(span, 0.0):
        # Ligne plate au milieu — évite division par zéro.
        y = height / 2
        coords = " ".join(
            f"{(i / (len(values) - 1)) * width:.2f},{y:.2f}" for i in range(len(values))
        )
    else:
        coords = " ".join(
            f"{(i / (len(values) - 1)) * width:.2f},{(height - ((v - v_min) / span) * height):.2f}"
            for i, v in enumerate(values)
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="sparkline" class="sparkline">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round" points="{coords}" />'
        f"</svg>"
    )


def all_filters() -> dict[str, Any]:
    """Retourne le dict des filtres à enregistrer dans ``templates.env.filters``."""
    return {
        "format_usd": format_usd,
        "format_size": format_size,
        "format_pct": format_pct,
        "humanize_dt": humanize_dt,
        "short_hash": short_hash,
        "wallet_label": wallet_label,
        "score_to_dasharray": score_to_dasharray,
        "side_icon": side_icon,
        "status_badge_class": status_badge_class,
        "sparkline_svg": sparkline_svg,
    }


__all__ = [
    "all_filters",
    "format_pct",
    "format_size",
    "format_usd",
    "humanize_dt",
    "score_to_dasharray",
    "short_hash",
    "side_icon",
    "sparkline_svg",
    "status_badge_class",
    "wallet_label",
]
