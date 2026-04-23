# CLAUDE.md

Contexte projet pour Claude Code. Lis ceci avant toute modification.

## Vue d'ensemble

`polycopy` est un bot de copy trading pour Polymarket en Python 3.11+. Architecture en 5 couches asynchrones (asyncio) faiblement couplées.

Voir `README.md` pour le pitch utilisateur, `docs/architecture.md` pour le détail technique.

## Environnement de dev

Environnement de référence : **WSL Ubuntu (bash)**, chemin canonique `/home/<user>/code/polycopy` (Linux natif). Éviter `/mnt/c/...` : I/O `drvfs` lent sur venv et pytest. Toutes les commandes de cette doc supposent un shell bash WSL.

Bootstrap complet et idempotent : `bash scripts/setup.sh` depuis la racine. Crée `.venv/`, installe les deps, copie `.env`, applique le patch §0.5 de la spec M1, lance un smoke test. Pas-à-pas utilisateur : [docs/setup.md](docs/setup.md).

## Conventions de code

- **Python 3.11+**, type hints partout (vérifié par `mypy --strict`)
- **Async par défaut** : tout I/O passe par `asyncio` + `httpx.AsyncClient` ou `websockets`
- **Pydantic v2** pour tous les DTOs, modèles de config et validation API
- **SQLAlchemy 2.0** style (async, `select()` pas de `Query`)
- **Naming** :
  - Modules et fichiers : `snake_case`
  - Classes : `PascalCase`
  - Constantes : `UPPER_SNAKE_CASE`
- **Pas d'abréviations cryptiques** : `target_wallet_address` pas `tw_addr`
- **Docstrings** en français (cohérent avec mes notes), code et identifiants en anglais
- **Logs structurés** via `structlog`, jamais de `print()` en dehors des scripts CLI (et de `src/polycopy/cli/` qui utilise `rich.console.Console.print` pour l'écran statique)
- **Modes d'exécution (M10+)** : `EXECUTION_MODE: "simulation" | "dry_run" | "live"` remplace `DRY_RUN: bool`. Ancien flag lu en fallback avec warning de deprecation 1 version. 3 modes testés séparément ; le dry-run est un **miroir fidèle** du live côté alertes/kill switch/logs — seule la signature CLOB (POST ordre réel) diffère. SIMULATION = backtest offline, pas de réseau, fixtures locales, `stop_event` local au run.
- **Instrumentation latence (M11+)** : `structlog.contextvars.bind_contextvars(trade_id=...)` en tête de pipeline (généré par `WalletPoller`). 6 stages mesurés : `watcher_detected_ms`, `strategy_enriched_ms`, `strategy_filtered_ms`, `strategy_sized_ms`, `strategy_risk_checked_ms`, `executor_submitted_ms`. Précision `time.perf_counter_ns`. Persistance append-only dans `trade_latency_samples` (purge 7 jours, scheduler quotidien + query boot). Dashboard `/latency` rend les p50/p95/p99 par stage avec filtre `?since=`. Feature flag `LATENCY_INSTRUMENTATION_ENABLED=false` désactive si surcharge CPU mesurée.
- **Scoring versionné (M12+)** : formule `Score_v2` vit dans un sous-package isolé `src/polycopy/discovery/scoring/v2/` avec **pure functions** par facteur (6 facteurs : `risk_adjusted` Sortino+Calmar, `calibration` Brier-skill, `timing_alpha` pair→wallet, `specialization` HHI catégories Gamma, `consistency` fraction mois PnL>0, `discipline` (1-zombie)×sizing_stability). Chaque facteur testable isolément (I/O séparé via `MetricsCollectorV2`). Winsorisation p5-p95 pool-wide + normalisation 0-1 par `normalization.py`. Registry `SCORING_VERSIONS_REGISTRY: dict[Literal["v1","v2"], Callable]` dans `scoring/__init__.py`. Pondération figée en code (`0.25/0.20/0.20/0.15/0.10/0.10`, somme = 1.0) — changer une pondération = bumper `SCORING_VERSION` (ex: `"v2.1"`) et **jamais** rewrite rétroactif des rows `trader_scores` historiques (audit trail sacré). 6 gates durs pré-scoring v2 dans `gates.py` (fail-fast, env→DTO lookups optimisés). Feature flag `SCORING_VERSION=v1` par défaut, `SCORING_V2_SHADOW_DAYS=14` pour coexistence v1/v2 avant cutover manuel. `TraderDailyPnl` table append-only peuplée par `TraderDailyPnlWriter` (scheduler 24h co-lancé dans `DiscoveryOrchestrator` TaskGroup) — source Sortino/Calmar.
- **Competitive eviction (M5_bis)** : package isolé `src/polycopy/discovery/eviction/` (CascadePlanner + EvictionStateMachine purs, HysteresisTracker in-memory, EvictionScheduler qui orchestre). Opt-in strict via `EVICTION_ENABLED=false` — flag off = lifecycle M5 identique (zéro transition `sell_only`, zéro cascade). Nouveaux status : `sell_only` (wind-down réversible — watcher poll + SELL copiés + BUY bloqués par `TraderLifecycleFilter` en tête du pipeline strategy) et `blacklisted` (terminal, piloté par `BLACKLISTED_WALLETS` env). `paused` fusionné dans `shadow` avec flag UX `previously_demoted_at` (migration 0007 one-shot + `_decide_active` demote branch écrit shadow désormais). Cascade 1 swap max par cycle (EC-2) : le shadow/sell_only avec la plus grande delta(score_candidat − score_worst_active) ≥ `EVICTION_SCORE_MARGIN` (0.15) pendant `EVICTION_HYSTERESIS_CYCLES` (3) cycles consécutifs évince le worst active non-pinned. Pinned jamais sujet à eviction (EC-7). Cap `MAX_SELL_ONLY_WALLETS` (defaults à `MAX_ACTIVE_TRADERS`) évite la cascade pathologique. `EvictionScheduler.reconcile_blacklist` appelé au boot + chaque cycle (idempotent) — T10/T11/T12 alignent DB ↔ env var. 6 nouveaux templates Telegram (eviction_started, eviction_aborted, eviction_completed_to_shadow, eviction_completed_to_active_via_rebound, blacklisted, blacklist_removed). Audit trail complet via `trader_events` avec `event_metadata` enrichi (delta, cycles_observed, triggering_wallet, reason_code). **Jamais de force-close** : les positions d'un sell_only se ferment via SELL copié naturellement ou résolution M8. Cf. spec [docs/specs/M5_bis_competitive_eviction_spec.md](docs/specs/M5_bis_competitive_eviction_spec.md).
- **Front-end dashboard (M6)** : pas de build step. Tailwind CDN JIT + palette Radix Colors (CSS variables) + Inter (Google Fonts) + Lucide icons + HTMX + Chart.js, tout via CDN HTTPS. SVG sparklines inline côté serveur (Jinja macro). Zéro `node_modules/`. Bundle CSS+JS < 300 KB au premier load (hors Google Fonts).
- **CLI entrypoint M9** : `__main__.py` est minimaliste (3 lignes). Toute la logique boot dans `src/polycopy/cli/runner.py`. Rendu terminal via `rich` (dépendance explicite). Par défaut silencieux (`CLI_SILENT=true`). Flag `--verbose` restaure le stream JSON stdout, flag `--no-cli` mode daemon (zéro stdout).
- **Logs M9** : destination par défaut = `~/.polycopy/logs/polycopy.log` via `RotatingFileHandler` (10 MB × 10). Permissions 0o700/0o600. Fichier **toujours** écrit, même en `--verbose` (double stream). Pas de logs en DB. Structlog routé via `stdlib.LoggerFactory` (M1..M8 utilisait `PrintLoggerFactory(stdout)` qui court-circuitait stdlib — bug latent levé par M9). Les processors structlog restent **identiques** à M1..M8.
- **Identité multi-machine (M12_bis Phase A)** : `MACHINE_ID: str | None` (fallback `socket.gethostname()`) + `MACHINE_EMOJI: str = "🖥️"` sont injectés en 2e ligne de chaque alerte Telegram via le helper `_inject_mode()` — pattern strict copy-paste du `mode_badge` M10 (cf. [alert_renderer.py:146-170](src/polycopy/monitoring/alert_renderer.py#L146)). Normalisation stricte dans `Settings._resolve_machine_id` : `strip().upper()` → regex `^[A-Z0-9_-]+$` (chars hors jeu → `-`) → cap 32 chars → `"UNKNOWN"` si aucun alphanumérique ne subsiste après normalisation. Public, non-sensible — loggé en clair au boot via event `machine_id_resolved` avec `source="env"|"hostname"`. Les 15 templates `.md.j2` portent la ligne `{{ machine_emoji }} *{{ machine_id | telegram_md_escape }}*`. Emoji au **choix de l'utilisateur par machine** (ex. `🖥️` PC fixe, `💻` MacBook, `🏫` université) — **pas de mapping automatique** hostname→emoji. Cf. spec [M12_bis §3](docs/specs/M12_bis_multi_machine_remote_control_spec.md). À partir de M12_bis Phase G, chaque template Telegram inclut aussi un lien dashboard cliquable `[📊 Dashboard](http://...)` calculé une fois au boot via `compute_dashboard_url(settings)` ([monitoring/dashboard_url.py](src/polycopy/monitoring/dashboard_url.py)) — URL Tailscale `http://{machine_id}.{tailnet}:{port}/` si `DASHBOARD_BIND_TAILSCALE=true` + tailnet résolu (via `tailscale status --json` ou `TAILNET_NAME` override) + `MACHINE_ID` set, fallback `http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/` sinon, absent si dashboard désactivé. `resolve_tailnet_name` est best-effort (ne lève jamais — fallback transparent sur erreur).
- **Remote control Tailscale (M12_bis Phase B..E)** : package [src/polycopy/remote_control/](src/polycopy/remote_control/) expose une API FastAPI **Tailscale-only** pilotée par `REMOTE_CONTROL_ENABLED=false` par défaut. Routes : `GET /v1/health`, `GET /v1/status/<machine>`, `POST /v1/{restart,stop,resume}/<machine>`. Auth : `TOTPGuard` (pyotp, `valid_window=1` ±30s) + `RateLimiter` deque sliding 60s/5 tentatives/IP + `AutoLockdown` 3-strikes → touch sentinel + Alert CRITICAL. Mismatch path machine → 404 body vide (silence strict). `RemoteControlOrchestrator.__init__` appelle `resolve_tailscale_ipv4` → crash boot clair (`RemoteControlBootError`) si Tailscale absent. Mode paused piloté par sentinel `~/.polycopy/halt.flag` (permissions 0o600/0o700) — bifurcation dans [cli/boot.py::build_orchestrators(mode)](src/polycopy/cli/boot.py) : `MonitoringOrchestrator(paused=True)` + `DashboardOrchestrator` + `RemoteControlOrchestrator`, Watcher/Strategy/Executor/Discovery/LatencyPurge exclus. Kill switch → `touch → stop_event.set()` **ordre strict** (inverse = respawn unsafe en mode normal). `--force-resume` CLI recovery. `DASHBOARD_BIND_TAILSCALE=true` expose le dashboard sur la même IP Tailscale (même résolveur, fail-fast cohérent). Artefacts superviseur : [scripts/supervisor/{systemd,launchd,windows}/](scripts/supervisor/). Setup guide : [docs/specs/M12_bis_remote_control_setup_guide.md](docs/specs/M12_bis_remote_control_setup_guide.md). ADR : [idea2_remote_control_decision.md](docs/specs/idea2_remote_control_decision.md).

## Architecture (rappel)

```
src/polycopy/
├── watcher/      Détection trades on-chain (Data API polling)
├── strategy/     Filtres, sizing, risk manager
│   ├── clob_ws_client.py   WebSocket CLOB `market` cache (M11)
│   ├── _cache_policy.py    TTL adaptatif Gamma (M11)
│   └── pipeline.py         6 filtres ordonnés : TraderLifecycle → Market → EntryPrice → PositionSizer → Slippage → Risk. EntryPriceFilter (bug 4) rejette les BUY > `STRATEGY_MAX_ENTRY_PRICE` (défaut 0.97, SELL passthrough).
├── executor/     Construction & envoi ordres CLOB
├── storage/      Models SQLAlchemy + repositories (inclut `trade_latency_samples` M11 + `LatencyPurgeScheduler`)
├── monitoring/   Logs, metrics, alertes
├── dashboard/    FastAPI + HTMX + Chart.js, localhost-only, read-only (M4.5, opt-in ; onglet `/latency` M11, onglet `/traders/scoring` M12)
├── discovery/
│   ├── scoring/v1.py               Formule v1 M5 (consistency+roi+div+vol)
│   ├── scoring/v2/factors/         6 facteurs purs M12 (Sortino+Brier+timing+HHI+consistency+discipline)
│   ├── scoring/v2/gates.py         6 gates durs M12 pre-scoring
│   ├── scoring/v2/normalization.py Winsorisation p5-p95 pool-wide
│   ├── scoring/v2/aggregator.py    compute_score_v2 (pondération fixe 0.25/0.20/0.20/0.15/0.10/0.10)
│   ├── scoring/v2/category_resolver.py  Condition_id → catégorie top-level (Gamma ?include_tag=true)
│   ├── metrics_collector_v2.py     Wrapper MetricsCollector M5 + TraderDailyPnl + brier/zombie/etc.
│   ├── trader_daily_pnl_writer.py  Scheduler 24h equity curve (prérequis v2 Sortino/Calmar)
│   ├── eviction/                   M5_bis — compétition adaptative (opt-in EVICTION_ENABLED)
│   │   ├── cascade_planner.py      Pure planner : 1 swap/cycle par plus grande delta
│   │   ├── state_machine.py        Pure T6/T8 + reconcile_blacklist decisions
│   │   ├── hysteresis_tracker.py   In-memory compteurs par wallet candidat
│   │   ├── scheduler.py            Orchestre DB + alerts + idempotent reconcile
│   │   └── dtos.py                 EvictionDecision + EvictionTransition Literal
│   └── ... (M5 candidate_pool, decision_engine, orchestrator — demote écrit shadow M5_bis)
├── remote_control/  M12_bis — FastAPI Tailscale-only + TOTP + sentinel (opt-in)
│   ├── tailscale.py       resolve_tailscale_ipv4 + RemoteControlBootError
│   ├── auth.py            TOTPGuard + RateLimiter + AutoLockdown
│   ├── sentinel.py        SentinelFile (halt.flag 0o600)
│   ├── server.py          build_app FastAPI — /v1/{health,status,restart,stop,resume}
│   └── orchestrator.py    RemoteControlOrchestrator (uvicorn Tailscale bind)
├── cli/
│   ├── runner.py          Entrypoint asyncio + bifurcation running|paused M12_bis
│   └── boot.py            build_orchestrators(mode) M12_bis
├── config.py     Pydantic Settings (env vars uniquement)
└── __main__.py   Entrypoint asyncio
```

Règle de dépendance : `watcher` → `storage`, `strategy` → `storage`, `executor` → `storage`. Aucun module ne dépend d'un autre module fonctionnel directement, tout passe par la DB ou par des events asyncio. Le `__main__` orchestre.

## APIs Polymarket utilisées

Source de vérité pour tous les schémas : skill Claude Code `/polymarket:polymarket`. Capturer toute réponse réelle en fixture (`tests/fixtures/`) avant de rédiger un DTO.

- **Data API** : `https://data-api.polymarket.com/activity` (public, no auth)
  - Doc : https://docs.polymarket.com/api-reference/core/get-user-activity
  - Rate limit : ~100 req/min, prévoir backoff exponentiel sur 429
- **Gamma API** : `https://gamma-api.polymarket.com` (public)
  - Métadonnées marchés (slug, conditionId, tokenIds, expiration).
  - **Utilisé à M2** par `MarketFilter` (liquidité, expiration, état actif). Cache TTL 60s côté client.
  - Pièges API : `clobTokenIds`, `outcomes`, `outcomePrices` sont des strings JSON-stringifiées (pas des arrays). `questionID` (et non `questionId`) — case spécifique.
- **CLOB API** : `https://clob.polymarket.com` (auth L1 + L2 pour trading)
  - Doc : https://docs.polymarket.com/developers/CLOB/
  - **À M2 utilisé en read-only** (`GET /midpoint?token_id=...`, sans auth) par `SlippageChecker`. Réponse réelle : `{"mid": "0.08"}` (et **non** `mid_price` comme l'OpenAPI annonce).
  - À partir de M3 : auth L1+L2 via `py-clob-client` (jamais d'appels REST directs sauf si le SDK n'expose pas l'endpoint).
- **CLOB WebSocket** : `wss://ws-subscriptions-clob.polymarket.com`
  - Channel `market` pour les prix temps réel.
  - **Utilisé à M11** par `SlippageChecker` via `ClobMarketWSClient` (cache in-memory mid-price, lazy sub sur les `token_id` candidats, unsub après 5 min d'inactivité, health check 30 s, reconnect tenacity). Fallback HTTP `/midpoint` transparent si WS down ou `STRATEGY_CLOB_WS_ENABLED=false`. Read-only public — aucune creds touchée.
  - Types de messages consommés : `book` (snapshot orderbook), `price_change`, `last_trade_price`, `best_bid_ask`, `market_resolved`. Schéma exact capturé dans `tests/fixtures/clob_ws_market_sample.jsonl` (rafraîchissable via `scripts/capture_clob_ws_fixture.py`).
- **Data API endpoints M5** (découverte + metrics, public no-auth) :
  - `GET /holders?market=<conditionId>&limit=20` — top holders d'un marché (bootstrap).
  - `GET /trades?limit=500&filterType=CASH&filterAmount=100&takerOnly=true` — feed global (bootstrap).
  - `GET /value?user=<addr>` — sanity check capital (pré-filtre pool).
  - `GET /positions?user=<addr>&sortBy=CASHPNL` — historique positions (calcul win_rate/ROI).
  - **Pièges confirmés** (§14.5 spec M5) : (1) `/trades` ne renvoie **pas** `usdcSize` — recalculer `size × price` client-side. (2) Le filtre `outcomeIndex` / `pseudonym` peuvent être null sur `/holders`.
- **Goldsky subgraph** (`DISCOVERY_BACKEND=goldsky|hybrid`, opt-in, public) :
  - URL default : `pnl-subgraph/0.0.14/gn` (⚠️ la spec initiale mentionnait `positions-subgraph/0.0.7` qui n'a **pas** de `realizedPnl` — utiliser `pnl-subgraph` / entité `userPositions`).
  - Numbers retournés en string BigInt (échelle USDC 10⁶, parser via `Decimal`).
  - Versions drift : hardcoder un fallback en code, permettre override via env (`GOLDSKY_POSITIONS_SUBGRAPH_URL`).
- **Pas de leaderboard endpoint** côté Polymarket public — le bootstrap M5 dérive via `/holders` + `/trades` (décision §14.3 #1 + §2.1 spec M5).

## Sécurité — RÈGLES STRICTES

- **JAMAIS** committer `.env`, clé privée, ou API credentials (vérifier `.gitignore`)
- La clé privée vit uniquement dans une env var, jamais en dur dans le code, jamais loggée
- `polymarket_private_key` et `polymarket_funder` sont **optionnels** au niveau config — ils ne sont consommés que par l'Executor (M3), qui devra refuser de démarrer si `DRY_RUN=false` et l'une des deux est absente.
- Tous les ordres passent par le `RiskManager.check()` avant `OrderExecutor.send()`. Pas d'exception.
- Le mode `--dry-run` doit être respecté partout : si `settings.dry_run is True`, l'executor log l'ordre mais ne l'envoie pas
- Le kill switch (`KILL_SWITCH_DRAWDOWN_PCT`) coupe tout : ferme le watcher, n'envoie plus d'ordres, alerte Telegram
- À M2 la strategy est **read-only** (Gamma + CLOB midpoint, pas de signature, pas de POST). `settings.dry_run` n'a pas d'effet sur la strategy. Le garde-fou `dry_run` kicks in à M3 quand l'Executor le lit avant d'envoyer un ordre.
- **Executor M3** : 4 garde-fous obligatoires :
  1. Lazy init `ClobClient` (pas instancié si `dry_run=true`).
  2. `RuntimeError` au démarrage si `dry_run=false` ET clés absentes (lève **avant** TaskGroup).
  3. Double check par ordre : `assert dry_run is False` juste avant chaque `create_and_post_order`.
  4. `WalletStateReader` re-fetch wallet state avant POST, reject si exposition + cost > capital.
- **Creds CLOB L2** (`api_key`, `api_secret`, `api_passphrase`) ne doivent JAMAIS être loggées même partiellement, même en debug. Le seul log lié = `executor_creds_ready` sans aucun champ creds.
- `signature_type` mismatch = transactions rejetées silencieusement par CLOB. `0` EOA standalone, `1` Magic/Polymarket.com (proxy), `2` Gnosis Safe (MetaMask connecté à polymarket.com — cas le plus fréquent).
- `TELEGRAM_BOT_TOKEN` ne doit JAMAIS être commit ni loggé, même partiellement. Le token est visible dans l'URL des appels `sendMessage` — HTTPS exclusif (httpx default), pas de log d'URL en clair, rotation immédiate si token compromis. Bypass silencieux si absent (aucun crash).
- **Monitoring M4** : kill switch déclenché EXCLUSIVEMENT par `PnlSnapshotWriter` sur `KILL_SWITCH_DRAWDOWN_PCT`. **Identique dans les 3 modes** SIMULATION/DRY_RUN/LIVE depuis M10 — le dry-run utilise capital virtuel + positions simulées pour le calcul du drawdown. Les alertes Telegram en dry-run portent un badge visuel `🟢 DRY-RUN` pour différencier de `🔴 LIVE`, mais la sévérité (CRITICAL) est identique. En SIMULATION (backtest offline), le `stop_event` est local au run, pas global. `RiskManager` (M2) reste inchangé — pas de refactor.
- **Migrations Alembic** : `alembic upgrade head` tourne au boot (`init_db`). Si DB M3 préexistante sans `alembic_version` → auto-stamp baseline puis upgrade. Manuel : `alembic stamp head` documenté dans `docs/setup.md`.
- **Dashboard M4.5 / M6** : bind `127.0.0.1` exclusif par défaut, opt-in via `DASHBOARD_ENABLED=true`. `DASHBOARD_HOST=0.0.0.0` = responsabilité de l'utilisateur (documenté avec ⚠️). Aucun endpoint write (toutes les routes sont `GET`, vérifié en test). Aucun secret (Telegram token, private key, funder, CLOB L2 creds, `GOLDSKY_API_KEY` hypothétique) ne doit apparaître dans les responses HTML/JSON ni dans les templates source — vérifié par `test_dashboard_security.py` + `test_dashboard_security_m6.py` (grep automatisé sur `templates/`). M6 conserve strictement ces invariants — les ajouts UX (Tailwind CDN, Lucide, Inter) ne touchent pas au back-end sécurité. `localStorage` client uniquement pour la préférence UI `polycopy.theme` (pas de token, pas de session, pas de donnée DB). CDN HTTPS uniquement (jsdelivr, unpkg, fonts.googleapis.com), pinned (Tailwind 3.4.16, HTMX 2.0.4, Chart.js 4.4.7, Lucide 0.469.0). Swagger/OpenAPI désactivés (`docs_url=None`, `openapi_url=None`). **M10 preset** : `localStorage` client stocke en plus `polycopy.logs.preset` (valeur `business` | `access`). Strictement UI, aucun contenu DB ni secret.
- **Discovery M5** : `DISCOVERY_ENABLED=false` par défaut. Read-only stricte (Data API + Gamma + Goldsky publics, aucune creds CLOB). Un wallet auto-découvert reste en `status='shadow'` pendant `TRADER_SHADOW_DAYS` jours avant `active` (capital safety — bypass uniquement avec `TRADER_SHADOW_DAYS=0` ET `DISCOVERY_SHADOW_BYPASS=true` ET log WARNING au boot). `MAX_ACTIVE_TRADERS` est un cap dur — M5 ne retire **jamais** arbitrairement un wallet existant pour faire place. `BLACKLISTED_WALLETS` est une exclusion absolue vérifiée 2× (pre-bootstrap + pre-promotion). Les wallets de `TARGET_WALLETS` env deviennent **`pinned`** (jamais demote-ables — `transition_status` raise `ValueError` sur pinned). Toute décision (`promote/demote/keep/skip`) est loggée structlog ET écrite dans `trader_events` (audit trail). Formule de scoring versionnée via `SCORING_VERSION` — pas de rewrite rétroactif des `trader_scores` historiques. `GOLDSKY_API_KEY` (hypothétique, Goldsky fair-use sans clé à v1) discipline identique à `TELEGRAM_BOT_TOKEN`. Throttle `asyncio.Semaphore(5)` in-process sur `DiscoveryDataApiClient` — pic ≤ ~60 req/min.
- **Telegram M7** : M7 étend M4 — `StartupNotifier`, `HeartbeatScheduler`, `DailySummaryScheduler`, `AlertDigestWindow` co-lancés par le `MonitoringOrchestrator`. Tous opt-in sauf `TELEGRAM_STARTUP_MESSAGE=true` (no-op si pas de token). Parse mode passé à `MarkdownV2` — templates Jinja2 (`src/polycopy/monitoring/templates/`) + `fallback.md.j2` échappent toutes les valeurs user-controlled via `telegram_md_escape`. `autoescape=False` + `StrictUndefined` (HTML escape incompatible MarkdownV2 ; variable manquante crash explicitement). Templates surchargeables via `assets/telegram/*.md.j2` (cascade FileSystemLoader). Bot reste **emitter-only** : aucune commande entrante — décision §13 spec M7 fermée. Aucune persistance DB des messages envoyés (éphémère). Rotation token tous les 6 mois (BotFather `/token`). Aucun secret ne doit apparaître dans un template ou dans un log — vérifié par grep automatisé dans `test_telegram_template_rendering.py`.
- **Logs file M9** : `~/.polycopy/logs/polycopy.log` peut contenir wallets publics, condition_ids, timestamps — **non sensible en soi** mais **à ne pas partager tel quel** (identifie ta stratégie). Permissions 0o600 fichier + 0o700 parent appliquées par `cli/logging_config.py`. Endpoint `/logs/download` accessible uniquement si `DASHBOARD_ENABLED=true` ET `DASHBOARD_LOGS_ENABLED=true` (default true) ET bind localhost (default `127.0.0.1`). Filename hardcodé `polycopy.log` — jamais user-controlled. Filtres `/logs?levels=` validés enum strict (5 levels stdlib uniquement) ; `q` capé à 200 chars (Pydantic) ; `events` cap 20 (validation custom). Aucun secret loggé fichier — vérifié via `test_cli_subprocess_smoke.py::test_no_secret_leak_in_log_file` et grep automatisé. **M10 hygiene** : processor structlog `filter_noisy_endpoints` (inséré en tête de chaîne) drop les `dashboard_request` 2xx/3xx des paths polling whitelist (`^/api/health-external$`, `^/partials/.*$`, `^/api/version$`) avant formatage JSON — économie CPU + fichier log ~30× moins volumineux sur Home actif. Erreurs 4xx/5xx passent toujours. Override via env `DASHBOARD_LOG_SKIP_PATHS` (additif). Lecteur `/logs` exclut `dashboard_request` par défaut ; opt-in via query `events=dashboard_request` ou preset UI "Include HTTP access" (persisté `localStorage` clé `polycopy.logs.preset`, cohérent M6 `polycopy.theme`).
- **Dry-run M8** : `DRY_RUN_REALISTIC_FILL=true` (opt-in strict, default `false`) active la simulation orderbook FOK via `GET /book` read-only public — utilisable uniquement si `EXECUTION_MODE=dry_run` (ignoré en SIMULATION et LIVE). **Triple garde-fou M3 préservé intact** + 4ᵉ garde-fou M8 réaffirmé M10 : `assert settings.execution_mode == "dry_run"` avant chaque `_persist_realistic_simulated`. Diff strictement additif sur M3 (zéro ligne modifiée dans `ClobWriteClient`, `_persist_sent_order`, `_assert_capital_available`). Aucune creds consommée par le path M8 (uniquement `/book`, `/midpoint`, Gamma `/markets`). Ségrégation data : `MyOrder.realistic_fill=True` + `MyPosition.simulated=True` + contrainte unique triple `(condition_id, asset_id, simulated)`. **Kill switch actif identique live depuis M10** (alerte CRITICAL `kill_switch_triggered` avec badge `🟢 DRY-RUN`, `stop_event.set()` déclenché). L'ancienne alerte `dry_run_virtual_drawdown` INFO est **supprimée** M10 — remplacée par le vrai kill_switch_triggered. v1 : SELL sur position virtuelle inexistante → `dry_run_sell_without_position` warning + skip. Marchés `neg_risk` → résolution skipped (`dry_run_resolution_neg_risk_unsupported`), position reste open virtuellement. `DryRunResolutionWatcher` lancé conditionnellement par `ExecutorOrchestrator` (TaskGroup, pas un nouveau top-level module). `VirtualWalletStateReader` alimente `PnlSnapshotWriter` M4 sans refactor. Cache book in-memory TTL 5 s + LRU 500 entries. `Decimal` pour les calculs orderbook, `float` pour la persistance (jamais `Decimal(float)`). Migration `0004` audit manuel (batch_alter_table SQLite-friendly). Aucun secret loggé — vérifié par `test_m8_security_grep.py`.
- **Pipeline temps réel M11** : le `ClobMarketWSClient` consomme **exclusivement** le channel `market` (public, read-only) — pas de canal `user`, pas d'auth L1/L2, pas de signature. Les creds CLOB restent confinées au chemin live M3. Si `STRATEGY_CLOB_WS_ENABLED=false`, aucune connexion WS ouverte, aucune nouvelle surface. Le cache Gamma adaptatif (`_cache_policy.compute_ttl`) est 100 % en mémoire, aucune creds impliquée. La table `trade_latency_samples` contient uniquement `trade_id` (uuid hex interne, pas une adresse wallet), `stage_name`, `duration_ms`, `timestamp` — **aucun secret, aucun PII**. Cap dur `STRATEGY_CLOB_WS_MAX_SUBSCRIBED=500` avec éviction LRU anti-leak mémoire. Watchdog 30 s : si aucun message reçu depuis 2× `STRATEGY_CLOB_WS_HEALTH_CHECK_SECONDS`, le status passe `down` et un reconnect tenacity est déclenché (max 10 retries avec backoff exponentiel). Flag off = comportement M2..M10 strict (fallback HTTP `/midpoint`).
- **Scoring v2 M12** : `SCORING_VERSION=v1` par défaut — invariant lifecycle M5 strict. Flip vers `v2` = décision humaine manuelle **uniquement après** (1) `SCORING_V2_SHADOW_DAYS` écoulés, (2) rapport `scripts/backtest_scoring_v2.py` montrant `Brier top-10 v2 < Brier top-10 v1 - 0.01` sur set labelé `assets/scoring_v2_labels.csv` (≥ 50 wallets recommandé). Aucun auto-flip. Pendant la shadow period, v2 calcule en parallèle (double-write `trader_scores` avec `scoring_version="v2"`) mais ne pilote **jamais** `DecisionEngine`. Les 6 gates durs pré-scoring v2 (`cash_pnl_90d>0`, `trade_count_90d≥50`, `days_active≥30`, `zombie_ratio<0.40`, not `BLACKLISTED_WALLETS`, not `WASH_CLUSTER_WALLETS`) s'appliquent uniquement en v2 — un wallet rejeté écrit `trader_events.event_type="gate_rejected"` avec raison explicite, **jamais scoré**. `WASH_CLUSTER_WALLETS` env var discipline identique `BLACKLISTED_WALLETS` (exclusion absolue, vérifiée 2× pre-pool + pre-scoring). `TraderDailyPnl` table contient uniquement `wallet_address` publique + `equity_usdc` + `date` — **aucun secret, aucun PII**. Versioning sacré : chaque row `trader_scores` porte sa `scoring_version`, **jamais** rewrite rétroactif d'une formule. La table n'est jamais purgée. Les alertes Telegram `gate_rejected` ne sont **pas** émises v1 (trop bavard ; audit via `trader_events` uniquement, reportable M12.1). Aucune creds CLOB touchée — M12 reste read-only (Data API + Gamma publique + DB locale).
- **Identité multi-machine M12_bis (Phase A)** : `MACHINE_ID` et `MACHINE_EMOJI` sont **publics** (affichés en clair dans chaque alerte Telegram, loggés en clair au boot) — pas de discipline secret, pas de rotation. `MACHINE_ID` est user-controlled (env var) mais la normalisation Pydantic (`^[A-Z0-9_-]+$`) réduit la surface d'injection MarkdownV2 à deux caractères actifs (`-` et `_`), tous les deux systématiquement échappés via `{{ machine_id | telegram_md_escape }}` dans les templates. Test de non-régression `test_machine_id_is_always_escaped_in_templates` (grep automatisé sur `templates/*.md.j2`) garantit qu'aucun futur template ne consomme `MACHINE_ID` sans filtre. Phase A = diff strictement additif (aucun flag maître, aucune régression M12 si l'user ne set pas `MACHINE_ID` — fallback hostname transparent).
- **Remote control M12_bis (Phase B..E)** : `REMOTE_CONTROL_ENABLED=false` par défaut — aucune surface ajoutée. Si `true`, le serveur FastAPI bind **strictement** sur l'IP Tailscale CGNAT (`100.64.0.0/10`) résolue au boot via `tailscale ip -4` — jamais `0.0.0.0` ni `127.0.0.1` (défense en profondeur : validator Pydantic refuse ces valeurs en override + `tailscale.py` re-vérifie la plage CGNAT). Preserve **strictement l'invariant M7 §13** (Telegram emitter-only) : le canal incoming passe par Tailscale, jamais `getUpdates`. `REMOTE_CONTROL_TOTP_SECRET` (base32 ≥16 chars) discipline identique `TELEGRAM_BOT_TOKEN` — **jamais loggé** (même partiel, même en debug, même dans les exceptions, même dans `repr(TOTPGuard)`). Pipeline auth routes destructives : 404 path mismatch (silence strict) → 423 lockdown → 429 rate limit → 401 TOTP invalid → OK. Auto-lockdown 3-strikes brute force : touch sentinel + Alert CRITICAL Telegram + HTTP 423 jusqu'à respawn (per-process, le redémarrage reset `_locked=False` pour permettre `/resume`). Sentinel file `~/.polycopy/halt.flag` permissions strictes 0o600 (fichier) + 0o700 (parent). Kill switch M4 touch sentinel **avant** `stop_event.set()` — ordre inverse = respawn unsafe en mode normal malgré drawdown. Dashboard bind Tailscale (Phase E) partage le même résolveur — même fail-fast, routes GET-only inchangées. Test `test_remote_control_no_secret_leak.py` grep le secret marker dans logs / alertes / body HTTP / headers / repr des objets. Cf. spec [M12_bis §4.4.6](docs/specs/M12_bis_multi_machine_remote_control_spec.md) + ADR [idea2_remote_control_decision.md](docs/specs/idea2_remote_control_decision.md).
- **Watcher live-reload M5_ter** : `WATCHER_RELOAD_INTERVAL_SECONDS=300` par défaut (range [30, 3600]). Diff strictement additif sur `WalletPoller`, `DataApiClient`, `DiscoveryOrchestrator`, `EvictionScheduler` — zéro ligne modifiée. `list_wallets_to_poll()` filtre `status IN ('active', 'pinned', 'sell_only')` en SQL **et** applique un double-check Python-side sur `BLACKLISTED_WALLETS` env (défense-en-profondeur si `reconcile_blacklist` n'a pas encore tourné au boot — aucune fenêtre où un blacklist wallet serait polled). Transitions `active ↔ sell_only` = no-op strict côté watcher (le poller continue pour copier les SELL, les BUY sont bloqués en aval par `TraderLifecycleFilter`). Un `pinned` reste **intouchable** par le watcher — `transition_status` raise sur pinned (safeguard M5), donc la mutation n'arrive jamais dans la DB. Cycle de reload isolé sur failure : un `list_wallets_to_poll()` qui raise est capturé (`watcher_reload_failed` warning), le cycle est skippé, les pollers existants continuent — pas de collapse en cascade. La table `target_traders` ne porte **aucun secret** (wallet addresses publiques on-chain) ; les logs `watcher_reload_cycle` contiennent uniquement des `wallet_address` + compteurs entiers (vérifié par `test_no_secret_leak_in_watcher_logs`). Tests M1/M5_bis existants passent identiques (non-régression via `except* asyncio.CancelledError` en sortie TaskGroup). Cf. spec [docs/specs/M5_ter_watcher_live_reload_spec.md](docs/specs/M5_ter_watcher_live_reload_spec.md).
- **Competitive eviction M5_bis** : `EVICTION_ENABLED=false` par défaut — zéro diff observable vs lifecycle M5 strict (EvictionScheduler non-instancié dans `DiscoveryOrchestrator`). Si `true`, **aucun force-close n'est émis par le bot** : les positions des `sell_only` se ferment uniquement via SELL copié du wallet source (`TraderLifecycleFilter` bloque BUY, laisse SELL passer) ou à résolution M8 du marché. Un `pinned` n'est **jamais** sujet à eviction (EC-7 safeguard, cohérent invariant M5). `MAX_SELL_ONLY_WALLETS` (défaut = `MAX_ACTIVE_TRADERS`) évite la cascade pathologique sur scores volatils. `BLACKLISTED_WALLETS` reste discipline identique M5 (public, non-secret) — `reconcile_blacklist` au boot + chaque cycle aligne DB ↔ env var. Conflit `TARGET_WALLETS ∩ BLACKLISTED_WALLETS` avec `EVICTION_ENABLED=true` = crash boot clair (validator Pydantic cross-field). Migration 0007 data migration idempotente : `paused → shadow + previously_demoted_at = last_scored_at`. L'enum `trader_events.event_type` M5_bis (promoted_active_via_eviction, demoted_to_sell_only, eviction_aborted, promoted_active_via_rebound, eviction_completed_to_shadow, blacklisted, blacklist_removed, defer_*) est append-only — jamais de purge. `event_metadata` porte delta, cycles_observed, triggering_wallet, reason_code. **HysteresisTracker in-memory** : un restart reset les compteurs (retard max = `EVICTION_HYSTERESIS_CYCLES` cycles supplémentaires, acceptable — pas de nouveau schéma DB). 6 nouveaux templates Telegram n'exposent **aucun secret** (vérifié par `test_eviction_telegram_templates.py::test_template_no_raw_private_key_leak` — grep defensive sur private_key/telegram_bot_token/api_secret). Cf. spec [docs/specs/M5_bis_competitive_eviction_spec.md](docs/specs/M5_bis_competitive_eviction_spec.md).

## Tests

- `pytest` + `pytest-asyncio`
- Mocks API : `respx` pour httpx, fixtures pour les réponses Polymarket réelles capturées (dans `tests/fixtures/`)
- Coverage cible : 80% sur `strategy/` et `executor/` (le code critique), best-effort ailleurs
- Pas d'appel réseau réel dans les tests unitaires. Les tests d'intégration vivent dans `tests/integration/` et sont opt-in (`pytest -m integration`)

## Workflow Git

- Branche par feature : `feat/watcher-polling`, `fix/slippage-edge-case`
- Conventional commits : `feat(watcher): poll multiple wallets in parallel`
- PR squashée vers `main`
- CI : ruff + mypy + pytest sur chaque PR

## Commandes courantes

```bash
pytest                         # tests unitaires
pytest -m integration          # tests réseau réels (lents)
ruff check . && ruff format .  # lint + format
mypy src                       # types
python -m polycopy --dry-run   # lance le bot en mode safe
```

## Quand tu hésites

- **Sur la sémantique d'un endpoint Polymarket** : invoque d'abord le skill `/polymarket:polymarket`. En dernier recours, https://docs.polymarket.com. Jamais deviner.
- **Sur un choix d'architecture** : préfère la simplicité. Pas d'abstraction tant qu'il n'y a pas 2 implémentations concrètes (règle "rule of three")
- **Sur un trade-off perf vs lisibilité** : lisibilité gagne. Ce bot ne fait pas du HFT, 50ms de latence en plus ne changent rien
- **Sur une feature ambiguë** : demande-moi avant d'implémenter
