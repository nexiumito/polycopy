"""Rendu Markdown v2 Telegram via templates Jinja2 (M7 §7).

Cascade de chemins (user overrides → defaults) :
1. ``assets/telegram/`` à la racine du projet — surcharges utilisateur.
2. ``src/polycopy/monitoring/templates/`` — défauts livrés avec polycopy.

``autoescape=False`` car Markdown v2 ≠ HTML. On échappe explicitement via le
filter ``telegram_md_escape`` dans chaque template. ``StrictUndefined`` fait
crasher explicitement si un template référence une variable absente.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateNotFound,
)

from polycopy.monitoring.dtos import (
    Alert,
    DailySummaryContext,
    DigestContext,
    HeartbeatContext,
    ShutdownContext,
    StartupContext,
)
from polycopy.monitoring.md_escape import (
    format_usd_tg,
    humanize_dt_tg,
    telegram_md_escape,
    wallet_short,
)

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)

_TELEGRAM_MAX_MSG_LENGTH: int = 4096

_LEVEL_EMOJI: dict[str, str] = {
    "INFO": "🟢",
    "WARNING": "🟡",
    "ERROR": "🔴",
    "CRITICAL": "🚨",
}

# M10 : badge visuel pour distinguer les modes dans les messages Telegram.
# Injecté en header de chaque template via le binding ``mode_badge``.
_MODE_BADGE: dict[str, str] = {
    "simulation": "🟢 SIMULATION",
    "dry_run": "🟢 DRY-RUN",
    "live": "🔴 LIVE",
}


class AlertRenderer:
    """Rendu Markdown v2 Telegram des alertes + messages M7 via Jinja2."""

    def __init__(
        self,
        project_root: Path | None = None,
        mode: str = "dry_run",
    ) -> None:
        root = project_root if project_root is not None else Path.cwd()
        search_paths: list[str] = []
        user_dir = root / "assets" / "telegram"
        if user_dir.exists() and user_dir.is_dir():
            search_paths.append(str(user_dir))
        default_dir = Path(__file__).parent / "templates"
        search_paths.append(str(default_dir))

        loader = FileSystemLoader(search_paths)
        self.env = Environment(
            loader=loader,
            autoescape=False,  # noqa: S701 — Markdown v2 ≠ HTML ; escape via filter telegram_md_escape
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
            keep_trailing_newline=False,
        )
        self.env.filters["telegram_md_escape"] = telegram_md_escape
        self.env.filters["wallet_short"] = wallet_short
        self.env.filters["format_usd_tg"] = format_usd_tg
        self.env.filters["humanize_dt_tg"] = humanize_dt_tg
        self._mode = mode
        # M10 : le badge est pré-résolu ; fallback défensif au cas d'un mode
        # inconnu (ne devrait jamais arriver vu la Literal côté Settings).
        self._mode_badge = _MODE_BADGE.get(mode, f"? {mode}")

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def render_alert(self, alert: Alert) -> str:
        """Render une ``Alert`` via son template dédié ou le fallback.

        Si le template ``{event_type}.md.j2`` n'existe pas, ``fallback.md.j2``
        reproduit le format M4 (``emoji *[event]*\\nbody``).
        """
        template_name = f"{alert.event}.md.j2"
        context = self._inject_mode(
            {
                "event_type": alert.event,
                "level": alert.level,
                "body": alert.body,
                "emoji": _LEVEL_EMOJI.get(alert.level, ""),
            },
        )
        try:
            template = self.env.get_template(template_name)
        except TemplateNotFound:
            template = self.env.get_template("fallback.md.j2")
        return self._finalize(template.render(**context))

    def render_startup(self, context: StartupContext) -> str:
        template = self.env.get_template("startup.md.j2")
        return self._finalize(template.render(**self._startup_vars(context)))

    def render_shutdown(self, context: ShutdownContext) -> str:
        template = self.env.get_template("shutdown.md.j2")
        return self._finalize(template.render(**self._inject_mode(context.model_dump())))

    def render_heartbeat(self, context: HeartbeatContext) -> str:
        template = self.env.get_template("heartbeat.md.j2")
        return self._finalize(template.render(**self._inject_mode(context.model_dump())))

    def render_daily_summary(self, context: DailySummaryContext) -> str:
        template = self.env.get_template("daily_summary.md.j2")
        return self._finalize(template.render(**self._inject_mode(context.model_dump())))

    def render_digest(self, context: DigestContext) -> str:
        template = self.env.get_template("digest.md.j2")
        return self._finalize(template.render(**self._inject_mode(context.model_dump())))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _inject_mode(self, base_ctx: dict[str, Any]) -> dict[str, Any]:
        """Ajoute ``mode`` + ``mode_badge`` au context Jinja (M10 §3.4.1).

        Les templates peuvent référencer ``{{ mode_badge | telegram_md_escape }}``
        pour afficher le badge visuel. ``mode`` est disponible pour du contenu
        conditionnel fin si besoin, mais M10 privilégie le badge seul.
        """
        base_ctx.setdefault("mode", self._mode)
        base_ctx.setdefault("mode_badge", self._mode_badge)
        return base_ctx

    def _startup_vars(self, context: StartupContext) -> dict[str, Any]:
        """Serialise StartupContext vers un dict consommable par Jinja."""
        data = context.model_dump()
        # M10 : le StartupContext porte déjà `mode` (enum execution_mode),
        # mais on ajoute le badge sans overrider le champ existant.
        data.setdefault("mode_badge", self._mode_badge)
        return data

    @staticmethod
    def _finalize(rendered: str) -> str:
        """Tronque à 4096 chars (limite Telegram) et strip trailing blanks."""
        stripped = rendered.rstrip()
        if len(stripped) <= _TELEGRAM_MAX_MSG_LENGTH:
            return stripped
        log.warning("telegram_message_truncated", length=len(stripped))
        return stripped[: _TELEGRAM_MAX_MSG_LENGTH - 1] + "…"
