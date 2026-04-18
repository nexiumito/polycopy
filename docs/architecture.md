# Architecture

## Vue d'ensemble

5 couches asynchrones, communiquant via la DB et des `asyncio.Queue` :

```
[Data API] [CLOB WS] [Gamma API]
        \     |     /
         v    v    v
        Watcher  ──> Event Store (SQLite)
                         |
                         v
                  Strategy Engine
                  (filtres, sizing, risk)
                         |
                         v
                     Executor ──> Polymarket CLOB
                         |              |
                         v              v
                  Position Tracker  Polygon settlement
                         |
                         v
                  Monitoring (logs, Telegram)
```

## Module : Watcher

> **Status M1** ✅ — implémenté. Voir `specs/M1-watcher-storage.md` pour le détail fonctionnel et `src/polycopy/watcher/` pour le code.

**Responsabilité** : détecter les nouveaux trades des wallets cibles.

**Implémentation** :
- Une coroutine par wallet cible (`asyncio.create_task`)
- Polling toutes les `POLL_INTERVAL_SECONDS` (défaut 5s) sur `data-api.polymarket.com/activity?user=<addr>&type=TRADE&start=<last_seen_ts>`
- Déduplication par `transactionHash` (clé unique en DB)
- Backoff exponentiel sur erreur réseau ou 429
- Émet un événement `NewTradeDetected` dans une `asyncio.Queue` consommée par le Strategy Engine

**Pourquoi pas WebSocket pour la détection ?** Le WS de Polymarket est par marché (token_id), pas par wallet. Pour suivre un wallet sur tous ses marchés, il faudrait s'abonner à des dizaines de tokens en parallèle et filtrer côté client — c'est moins efficace que le polling REST sur `/activity`.

## Module : Storage

> **Status M1** ✅ — `target_traders` et `detected_trades` implémentés. Les autres tables (`my_orders`, `my_positions`, `pnl_snapshots`) sont déclarées en structure mais peuplées à partir de M3.

**Tables principales** :

- `target_traders` (id, wallet_address, label, score, active, added_at)
- `detected_trades` (id, tx_hash UNIQUE, target_wallet, condition_id, asset_id, side, size, usdc_size, price, timestamp, raw_json)
- `my_orders` (id, source_trade_id FK, clob_order_id, side, size, price, status, sent_at, filled_at)
- `my_positions` (id, condition_id, asset_id, size, avg_price, opened_at, closed_at)
- `pnl_snapshots` (id, timestamp, total_usdc, realized_pnl, unrealized_pnl, drawdown_pct)

**Pourquoi SQLite** : single-process, le bot tourne sur un seul VPS, pas besoin de concurrence write multi-instance. Migration vers Postgres triviale via SQLAlchemy si besoin.

## Module : Strategy

> **Status M2** ✅ — implémenté. Pipeline `MarketFilter → PositionSizer → SlippageChecker → RiskManager` exécuté à chaque `DetectedTrade` reçu via `asyncio.Queue` partagée avec le Watcher. Décisions persistées dans `strategy_decisions`. Voir `specs/M2-strategy-engine.md` et `src/polycopy/strategy/`.

> **Status M11** ✅ — pipeline temps réel phase 1. Trois leviers additifs, tous derrière feature flags default `true` : (A) `ClobMarketWSClient` consomme le channel `market` (read-only public, sub/unsub lazy, cache in-memory mid-price, reconnect tenacity, watchdog 30 s, LRU cap 500 tokens) et alimente `SlippageChecker` avec fallback HTTP transparent si WS down ou flag off. (B) Cache Gamma à TTL adaptatif par segment via `_cache_policy.compute_ttl` (résolu → 1 an, proche résolution → 10 s, actif → 300 s, inactif → 3600 s) remplace le TTL 60 s uniforme M2. (C) Instrumentation latence : `trade_id` uuid hex généré par `WalletPoller`, propagé via DTOs, bindé en contextvar structlog ; 6 stages chronométrés avec `time.perf_counter_ns` (`watcher_detected_ms`, `strategy_enriched_ms`, `strategy_filtered_ms`, `strategy_sized_ms`, `strategy_risk_checked_ms`, `executor_submitted_ms`), persistés dans la nouvelle table `trade_latency_samples` (migration 0005 additive, purge 7 jours au boot + scheduler quotidien). Dashboard `/latency` rend un bar chart p50/p95/p99 par stage avec filtre `?since=`. Cible 2-3 s end-to-end vs ~10-15 s pré-M11. Voir `specs/M11-realtime-pipeline-phase1.md`.

Pipeline en étages, chaque étage peut rejeter le trade avec une raison loggée :

1. **MarketFilter** : vérifie liquidité ≥ seuil, expiration ≥ seuil, marché actif (via Gamma API, cache 60s)
2. **PositionSizer** : calcule `my_size = source_size * COPY_RATIO`, plafonne à `MAX_POSITION_USD`, vérifie qu'on n'a pas déjà la position
3. **SlippageChecker** : query le mid-price actuel via CLOB, rejette si `|current - source_price| / source_price > MAX_SLIPPAGE_PCT`
4. **RiskManager** : vérifie capital disponible, exposition totale, drawdown vs `KILL_SWITCH_DRAWDOWN_PCT`

Si tous les checks passent, émet un événement `OrderApproved` consommé par l'Executor.

## Module : Executor

> **Status M3** ✅ — implémenté. Dry-run par défaut (aucun POST CLOB). Mode réel via `py-clob-client` avec L1→L2 auth dérivation au boot. Pipeline : metadata fetch → tick-size round → garde-fou capital → POST → persist + position upsert. Voir `specs/M3-executor.md` et `src/polycopy/executor/`.

> **Status M8** ✅ — dry-run réaliste. Si `EXECUTION_MODE=dry_run` ET `DRY_RUN_REALISTIC_FILL=true`, l'executor simule chaque FOK via `GET /book` (read-only public, aucune signature), calcule le prix moyen pondéré level-by-level (`Decimal` interne, `float` à la persistance), persiste l'ordre + la position virtuelle (`MyOrder.realistic_fill=True`, `MyPosition.simulated=True`, contrainte unique triple `(condition_id, asset_id, simulated)`). Un `DryRunResolutionWatcher` co-lancé dans le TaskGroup executor poll Gamma toutes les `DRY_RUN_RESOLUTION_POLL_MINUTES` (30 min default) pour matérialiser le `realized_pnl` à la résolution des marchés binaires (neg_risk skipped v1). Le `VirtualWalletStateReader` agrège `Σ size × midpoint` + `realized` pour alimenter `PnlSnapshotWriter` M4 avec `is_dry_run=True`. **Triple garde-fou M3 préservé intact + 4ᵉ garde-fou** (`assert settings.execution_mode == "dry_run"` avant chaque `_persist_realistic_simulated`). Voir `specs/M8-dry-run-realistic.md`.

> **Status M10** ✅ — parité dry-run / live + hygiène logs. `EXECUTION_MODE: "simulation" | "dry_run" | "live"` remplace `DRY_RUN: bool` (legacy lu 1 version avec warning). Kill switch + alertes Telegram **identiques dans les 3 modes** — seule la signature CLOB (POST réel) diffère. Chaque template Telegram porte un badge header `🟢 SIMULATION` / `🟢 DRY-RUN` / `🔴 LIVE` injecté par `AlertRenderer(mode=...)`. Un processor structlog `filter_noisy_endpoints` drop les `dashboard_request` 2xx/3xx des paths polling haute fréquence (`^/api/health-external$`, `^/partials/.*$`, `^/api/version$`) avant formatage JSON — ratio log utile ~28× supérieur. L'onglet `/logs` exclut `dashboard_request` par défaut (opt-in via `events=dashboard_request` ou bouton preset UI). Voir `specs/M10-parity-and-log-hygiene.md`.

- Initialise `ClobClient` au démarrage avec les credentials L2 dérivés
- Pour chaque `OrderApproved` :
  - Construit un `MarketOrderArgs` (FOK) ou `OrderArgs` (GTC limit)
  - Signe et envoie via `client.post_order()`
  - Persiste l'ordre dans `my_orders`, met à jour `my_positions` au fill
- Gestion des erreurs CLOB : retry sur erreurs transitoires, alerte sur erreurs de signature/auth

**Choix maker vs taker** : par défaut on fait du taker (FOK) pour la simplicité et la garantie d'exécution. Une amélioration future est de poster du limit légèrement sous le mid pour profiter des rebates maker.

## Module : Monitoring

> **Status M4** ✅ — implémenté. Alertes Telegram (httpx direct) avec cooldown 60 s par event_type. PnL snapshots persistés toutes les 5 min via `WalletStateReader` (M3 réutilisé). Kill switch déclenché par le writer si drawdown ≥ `KILL_SWITCH_DRAWDOWN_PCT` — depuis M10 **identique dans les 3 modes** (inversion de l'invariant M4 initial). Alembic gère les migrations. Voir `specs/M4-monitoring.md` et `src/polycopy/monitoring/`.

- **Logs** : `structlog` JSON, tous les events importants (trade détecté, filtré, exécuté, erreur, kill switch).
- **Alertes Telegram** (optionnelles) : envoi async sur events critiques. Si `TELEGRAM_BOT_TOKEN` absent, bypass silencieux total.
- **Snapshots PnL** : `PnlSnapshotWriter` lit `WalletStateReader` toutes les `PNL_SNAPSHOT_INTERVAL_SECONDS`, persiste en `pnl_snapshots`, calcule le drawdown contre le max historique (filtré `only_real` pour ne pas mélanger stubs dry-run et valeurs réelles).
- **Kill switch** : seul le writer le déclenche (single source of truth). `stop_event.set()` + alerte CRITICAL Telegram + `executor_stopped` en cascade. Jamais en dry-run.
- **Rapport PnL manuel** : `scripts/pnl_report.py --since 7 --output html` génère un HTML statique avec stats + sparkline SVG natif (zéro dep).
- **Migrations DB** : `alembic upgrade head` au boot dans `init_db` ; auto-stamp baseline si DB M3 préexistante sans `alembic_version`.

> **Status M7** ✅ — refonte de la couche Telegram en *compagnon conversationnel*. `StartupNotifier`, `HeartbeatScheduler`, `DailySummaryScheduler` co-lancés par `MonitoringOrchestrator` dans le même TaskGroup que `PnlSnapshotWriter` + `AlertDispatcher`. Templates Jinja2 (`src/polycopy/monitoring/templates/`, `autoescape=False` + `StrictUndefined` + filter `telegram_md_escape` pour Markdown v2) surchargeables via `assets/telegram/*.md.j2` sans fork. `AlertDispatcher` M4 étendu par composition (injection `AlertRenderer` + `AlertDigestWindow`) — zéro refactor, tests M4 inchangés. `fallback.md.j2` préserve le format M4 pour les `event_type` non documentés (zéro régression). Bot reste **emitter-only** : aucune commande entrante. Parse mode passé à `MarkdownV2`. Voir `specs/M7-telegram-enhanced.md`.

## Module : Dashboard (optionnel)

> **Status M4.5** ✅ — implémenté. FastAPI + HTMX + Chart.js. Lancé dans le même `asyncio.TaskGroup` que les autres modules si `DASHBOARD_ENABLED=true`. Bind `127.0.0.1:8787` par défaut. Read-only strict (zéro endpoint write, zéro auth applicative — bind localhost suffit). Voir `specs/M4.5-dashboard.md` et `src/polycopy/dashboard/`.

- **Routes** : 6 pages (Home, Détection, Stratégie, Exécution, Positions, PnL) + ~6 partials HTMX + 1 endpoint JSON pour Chart.js + `/healthz`.
- **Real-time** : HTMX polling `hx-trigger="every 3s"` sur les partials, `setInterval(fetch, 5000)` pour le graph Chart.js.
- **Frontend** : Pico.css classless + HTMX + Chart.js via CDN. Zéro build step.
- **Sécurité** : bind explicite `127.0.0.1`, aucun secret loggé ni rendu (Telegram token, private key, funder, CLOB L2 creds), `SELECT`-only via `session_factory`. Swagger / OpenAPI désactivés.
- **Lifecycle** : uvicorn in-process, shutdown via watchdog `server.should_exit = True` déclenché par le `stop_event` partagé avec `__main__`.

> **Status M6** ✅ — refonte UX. Même back-end, templates réécrits en Tailwind CDN JIT + palette Radix Colors + typo Inter + icônes Lucide. Sidebar gauche, 4 KPI cards Home avec sparkline SVG, jauge score SVG sur Traders, area chart + overlay drawdown + timeline milestones sur PnL, footer avec health Gamma/Data API (cache 30 s). Dark-first, toggle light en localStorage. Responsive mobile via `<details>` sidebar. Stub `/logs` (M9) et toggle dry-run/réel sur PnL préparé (M8). 3 nouvelles routes GET-only : `/logs`, `/api/health-external`, `/api/version`. Aucun secret ne fuite — vérifié par grep automatisé sur les templates source. Voir `specs/M6-dashboard-2026.md`.

> **Status M9** ✅ — onglet `/logs` fonctionnel. Lecture du fichier `~/.polycopy/logs/polycopy.log` (pas DB), filtres serveur (levels enum strict, events cap 20, q max 200 chars), live tail HTMX polling 2 s (pas de WebSocket — invariant M4.5 préservé), bouton `/logs/download` (filename hardcodé `polycopy.log`). Stub conservé si `DASHBOARD_LOGS_ENABLED=false`. Toutes les routes restent GET. Voir `specs/M9-silent-cli-and-readme.md`.

## Module : CLI / Logging (M9)

> **Status M9** ✅ — couche présentation au boot. Écran `rich` statique (panel + 6 modules + dashboard URL + log file path), couleur cyan en dry-run / rouge en LIVE. Fichier log rotatif `RotatingFileHandler` (10 MB × 10 par défaut) toujours actif, **même en `--verbose`** (double stream). Permissions 0o600 sur le fichier, 0o700 sur le parent. Flag `--verbose` restaure le mode M1..M8 (stream JSON stdout). Flag `--no-cli` mode daemon (zéro stdout). `__main__.py` réduit à 3 lignes — toute la logique boot vit dans `src/polycopy/cli/runner.py`. Re-render conditionnel sur status change uniquement (pas de `rich.live.Live`). Aucun secret loggé — vérifié par grep automatisé. Voir `specs/M9-silent-cli-and-readme.md`.

## Module : Discovery (optionnel, M5)

> **Status M5** ✅ — implémenté. Module de découverte et scoring automatique de wallets candidats. Opt-in strict via `DISCOVERY_ENABLED=true`. Read-only sur Data API publique + Gamma + Goldsky (backend opt-in). Aucune signature CLOB, aucune dépendance aux credentials L2. Voir `specs/M5-trader-scoring.md` et `src/polycopy/discovery/`.

- **Pool de candidats** : top holders des top-liquidité markets (`/holders` fan-out) + feed global `/trades` filtré par `usdcSize ≥ $100`. Backend opt-in `goldsky` / `hybrid` : ranking par `realizedPnl` via subgraph GraphQL.
- **Metrics** (fenêtre glissante `SCORING_LOOKBACK_DAYS=90` j) : win rate, ROI réalisé, indice Herfindahl (diversité), volume log-scale.
- **Score v1** : `0.30·consistency + 0.30·roi_norm + 0.20·diversity + 0.20·volume_norm` ∈ [0, 1]. Versionné via `SCORING_VERSION` (reproductibilité, pas de rewrite rétroactif).
- **Statuts** : `shadow` (observation) → `active` (copié) → `paused` (retiré après 3 cycles low). `pinned` = jamais touché (provient de `TARGET_WALLETS` env).
- **Garde-fous non-négociables** : `MAX_ACTIVE_TRADERS` cap dur (refuse + alerte, jamais retire), `TRADER_SHADOW_DAYS` observation obligatoire avant copy, `BLACKLISTED_WALLETS` exclusion absolue (vérifiée 2× : pre-bootstrap + pre-promotion), hystérésis `K=3` cycles avant demote.
- **Audit trail** : chaque décision (`discovered`, `promoted_active`, `demoted_paused`, `kept`, `skipped_blacklist`, `skipped_cap`, `revived_shadow`) écrite dans `trader_events` + log structuré. Historique des scores append-only dans `trader_scores`.
- **Backtest obligatoire avant prod** : `scripts/score_backtest.py` — corrélation Spearman score ↔ ROI observé ≥ 0.30 sur ≥ 50 wallets seed (cf. `specs/m5_backtest_seed.txt`).
- **Throttle API** : `asyncio.Semaphore(5)` in-process sur le `DiscoveryDataApiClient` — pic ≤ ~60 req/min observé.
- **Règle de dépendance** : `discovery/ → storage/` + `config/` + `monitoring/dtos` uniquement. Aucune dépendance vers `watcher/`, `strategy/`, `executor/`, `dashboard/core`. Communique via DB (update `target_traders`) et `alerts_queue`.

## Latence & timing

Latence cible détection → exécution : **~10-15 secondes** sur le path heureux.
- Polling : moyenne 2.5s (intervalle 5s / 2)
- Network round-trip Data API : ~200ms
- Strategy pipeline (avec query Gamma + CLOB mid) : ~500ms
- Order signing + post : ~300ms
- Confirmation matching CLOB : ~100ms

C'est trop lent pour les marchés news-driven très actifs. Acceptable pour les marchés à volatilité modérée et pour des stratégies de "smart money following" où l'edge dure des heures, pas des secondes.

## Évolutions possibles

- **Multi-process** : un process par trader cible si on en suit beaucoup (>20)
- **Stream on-chain direct** via Goldsky subgraph WebSocket pour réduire la latence de détection à ~2s
- **Stratégies dérivées** : pas seulement copier, mais agréger N traders et trader sur consensus
- **Backtesting framework** : rejouer l'historique d'un trader sur des données passées pour valider la stratégie de copy avant de la lancer
