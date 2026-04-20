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
        machine_id: str = "UNKNOWN",
        machine_emoji: str = "🖥️",
        dashboard_url: str | None = None,
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
        # M12_bis : identité multi-machine injectée dans chaque contexte
        # template (cf. spec §3.2).
        self._machine_id = machine_id
        self._machine_emoji = machine_emoji
        # M12_bis Phase G : URL dashboard injectée dans chaque template pour
        # fournir un lien cliquable (``[📊 Dashboard](url)``) depuis téléphone.
        # Calculée une fois au boot par ``compute_dashboard_url(settings)``.
        self._dashboard_url = dashboard_url

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
        """Ajoute ``mode`` + ``mode_badge`` + ``machine_id`` + ``machine_emoji``.

        M10 : ``mode_badge`` (🟢 DRY-RUN / 🔴 LIVE / 🟢 SIMULATION) —
        consommé par la 1ère ligne de chaque template.
        M12_bis §3.2 : ``machine_id`` + ``machine_emoji`` — consommés par la
        2e ligne (``{{ machine_emoji }} *{{ machine_id | telegram_md_escape }}*``)
        pour identifier la machine source en multi-machine.
        """
        base_ctx.setdefault("mode", self._mode)
        base_ctx.setdefault("mode_badge", self._mode_badge)
        base_ctx.setdefault("machine_id", self._machine_id)
        base_ctx.setdefault("machine_emoji", self._machine_emoji)
        # M12_bis Phase G : ``dashboard_url`` — les DTO (DigestContext,
        # DailySummaryContext) sérialisent la clé à ``None`` via
        # ``model_dump()``, donc ``setdefault`` ne suffirait pas : on override
        # uniquement si la valeur sérialisée vaut ``None``. Si le DTO/caller
        # porte une URL explicite, elle gagne (backwards-compat M7).
        if base_ctx.get("dashboard_url") is None:
            base_ctx["dashboard_url"] = self._dashboard_url
        return base_ctx

    def _startup_vars(self, context: StartupContext) -> dict[str, Any]:
        """Serialise StartupContext vers un dict consommable par Jinja."""
        data = context.model_dump()
        # M10 : le StartupContext porte déjà `mode` (enum execution_mode),
        # mais on ajoute le badge sans overrider le champ existant.
        data.setdefault("mode_badge", self._mode_badge)
        # M12_bis §3.2 : idem pour les bindings machine.
        data.setdefault("machine_id", self._machine_id)
        data.setdefault("machine_emoji", self._machine_emoji)
        # M12_bis Phase G : inject fallback dashboard_url si le DTO n'en porte
        # pas (ou en porte un à ``None``). ``model_dump`` renvoie toujours la
        # clé — on override uniquement si c'est explicitement ``None``.
        if data.get("dashboard_url") is None:
            data["dashboard_url"] = self._dashboard_url
        return data

    @staticmethod
    def _finalize(rendered: str) -> str:
        """Tronque à 4096 chars (limite Telegram) et strip trailing blanks."""
        stripped = rendered.rstrip()
        if len(stripped) <= _TELEGRAM_MAX_MSG_LENGTH:
            return stripped
        log.warning("telegram_message_truncated", length=len(stripped))
        return stripped[: _TELEGRAM_MAX_MSG_LENGTH - 1] + "…"
