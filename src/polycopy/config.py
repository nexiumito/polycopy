"""Configuration centralisée via Pydantic Settings.

Toutes les variables sont chargées depuis l'environnement (ou .env en dev).
Aucune valeur sensible en dur dans le code.
"""

import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Flag module-level positionné par le validator `_migrate_legacy_dry_run` quand
# une env var legacy `DRY_RUN` a été utilisée pour dériver `execution_mode`.
# Le warning de deprecation est émis au boot par `cli/runner.py` après la
# configuration de la chaîne structlog (cf. spec M10 §3.2.2 + §6.13).
_LEGACY_DRY_RUN_DETECTED: bool = False

# Flag module-level positionné par `_resolve_machine_id` indiquant si
# `MACHINE_ID` a été lu depuis l'env var (`"env"`) ou dérivé du hostname
# (`"hostname"`). Lu par `cli/runner.py` pour émettre un log unique au boot
# après `configure_logging` (cf. spec M12_bis §3.1).
_MACHINE_ID_SOURCE: str = "env"


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

    # --- Watcher live-reload (M5_ter) ------------------------------------
    watcher_reload_interval_seconds: int = Field(
        300,
        ge=30,
        le=3600,
        description=(
            "M5_ter : TTL du cycle de reload du WatcherOrchestrator. À chaque "
            "tick, re-fetch `list_wallets_to_poll()` et diff set-based contre "
            "les pollers en cours (`tg.create_task` pour les nouveaux, "
            "`task.cancel()` pour les retirés). Réactif aux mutations M5 "
            "(promote/demote) et M5_bis (eviction cascade, sell_only wind-down, "
            "blacklist reconcile) sans restart. Range [30, 3600]."
        ),
    )

    # --- Multi-machine identity (M12_bis) --------------------------------
    machine_id: str | None = Field(
        None,
        description=(
            "Badge texte identifiant la machine dans les messages Telegram "
            "(ex. 'PC-FIXE', 'MACBOOK'). Fallback socket.gethostname() si "
            "absent/vide. Normalisé upper + regex ^[A-Z0-9_-]+$ (chars hors "
            "jeu → '-'), cap 32 chars, 'UNKNOWN' si tout invalide. Public, "
            "non-sensible, loggé en clair."
        ),
    )
    machine_emoji: str = Field(
        "🖥️",
        max_length=8,
        description=(
            "Emoji affiché devant MACHINE_ID dans les messages Telegram "
            "(ex. 💻 MacBook, 🏫 université). Cosmétique, max 8 chars pour "
            "accommoder les séquences ZWJ/variation selectors."
        ),
    )
    tailnet_name: str | None = Field(
        None,
        description=(
            "Override pour la résolution du tailnet Tailscale (default : auto "
            "via `tailscale status --json` → CurrentTailnet.MagicDNSSuffix). "
            "Format strict `<nom>.ts.net`, lowercase forcé. Utile si MagicDNS "
            "désactivé côté tailnet OU pour tests intégration. Consommé par "
            "`compute_dashboard_url` (M12_bis Phase G) pour générer les liens "
            "Telegram cliquables vers le dashboard multi-machine. Public, "
            "non-sensible — loggé en clair."
        ),
    )

    @field_validator("tailnet_name", mode="before")
    @classmethod
    def _normalize_tailnet_name(cls, v: object) -> object:
        """Normalise vide → None pour cohérence avec env var non-settée."""
        if v is None:
            return None
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return None
            return stripped.lower()
        return v

    @field_validator("tailnet_name")
    @classmethod
    def _validate_tailnet_name(cls, v: str | None) -> str | None:
        """Refuse tout ce qui n'est pas ``<nom>.ts.net`` (M12_bis Phase G)."""
        if v is None:
            return None
        if not re.fullmatch(r"[a-z0-9-]+\.ts\.net", v):
            raise ValueError(
                f"TAILNET_NAME={v!r} format invalide. Attendu `<nom>.ts.net` "
                "(lowercase, chars ``[a-z0-9-]`` uniquement). "
                "Exemple valable : `taila157fd.ts.net` ou `alpha-beta.ts.net`.",
            )
        return v

    # --- Remote Control (M12_bis Phase B+, opt-in strict) ----------------
    remote_control_enabled: bool = Field(
        False,
        description=(
            "Opt-in strict M12_bis. Si false (default), le package "
            "`remote_control` n'est pas instancié (zéro overhead, zéro port "
            "ouvert). Requiert `REMOTE_CONTROL_TOTP_SECRET` (Phase C) pour "
            "démarrer si true. Cf. spec §2."
        ),
    )
    remote_control_port: int = Field(
        8765,
        ge=1024,
        le=65535,
        description=(
            "Port TCP bindé sur l'IP Tailscale uniquement (jamais 0.0.0.0 ni "
            "127.0.0.1). 8765 par défaut — pas 8000 (conflit dashboard) ni "
            "8080 (conflit commun dev)."
        ),
    )
    remote_control_tailscale_ip_override: str | None = Field(
        None,
        description=(
            "Bypass `tailscale ip -4` : force une IP de bind spécifique "
            "(tests intégration, edge cases NAT exotiques). Doit être une "
            "IPv4 non-loopback et non-unspecified. Crash boot sinon."
        ),
    )
    remote_control_totp_secret: str | None = Field(
        None,
        description=(
            "Secret TOTP RFC 6238 en base32 (≥16 chars). Requis si "
            "REMOTE_CONTROL_ENABLED=true (crash boot sinon). Générer via "
            '`python -c "import pyotp; print(pyotp.random_base32())"`. '
            "⚠️ Discipline secret identique TELEGRAM_BOT_TOKEN : jamais "
            "loggé ni committé. Rotation trimestrielle."
        ),
    )
    remote_control_sentinel_path: str = Field(
        "~/.polycopy/halt.flag",
        description=(
            "Chemin du sentinel file M12_bis (§5.2). Touché par "
            "AutoLockdown + PnlSnapshotWriter (Phase D) + `/stop`. "
            "Expansion `~` → home dir utilisateur au boot. Permissions "
            "strict 0o600 (fichier) + 0o700 (parent). Override utile "
            "pour tests intégration."
        ),
    )

    @field_validator("remote_control_totp_secret")
    @classmethod
    def _validate_remote_control_totp_secret(cls, v: str | None) -> str | None:
        """Refuse tout ce qui n'est pas base32 ≥16 chars (M12_bis §4.4.3)."""
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if len(stripped) < 16:
            raise ValueError(
                "REMOTE_CONTROL_TOTP_SECRET doit faire ≥16 caractères "
                '(base32). Régénérer via `python -c "import pyotp; '
                'print(pyotp.random_base32())"`.',
            )
        if not re.fullmatch(r"[A-Z2-7]+=*", stripped):
            raise ValueError(
                "REMOTE_CONTROL_TOTP_SECRET doit être en base32 valide "
                "(caractères A-Z et 2-7 uniquement, padding `=` optionnel).",
            )
        return stripped

    @field_validator("remote_control_tailscale_ip_override")
    @classmethod
    def _validate_remote_control_ip_override(cls, v: str | None) -> str | None:
        """Refuse 127.x.x.x, 0.0.0.0, ::1, et toute string non-IPv4 (M12_bis §4.4)."""
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        try:
            ip = ipaddress.ip_address(stripped)
        except ValueError as exc:
            raise ValueError(
                f"REMOTE_CONTROL_TAILSCALE_IP_OVERRIDE={v!r} n'est pas une IP valide.",
            ) from exc
        if not isinstance(ip, ipaddress.IPv4Address):
            raise ValueError(
                f"REMOTE_CONTROL_TAILSCALE_IP_OVERRIDE={v!r} doit être IPv4 "
                "(IPv6 non supporté — Tailscale MagicDNS résout en IPv4).",
            )
        if ip.is_loopback:
            raise ValueError(
                f"REMOTE_CONTROL_TAILSCALE_IP_OVERRIDE={v!r} interdit : "
                "bind loopback viderait la garantie Tailscale-only.",
            )
        if ip.is_unspecified:
            raise ValueError(
                f"REMOTE_CONTROL_TAILSCALE_IP_OVERRIDE={v!r} interdit : "
                "0.0.0.0 exposerait le port sur toutes les interfaces.",
            )
        return stripped

    # --- Storage ---
    database_url: str = "sqlite+aiosqlite:///polycopy.db"

    # --- Mode (M10) ---
    execution_mode: Literal["simulation", "dry_run", "live"] = Field(
        "dry_run",
        description=(
            "Mode d'exécution M10. "
            "'simulation' = backtest offline (fixtures, pas de réseau). "
            "'dry_run' = pipeline complet online, simulation fill (stub M3 ou "
            "realistic M8 selon DRY_RUN_REALISTIC_FILL), alertes + kill switch "
            "identiques à LIVE (badge visuel différent). "
            "'live' = exécution réelle CLOB. Backward-compat : legacy DRY_RUN=true "
            "ou DRY_RUN=false est lu avec warning de deprecation (1 version)."
        ),
    )
    # M10 : ghost field pour capter ``DRY_RUN`` env var legacy.
    # Consommé + retiré par ``_migrate_legacy_dry_run`` avant validation finale.
    # Disparaît à version+2 (cf. spec §11.1).
    dry_run_env_legacy: bool | None = Field(
        None,
        alias="DRY_RUN",
        validation_alias="DRY_RUN",
        exclude=True,
        description="[legacy M10] Lu une dernière version avec warning, puis supprimé.",
    )

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
    dashboard_bind_tailscale: bool = Field(
        False,
        description=(
            "M12_bis §4.7 : opt-in strict. Si true, le dashboard bind "
            "sur l'IP Tailscale (résolue via `tailscale ip -4`, même "
            "logique que remote_control §4.4.1) au lieu de "
            "DASHBOARD_HOST. Crash boot si Tailscale absent. Cohabite "
            "avec DASHBOARD_HOST : warning si les deux sont settés, "
            "priorité au binding Tailscale. Invariant M4.5/M6 "
            "préservé : aucun secret, routes GET only."
        ),
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

    # --- Dashboard M10 : hygiène des logs (filter noisy endpoints) -------
    dashboard_log_skip_paths: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("dashboard_log_skip_paths", mode="before")
    @classmethod
    def _parse_skip_paths(cls, v: object) -> object:
        """Accepte CSV (``^/a$,^/b$``) ou JSON array (``["^/a$","^/b$"]``).

        Additif aux defaults hardcodés dans ``cli/logging_config.py``
        (``^/api/health-external$``, ``^/partials/.*$``, ``^/api/version$``).
        """
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v

    # --- Pipeline temps réel (M11, opt-in par défaut) --------------------
    strategy_clob_ws_enabled: bool = Field(
        True,
        description=(
            "M11 : active le client WebSocket CLOB `market` channel dans "
            "SlippageChecker. Si false → fallback HTTP /midpoint strict "
            "(comportement M2..M10). Read-only public, aucune creds."
        ),
    )
    strategy_clob_ws_url: str = Field(
        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        description="URL du WebSocket CLOB market (override test/staging).",
    )
    strategy_clob_ws_max_subscribed: int = Field(
        500,
        ge=50,
        le=5000,
        description=(
            "Cap dur nombre de tokens subscribés simultanément (anti-leak "
            "mémoire). Au-delà, LRU unsub le plus ancien."
        ),
    )
    strategy_clob_ws_inactivity_unsub_seconds: int = Field(
        300,
        ge=60,
        le=3600,
        description=(
            "Unsub auto après N secondes d'inactivité sur un token (GC mémoire, cf. §3.4 spec M11)."
        ),
    )
    strategy_clob_ws_health_check_seconds: int = Field(
        30,
        ge=5,
        le=300,
        description=(
            "Période du watchdog : si aucun message reçu depuis 2x cette "
            "valeur, statut → `down` et reconnect forcé."
        ),
    )
    strategy_gamma_adaptive_cache_enabled: bool = Field(
        True,
        description=(
            "M11 : active le cache Gamma à TTL adaptatif par segment (résolu "
            "/ proche résolution / actif / inactif). Si false → TTL 60 s "
            "uniforme M2."
        ),
    )
    latency_sample_retention_days: int = Field(
        7,
        ge=1,
        le=90,
        description=(
            "M11 : rétention des rows `trade_latency_samples`. Purge au boot "
            "+ quotidien via LatencyPurgeScheduler."
        ),
    )
    latency_instrumentation_enabled: bool = Field(
        True,
        description=(
            "M11 : active l'instrumentation latence globale (6 stages + "
            "insert DB). Si false → pipeline tourne sans latence loggée "
            "(secours si surcharge CPU)."
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
    # WASH_CLUSTER_WALLETS (M12) : liste manuelle d'exclusion wash cluster.
    # Discipline identique BLACKLISTED_WALLETS : CSV ou JSON array, lowercase.
    # Auto-detection wash cluster reportée M17+ (§14.6 spec M12).
    wash_cluster_wallets: Annotated[list[str], NoDecode] = Field(default_factory=list)
    scoring_version: Literal["v1", "v2"] = Field(
        "v1",
        description=(
            "Version de la formule de scoring. Loggée + écrite avec chaque score "
            "pour reproductibilité (cf. §7.6 spec). M12 : promu à Literal pour "
            "rejet boot des valeurs invalides."
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

    # --- Equity curve quotidienne (M12, prérequis scoring v2) -----------------
    # Consommé par `TraderDailyPnlWriter` co-lancé dans `DiscoveryOrchestrator`
    # TaskGroup. Source unique de l'equity curve pour Sortino/Calmar/consistency.
    trader_daily_pnl_enabled: bool = Field(
        True,
        description=(
            "Active le scheduler TraderDailyPnlWriter (snapshot equity curve "
            "quotidien). Si false, scoring v2 tourne en mode dégradé (Sortino=0 "
            "sans curve)."
        ),
    )
    trader_daily_pnl_interval_seconds: int = Field(
        86400,
        ge=3600,
        le=604800,
        description=(
            "Cadence du snapshot equity curve. 24h par défaut. Borne min 1h, borne max 7 jours."
        ),
    )

    # --- M5_bis — compétition adaptative entre wallets (eviction) -------------
    # Opt-in strict. Off par défaut = zéro diff lifecycle M5 (cf. spec §13).
    eviction_enabled: bool = Field(
        False,
        description=(
            "Active la compétition adaptative M5_bis : un shadow/sell_only "
            "significativement meilleur qu'un active peut l'évincer (cascade "
            "active → sell_only). Off = lifecycle M5 strict."
        ),
    )
    eviction_score_margin: float = Field(
        0.15,
        ge=0.05,
        le=0.50,
        description=(
            "Delta minimum score(candidat) - score(worst_active) requis pour "
            "déclencher une eviction. Applique aussi à l'abort (T6) et au "
            "rebond (T7) — même valeur pour les 3 directions."
        ),
    )
    eviction_hysteresis_cycles: int = Field(
        3,
        ge=1,
        le=10,
        description=(
            "Cycles consécutifs où la condition d'eviction/abort/rebond doit "
            "tenir avant déclenchement (anti-whipsaw, cf. spec §4.3)."
        ),
    )
    max_sell_only_wallets: int = Field(
        10,
        ge=1,
        le=100,
        description=(
            "Cap dur sur le pool sell_only. Évite la cascade pathologique si "
            "les scores sont très volatils. Par défaut égal à MAX_ACTIVE_TRADERS "
            "(le validator cross-field aligne les deux si non set explicitement)."
        ),
    )

    # --- Scoring v2 — shadow period + backtest + cutover (M12 §6.1) ----------
    # Tant que SCORING_VERSION=v1 (default), v2 ne pilote rien. SHADOW_DAYS>0
    # active le dual-compute en parallèle (observation) — seule v1 reste
    # autoritaire pour `DecisionEngine`. Cutover manuel après backtest OK.
    scoring_v2_shadow_days: int = Field(
        14,
        ge=0,
        le=90,
        description=(
            "Durée de coexistence v1/v2 en shadow. Pendant la fenêtre, v2 "
            "calcule + écrit trader_scores (scoring_version='v2') mais NE "
            "PILOTE PAS DecisionEngine. 0 = pas de calcul parallèle."
        ),
    )
    scoring_v2_window_days: int = Field(
        90,
        ge=30,
        le=365,
        description=(
            "Fenêtre temporelle des facteurs v2 (Sortino/Calmar/Brier/"
            "timing_alpha). 90j par défaut. Weighting uniforme v1 (half-life "
            "exponentiel reportable v2.1, cf. §14.1 spec)."
        ),
    )
    scoring_v2_cold_start_mode: bool = Field(
        False,
        description=(
            "Si true, relâche les gates `trade_count_90d` (≥ 20 au lieu de 50) "
            "et `days_active` (≥ 7 au lieu de 30). WARNING loggé au boot. "
            "Reportable v1.1 si pool trop restreint."
        ),
    )
    scoring_v2_backtest_label_file: str = Field(
        "assets/scoring_v2_labels.csv",
        description=(
            "Chemin du set labelé smart_money/random utilisé par "
            "scripts/backtest_scoring_v2.py avant décision cutover."
        ),
    )
    scoring_v2_cutover_ready: bool = Field(
        False,
        description=(
            "Active le bouton dashboard 'Validate v2 & flip'. User-controlled "
            "— doit être set à true uniquement après rapport backtest validé "
            "(brier_v2 < brier_v1 - 0.01)."
        ),
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

    @field_validator("wash_cluster_wallets", mode="before")
    @classmethod
    def _parse_wash_cluster_wallets(cls, v: object) -> object:
        """M12 : même discipline que BLACKLISTED_WALLETS (CSV ou JSON, lowercase).

        ⚠️ Contrairement à ``_parse_blacklisted_wallets`` M5 qui ne lowercase
        pas les entrées JSON (bug historique préservé pour backward-compat),
        M12 normalise **toujours** en lowercase — cohérent avec le rôle sécurité
        absolue du gate.
        """
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(item).lower() for item in parsed]
                return parsed
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_dry_run(cls, data: Any) -> Any:
        """Backward-compat M10 : traduit legacy ``DRY_RUN`` en ``execution_mode``.

        - ``DRY_RUN=true/1/yes/on`` → ``execution_mode="dry_run"``.
        - ``DRY_RUN=false/0/no/off`` → ``execution_mode="live"``.
        - ``EXECUTION_MODE`` explicite gagne sur legacy sans warning.

        Le warning ``config_deprecation_dry_run_env`` est émis au boot par
        ``cli/runner.py`` (pas ici : on veut la chaîne structlog M9 configurée
        avant d'émettre sur stderr).
        """
        global _LEGACY_DRY_RUN_DETECTED
        if not isinstance(data, dict):
            return data
        # Sources possibles pour la valeur legacy (ordre : kwarg test direct,
        # alias env var, variante majuscule). ``dry_run`` kwarg est encore
        # supporté dans les tests pour faciliter la migration.
        raw_legacy = data.get("dry_run_env_legacy")
        if raw_legacy is None:
            raw_legacy = data.get("dry_run")
        if raw_legacy is None:
            raw_legacy = data.get("DRY_RUN")
        explicit_mode = data.get("execution_mode")
        if explicit_mode is None:
            explicit_mode = data.get("EXECUTION_MODE")
        if raw_legacy is not None and explicit_mode is None:
            if str(raw_legacy).strip().lower() in {"true", "1", "yes", "on"}:
                data["execution_mode"] = "dry_run"
            else:
                data["execution_mode"] = "live"
            _LEGACY_DRY_RUN_DETECTED = True
        # On retire les alias legacy avant la validation finale pour ne pas
        # polluer le schéma ``Settings`` — le ghost field ``dry_run_env_legacy``
        # reste exposé mais jamais lu par le code métier.
        data.pop("dry_run", None)
        data.pop("DRY_RUN", None)
        return data

    @model_validator(mode="after")
    def _resolve_machine_id(self) -> "Settings":
        """Résout ``MACHINE_ID`` (env ou hostname) + normalise (M12_bis §3.1).

        - ``MACHINE_ID`` absent/vide/whitespace → fallback ``socket.gethostname()``.
        - Normalisation : ``strip().upper()`` → tout caractère hors
          ``[A-Z0-9_-]`` remplacé par ``-`` → cap 32 chars.
        - Si la normalisation produit une string vide (entrée ``"@@@"`` par
          ex.), on retombe sur ``"UNKNOWN"`` pour ne jamais avoir un badge
          vide dans les alertes Telegram.

        Le flag ``_MACHINE_ID_SOURCE`` est positionné pour que ``cli/runner.py``
        puisse logger la source (env vs hostname) une fois structlog configuré.
        """
        global _MACHINE_ID_SOURCE
        raw = self.machine_id
        source = "env"
        if raw is None or not raw.strip():
            raw = socket.gethostname()
            source = "hostname"
        normalized = re.sub(r"[^A-Z0-9_-]", "-", raw.strip().upper())[:32]
        if not normalized or not re.search(r"[A-Z0-9]", normalized):
            normalized = "UNKNOWN"
        object.__setattr__(self, "machine_id", normalized)
        _MACHINE_ID_SOURCE = source
        return self

    @model_validator(mode="after")
    def _validate_remote_control_requires_totp(self) -> "Settings":
        """Cross-field : REMOTE_CONTROL_ENABLED=true ⇒ TOTP_SECRET requis (§4.4.3)."""
        if self.remote_control_enabled and not self.remote_control_totp_secret:
            raise ValueError(
                "REMOTE_CONTROL_ENABLED=true requires REMOTE_CONTROL_TOTP_SECRET. "
                'Générer via : python -c "import pyotp; print(pyotp.random_base32())" '
                "puis coller la valeur dans `.env`.",
            )
        return self

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

    @model_validator(mode="after")
    def _validate_m5_bis_eviction(self) -> "Settings":
        """Cross-field M5_bis : TARGET_WALLETS ∩ BLACKLISTED_WALLETS impossible.

        Si l'utilisateur ajoute un wallet à la fois dans ``TARGET_WALLETS`` et
        ``BLACKLISTED_WALLETS``, l'intention est ambiguë (whitelist vs
        exclusion). Crash boot clair plutôt que comportement surprise.

        N'exécute la vérification que si ``EVICTION_ENABLED=true`` — en off,
        ``BLACKLISTED_WALLETS`` garde sa sémantique M5 (skip silent) et le
        conflit reste cosmétique.
        """
        if not self.eviction_enabled:
            return self
        target_lc = {w.lower() for w in self.target_wallets}
        blacklist_lc = {w.lower() for w in self.blacklisted_wallets}
        overlap = sorted(target_lc & blacklist_lc)
        if overlap:
            raise ValueError(
                "Conflict: wallets "
                f"{overlap} are in both TARGET_WALLETS and BLACKLISTED_WALLETS. "
                "Pick one (EVICTION_ENABLED=true forbids this overlap).",
            )
        return self

    @property
    def dry_run(self) -> bool:
        """Proxy deprecation-only — dérive depuis ``execution_mode``.

        Retourne True si ``execution_mode in {"simulation", "dry_run"}``.
        Backward-compat M10 : disparaît à version+2. Le code qui a besoin de
        distinguer SIMULATION de DRY_RUN doit lire ``self.execution_mode``.
        """
        return self.execution_mode in {"simulation", "dry_run"}


def legacy_dry_run_detected() -> bool:
    """Retourne True si le validator a traduit un ``DRY_RUN`` legacy.

    Flag lu par ``cli/runner.py`` pour émettre le warning de deprecation
    une fois la chaîne structlog configurée.
    """
    return _LEGACY_DRY_RUN_DETECTED


def machine_id_source() -> str:
    """Retourne ``"env"`` ou ``"hostname"`` selon la source de ``MACHINE_ID``.

    Lu par ``cli/runner.py`` pour émettre le log ``machine_id_resolved``
    une fois la chaîne structlog configurée (cf. spec M12_bis §3.1).
    """
    return _MACHINE_ID_SOURCE


settings = Settings()
