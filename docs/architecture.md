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

Pipeline en étages, chaque étage peut rejeter le trade avec une raison loggée :

1. **MarketFilter** : vérifie liquidité ≥ seuil, expiration ≥ seuil, marché actif (via Gamma API, cache 60s)
2. **PositionSizer** : calcule `my_size = source_size * COPY_RATIO`, plafonne à `MAX_POSITION_USD`, vérifie qu'on n'a pas déjà la position
3. **SlippageChecker** : query le mid-price actuel via CLOB, rejette si `|current - source_price| / source_price > MAX_SLIPPAGE_PCT`
4. **RiskManager** : vérifie capital disponible, exposition totale, drawdown vs `KILL_SWITCH_DRAWDOWN_PCT`

Si tous les checks passent, émet un événement `OrderApproved` consommé par l'Executor.

## Module : Executor

> **Status M3** ✅ — implémenté. Dry-run par défaut (aucun POST CLOB). Mode réel via `py-clob-client` avec L1→L2 auth dérivation au boot. Pipeline : metadata fetch → tick-size round → garde-fou capital → POST → persist + position upsert. Voir `specs/M3-executor.md` et `src/polycopy/executor/`.

- Initialise `ClobClient` au démarrage avec les credentials L2 dérivés
- Pour chaque `OrderApproved` :
  - Construit un `MarketOrderArgs` (FOK) ou `OrderArgs` (GTC limit)
  - Signe et envoie via `client.post_order()`
  - Persiste l'ordre dans `my_orders`, met à jour `my_positions` au fill
- Gestion des erreurs CLOB : retry sur erreurs transitoires, alerte sur erreurs de signature/auth

**Choix maker vs taker** : par défaut on fait du taker (FOK) pour la simplicité et la garantie d'exécution. Une amélioration future est de poster du limit légèrement sous le mid pour profiter des rebates maker.

## Module : Monitoring

> **Status M4** ✅ — implémenté. Alertes Telegram (httpx direct) avec cooldown 60 s par event_type. PnL snapshots persistés toutes les 5 min via `WalletStateReader` (M3 réutilisé). Kill switch déclenché par le writer si drawdown ≥ `KILL_SWITCH_DRAWDOWN_PCT` — **jamais en dry-run**. Alembic gère les migrations. Voir `specs/M4-monitoring.md` et `src/polycopy/monitoring/`.

- **Logs** : `structlog` JSON, tous les events importants (trade détecté, filtré, exécuté, erreur, kill switch).
- **Alertes Telegram** (optionnelles) : envoi async sur events critiques. Si `TELEGRAM_BOT_TOKEN` absent, bypass silencieux total.
- **Snapshots PnL** : `PnlSnapshotWriter` lit `WalletStateReader` toutes les `PNL_SNAPSHOT_INTERVAL_SECONDS`, persiste en `pnl_snapshots`, calcule le drawdown contre le max historique (filtré `only_real` pour ne pas mélanger stubs dry-run et valeurs réelles).
- **Kill switch** : seul le writer le déclenche (single source of truth). `stop_event.set()` + alerte CRITICAL Telegram + `executor_stopped` en cascade. Jamais en dry-run.
- **Rapport PnL manuel** : `scripts/pnl_report.py --since 7 --output html` génère un HTML statique avec stats + sparkline SVG natif (zéro dep).
- **Migrations DB** : `alembic upgrade head` au boot dans `init_db` ; auto-stamp baseline si DB M3 préexistante sans `alembic_version`.

## Module : Dashboard (optionnel)

> **Status M4.5** ✅ — implémenté. FastAPI + HTMX + Chart.js. Lancé dans le même `asyncio.TaskGroup` que les autres modules si `DASHBOARD_ENABLED=true`. Bind `127.0.0.1:8787` par défaut. Read-only strict (zéro endpoint write, zéro auth applicative — bind localhost suffit). Voir `specs/M4.5-dashboard.md` et `src/polycopy/dashboard/`.

- **Routes** : 6 pages (Home, Détection, Stratégie, Exécution, Positions, PnL) + ~6 partials HTMX + 1 endpoint JSON pour Chart.js + `/healthz`.
- **Real-time** : HTMX polling `hx-trigger="every 3s"` sur les partials, `setInterval(fetch, 5000)` pour le graph Chart.js.
- **Frontend** : Pico.css classless + HTMX + Chart.js via CDN. Zéro build step.
- **Sécurité** : bind explicite `127.0.0.1`, aucun secret loggé ni rendu (Telegram token, private key, funder, CLOB L2 creds), `SELECT`-only via `session_factory`. Swagger / OpenAPI désactivés.
- **Lifecycle** : uvicorn in-process, shutdown via watchdog `server.should_exit = True` déclenché par le `stop_event` partagé avec `__main__`.

> **Status M6** ✅ — refonte UX. Même back-end, templates réécrits en Tailwind CDN JIT + palette Radix Colors + typo Inter + icônes Lucide. Sidebar gauche, 4 KPI cards Home avec sparkline SVG, jauge score SVG sur Traders, area chart + overlay drawdown + timeline milestones sur PnL, footer avec health Gamma/Data API (cache 30 s). Dark-first, toggle light en localStorage. Responsive mobile via `<details>` sidebar. Stub `/logs` (M9) et toggle dry-run/réel sur PnL préparé (M8). 3 nouvelles routes GET-only : `/logs`, `/api/health-external`, `/api/version`. Aucun secret ne fuite — vérifié par grep automatisé sur les templates source. Voir `specs/M6-dashboard-2026.md`.

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
