"""Configuration centralisée via Pydantic Settings.

Toutes les variables sont chargées depuis l'environnement (ou .env en dev).
Aucune valeur sensible en dur dans le code.
"""

import json
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Settings du bot, validées au démarrage."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Polymarket wallet ---
    polymarket_private_key: str | None = Field(
        None,
        description="Clé privée du wallet de signature (requis à M3)",
    )
    polymarket_funder: str | None = Field(
        None,
        description="Adresse du proxy wallet (requis à M3)",
    )
    polymarket_signature_type: int = Field(1, ge=0, le=2)

    # --- Cibles ---
    # `NoDecode` désactive le JSON-decode auto de pydantic-settings pour ce champ ;
    # le validator ci-dessous reçoit la string brute et gère CSV + JSON.
    target_wallets: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("target_wallets", mode="before")
    @classmethod
    def _parse_target_wallets(cls, v: object) -> object:
        """Accepte `TARGET_WALLETS` en CSV (`0xabc,0xdef`) ou en JSON (`["0xabc","0xdef"]`)."""
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v

    # --- Sizing & risk ---
    copy_ratio: float = Field(0.01, gt=0, le=1)
    max_position_usd: float = Field(100, gt=0)
    min_market_liquidity_usd: float = Field(5000, ge=0)
    min_hours_to_expiry: float = Field(24, ge=0)
    max_slippage_pct: float = Field(2.0, ge=0)
    kill_switch_drawdown_pct: float = Field(20, ge=0, le=100)
    risk_available_capital_usd_stub: float = Field(
        1000.0,
        gt=0,
        description=(
            "Stub M2 du capital dispo pour le RiskManager. "
            "Remplacé par lecture wallet on-chain à M3."
        ),
    )

    # --- Polling ---
    poll_interval_seconds: int = Field(5, ge=1)

    # --- Storage ---
    database_url: str = "sqlite+aiosqlite:///polycopy.db"

    # --- Mode ---
    dry_run: bool = True

    # --- Mode dry-run réaliste (M8, opt-in strict) -----------------------
    dry_run_realistic_fill: bool = Field(
        False,
        description=(
            "Opt-in M8. Si true ET DRY_RUN=true, simule chaque FOK sur la "
            "profondeur orderbook réelle (read-only `/book`) au lieu du fill "
            "stub instantané M3. Ignoré en live (`DRY_RUN=false`)."
        ),
    )
    dry_run_virtual_capital_usd: float = Field(
        1000.0,
        ge=10.0,
        le=1_000_000.0,
        description=(
            "Capital initial virtuel pour le PnL dry-run M8. Remplace "
            "RISK_AVAILABLE_CAPITAL_USD_STUB uniquement dans les snapshots "
            "is_dry_run=true. Ne pilote PAS le RiskManager M2."
        ),
    )
    dry_run_book_cache_ttl_seconds: int = Field(
        5,
        ge=1,
        le=60,
        description=(
            "TTL du cache in-memory ClobOrderbookReader (M8). 5 s par défaut "
            "= compromis fraîcheur / efficacité (cf. spec §2.6)."
        ),
    )
    dry_run_resolution_poll_minutes: int = Field(
        30,
        ge=5,
        le=1440,
        description=(
            "Cadence du DryRunResolutionWatcher M8 — vérifie les marchés "
            "virtuels résolus et matérialise le realized_pnl."
        ),
    )
    dry_run_allow_partial_book: bool = Field(
        False,
        description=(
            "M8 : si false (default), FOK strict — book insuffisant → REJECT. "
            "Si true, accepte un fill partiel (s'écarte du comportement live)."
        ),
    )

    # --- Monitoring ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    pnl_snapshot_interval_seconds: int = Field(
        300,
        ge=1,
        description="Période entre 2 snapshots PnL (default 5 min).",
    )
    alert_large_order_usd_threshold: float = Field(
        50.0,
        ge=0,
        description="Seuil USD au-dessus duquel un fill déclenche `order_filled_large`.",
    )
    alert_cooldown_seconds: int = Field(
        60,
        ge=0,
        description="Anti-spam par event_type (in-memory, reset au boot).",
    )

    # --- Monitoring M7 : Telegram enrichi (opt-in sauf startup) ----------
    telegram_startup_message: bool = Field(
        True,
        description=(
            "Envoie un message de démarrage au boot (version, modules, dashboard). "
            "ON par défaut — no-op si TELEGRAM_BOT_TOKEN absent."
        ),
    )
    telegram_heartbeat_enabled: bool = Field(
        False,
        description=(
            "Opt-in M7. Si true, envoie un heartbeat toutes les "
            "TELEGRAM_HEARTBEAT_INTERVAL_HOURS heures pour détecter une panne."
        ),
    )
    telegram_heartbeat_interval_hours: int = Field(
        12,
        ge=1,
        le=168,
        description="Intervalle entre 2 heartbeats (1 h à 7 jours).",
    )
    telegram_daily_summary: bool = Field(
        False,
        description="Opt-in M7. Envoie un résumé quotidien à l'heure configurée.",
    )
    tg_daily_summary_hour: int = Field(
        9,
        ge=0,
        le=23,
        description="Heure locale [0, 23] d'envoi du résumé quotidien.",
    )
    tg_daily_summary_timezone: str = Field(
        "Europe/Paris",
        description=(
            "Nom IANA de la TZ du résumé quotidien. Validé via zoneinfo.ZoneInfo "
            "au boot. Nécessite le package système `tzdata` sur Linux minimal."
        ),
    )
    telegram_digest_threshold: int = Field(
        5,
        ge=2,
        le=100,
        description=(
            "Nombre d'alertes du même event_type dans la fenêtre pour activer "
            "le digest mode (batch en 1 seul message)."
        ),
    )
    telegram_digest_window_minutes: int = Field(
        60,
        ge=5,
        le=1440,
        description="Fenêtre glissante (minutes) pour le compteur de digest.",
    )

    # --- Logs ---
    log_level: str = "INFO"

    # --- CLI / Logs M9 (silent CLI + rotation fichier) -------------------
    cli_silent: bool = Field(
        True,
        description=(
            "BREAKING M8→M9. Si true, le terminal affiche un écran rich "
            "statique au boot ; les logs JSON ne sont PAS streamés sur stdout. "
            "Flag --verbose ou CLI_SILENT=false restaure le comportement M1..M8."
        ),
    )
    log_file: Path = Field(
        Path("~/.polycopy/logs/polycopy.log"),
        description=(
            "Chemin du fichier log rotatif (toujours écrit, même en --verbose). "
            "Expanded via Path.expanduser(). Permissions 0o600 + parent 0o700."
        ),
    )
    log_file_max_bytes: int = Field(
        10_485_760,
        ge=1_048_576,
        description="Taille max d'un fichier log avant rotation (default 10 MB).",
    )
    log_file_backup_count: int = Field(
        10,
        ge=1,
        le=100,
        description="Nb de fichiers rotatifs conservés (default 10).",
    )

    @field_validator("log_file", mode="before")
    @classmethod
    def _expand_log_file(cls, v: object) -> object:
        """Expanduser sur le chemin log_file (~ → /home/<user>) au boot."""
        if isinstance(v, str):
            return Path(v).expanduser()
        if isinstance(v, Path):
            return v.expanduser()
        return v

    # --- Dashboard (M4.5, optionnel) ---
    dashboard_enabled: bool = Field(
        False,
        description=(
            "Opt-in strict. Si false, __main__ n'instancie pas le DashboardOrchestrator "
            "(zéro overhead, zéro port ouvert)."
        ),
    )
    dashboard_host: str = Field(
        "127.0.0.1",
        description=(
            "Bind explicite localhost-only. Changer à ses risques — "
            "DASHBOARD_HOST=0.0.0.0 expose le dashboard sur toutes les interfaces."
        ),
    )
    dashboard_port: int = Field(
        8787,
        ge=1,
        le=65535,
        description="Port TCP local du dashboard.",
    )

    # --- Dashboard M6 (UX cosmétique, opt-in) ----------------------------
    dashboard_theme: Literal["dark", "light"] = Field(
        "dark",
        description=(
            "Thème initial du dashboard. Toggle front-end persiste via localStorage "
            "(clé 'polycopy.theme'). UI cosmétique — aucun impact sécurité."
        ),
    )
    dashboard_poll_interval_seconds: int = Field(
        5,
        ge=2,
        le=60,
        description=(
            "Cadence du polling HTMX des partials Home/listes (s). "
            "UI cosmétique — gain log de fond vs M4.5 (3s → 5s)."
        ),
    )

    # --- Dashboard M9 : onglet /logs (lecture fichier) -------------------
    dashboard_logs_enabled: bool = Field(
        True,
        description=(
            "Active l'onglet /logs (lecture du fichier LOG_FILE, filtres + "
            "live tail HTMX 2s). Si false, /logs renvoie un stub config-disabled."
        ),
    )
    dashboard_logs_tail_lines: int = Field(
        500,
        ge=50,
        le=5000,
        description=(
            "Nb max de lignes affichées dans /logs (anti-RAM browser). "
            "Au-delà → bouton Télécharger .log."
        ),
    )

    # --- Discovery (M5, optionnel, opt-in strict) ------------------------
    discovery_enabled: bool = Field(
        False,
        description=(
            "Opt-in strict M5. Si false, __main__ n'instancie pas le "
            "DiscoveryOrchestrator (zéro overhead, zéro appel API supplémentaire)."
        ),
    )
    discovery_interval_seconds: int = Field(
        21600,
        ge=3600,
        le=604800,
        description=(
            "Cadence d'un cycle complet de scoring. Default 6h. "
            "Borne [3600, 604800] (1h–7j) via validator Pydantic."
        ),
    )
    discovery_candidate_pool_size: int = Field(
        100,
        ge=1,
        le=5000,
        description="Wallets candidats scannés par cycle (budget API ~2 calls/wallet).",
    )
    discovery_top_markets_for_holders: int = Field(
        20,
        ge=1,
        le=100,
        description="Nb de marchés Gamma top-liquidité scannés via /holders en bootstrap.",
    )
    discovery_global_trades_lookback_hours: int = Field(
        24,
        ge=1,
        le=168,
        description=(
            "Fenêtre du feed /trades global (informatif — l'API retourne les "
            "500 trades récents quel que soit ce paramètre)."
        ),
    )
    max_active_traders: int = Field(
        10,
        ge=1,
        le=100,
        description=(
            "Plafond dur sur les target_traders.status='active'. Si dépassement, "
            "M5 refuse + alerte. Jamais de retrait arbitraire."
        ),
    )
    blacklisted_wallets: Annotated[list[str], NoDecode] = Field(default_factory=list)
    scoring_version: str = Field(
        "v1",
        description=(
            "Version de la formule de scoring. Loggée + écrite avec chaque score "
            "pour reproductibilité (cf. §7.6 spec)."
        ),
    )
    scoring_min_closed_markets: int = Field(
        10,
        ge=0,
        description=(
            "Seuil cold start : un wallet avec < N marchés résolus est scoré 0 "
            "et flaggé low_confidence."
        ),
    )
    scoring_lookback_days: int = Field(
        90,
        ge=1,
        le=3650,
        description="Fenêtre glissante de PnL/volume retenue pour le scoring.",
    )
    scoring_promotion_threshold: float = Field(
        0.65,
        ge=0.0,
        le=1.0,
        description="Score ≥ seuil → candidat à promotion (shadow → active).",
    )
    scoring_demotion_threshold: float = Field(
        0.40,
        ge=0.0,
        le=1.0,
        description="Score < seuil pendant K cycles → demote (active → paused).",
    )
    scoring_demotion_hysteresis_cycles: int = Field(
        3,
        ge=1,
        le=100,
        description="K cycles consécutifs sous le seuil avant demote (anti-whipsaw).",
    )
    trader_shadow_days: int = Field(
        7,
        ge=0,
        le=90,
        description=(
            "Jours d'observation 'shadow' avant qu'un wallet auto-promu devienne "
            "'active'. 0 = bypass shadow (uniquement si DISCOVERY_SHADOW_BYPASS=true)."
        ),
    )
    discovery_shadow_bypass: bool = Field(
        False,
        description=(
            "Si true ET TRADER_SHADOW_DAYS=0, autorise l'auto-promote immédiat "
            "(shadow → active sans observation). ⚠️ Log WARNING au boot."
        ),
    )
    discovery_backend: Literal["data_api", "goldsky", "hybrid"] = Field(
        "data_api",
        description=(
            "Backend de découverte. 'data_api' (default, zéro dep) / 'goldsky' / "
            "'hybrid' (ranking Goldsky + enrichissement Data API)."
        ),
    )
    goldsky_positions_subgraph_url: str = Field(
        # ⚠️ Par défaut pointe vers pnl-subgraph (qui expose realizedPnl via
        # UserPosition). Le subgraph "positions-subgraph/0.0.7" historiquement
        # mentionné dans la spec ne contient PAS d'entité Position avec PnL —
        # divergence §14.5 #3 confirmée par introspection 2026-04-18.
        "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/pnl-subgraph/0.0.14/gn",
        description=(
            "URL du subgraph Goldsky hébergeant UserPosition{realizedPnl}. "
            "Override possible si la version drift."
        ),
    )
    goldsky_pnl_subgraph_url: str = Field(
        "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/pnl-subgraph/0.0.14/gn",
        description="URL du subgraph pnl (miroir par défaut de positions_url, optionnel).",
    )

    @field_validator("blacklisted_wallets", mode="before")
    @classmethod
    def _parse_blacklisted_wallets(cls, v: object) -> object:
        """Accepte `BLACKLISTED_WALLETS` en CSV ou JSON array (même logique que TARGET_WALLETS)."""
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip().lower() for item in stripped.split(",") if item.strip()]
        if isinstance(v, list):
            return [str(item).lower() for item in v]
        return v

    @field_validator("tg_daily_summary_timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        """Fail-fast si la TZ IANA n'existe pas (ex: `tzdata` manquant sur WSL minimal)."""
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"TG_DAILY_SUMMARY_TIMEZONE={v!r} introuvable — "
                "installer `tzdata` ou vérifier le nom IANA.",
            ) from exc
        return v

    @model_validator(mode="after")
    def _validate_discovery_thresholds(self) -> "Settings":
        """Cross-field : demotion_threshold doit être strictement < promotion_threshold.

        Empêche l'état incohérent `demote=0.70, promote=0.65` où chaque score
        déclenche à la fois promotion ET demote. Raise `ValueError` au boot.
        """
        if self.scoring_demotion_threshold >= self.scoring_promotion_threshold:
            raise ValueError(
                "SCORING_DEMOTION_THRESHOLD "
                f"({self.scoring_demotion_threshold}) must be strictly less than "
                f"SCORING_PROMOTION_THRESHOLD ({self.scoring_promotion_threshold}).",
            )
        return self


settings = Settings()
