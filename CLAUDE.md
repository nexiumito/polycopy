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
- **Front-end dashboard (M6)** : pas de build step. Tailwind CDN JIT + palette Radix Colors (CSS variables) + Inter (Google Fonts) + Lucide icons + HTMX + Chart.js, tout via CDN HTTPS. SVG sparklines inline côté serveur (Jinja macro). Zéro `node_modules/`. Bundle CSS+JS < 300 KB au premier load (hors Google Fonts).
- **CLI entrypoint M9** : `__main__.py` est minimaliste (3 lignes). Toute la logique boot dans `src/polycopy/cli/runner.py`. Rendu terminal via `rich` (dépendance explicite). Par défaut silencieux (`CLI_SILENT=true`). Flag `--verbose` restaure le stream JSON stdout, flag `--no-cli` mode daemon (zéro stdout).
- **Logs M9** : destination par défaut = `~/.polycopy/logs/polycopy.log` via `RotatingFileHandler` (10 MB × 10). Permissions 0o700/0o600. Fichier **toujours** écrit, même en `--verbose` (double stream). Pas de logs en DB. Structlog routé via `stdlib.LoggerFactory` (M1..M8 utilisait `PrintLoggerFactory(stdout)` qui court-circuitait stdlib — bug latent levé par M9). Les processors structlog restent **identiques** à M1..M8.

## Architecture (rappel)

```
src/polycopy/
├── watcher/      Détection trades on-chain (Data API polling)
├── strategy/     Filtres, sizing, risk manager
├── executor/     Construction & envoi ordres CLOB
├── storage/      Models SQLAlchemy + repositories
├── monitoring/   Logs, metrics, alertes
├── dashboard/    FastAPI + HTMX + Chart.js, localhost-only, read-only (M4.5, opt-in)
├── discovery/    Pool candidats + scoring + decisions (M5, opt-in, read-only)
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
  - Channel `market` pour les prix temps réel
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
- **Monitoring M4** : kill switch déclenché EXCLUSIVEMENT par `PnlSnapshotWriter`, **jamais en dry-run** (sécurité critique). `RiskManager` (M2) reste inchangé — pas de refactor.
- **Migrations Alembic** : `alembic upgrade head` tourne au boot (`init_db`). Si DB M3 préexistante sans `alembic_version` → auto-stamp baseline puis upgrade. Manuel : `alembic stamp head` documenté dans `docs/setup.md`.
- **Dashboard M4.5 / M6** : bind `127.0.0.1` exclusif par défaut, opt-in via `DASHBOARD_ENABLED=true`. `DASHBOARD_HOST=0.0.0.0` = responsabilité de l'utilisateur (documenté avec ⚠️). Aucun endpoint write (toutes les routes sont `GET`, vérifié en test). Aucun secret (Telegram token, private key, funder, CLOB L2 creds, `GOLDSKY_API_KEY` hypothétique) ne doit apparaître dans les responses HTML/JSON ni dans les templates source — vérifié par `test_dashboard_security.py` + `test_dashboard_security_m6.py` (grep automatisé sur `templates/`). M6 conserve strictement ces invariants — les ajouts UX (Tailwind CDN, Lucide, Inter) ne touchent pas au back-end sécurité. `localStorage` client uniquement pour la préférence UI `polycopy.theme` (pas de token, pas de session, pas de donnée DB). CDN HTTPS uniquement (jsdelivr, unpkg, fonts.googleapis.com), pinned (Tailwind 3.4.16, HTMX 2.0.4, Chart.js 4.4.7, Lucide 0.469.0). Swagger/OpenAPI désactivés (`docs_url=None`, `openapi_url=None`).
- **Discovery M5** : `DISCOVERY_ENABLED=false` par défaut. Read-only stricte (Data API + Gamma + Goldsky publics, aucune creds CLOB). Un wallet auto-découvert reste en `status='shadow'` pendant `TRADER_SHADOW_DAYS` jours avant `active` (capital safety — bypass uniquement avec `TRADER_SHADOW_DAYS=0` ET `DISCOVERY_SHADOW_BYPASS=true` ET log WARNING au boot). `MAX_ACTIVE_TRADERS` est un cap dur — M5 ne retire **jamais** arbitrairement un wallet existant pour faire place. `BLACKLISTED_WALLETS` est une exclusion absolue vérifiée 2× (pre-bootstrap + pre-promotion). Les wallets de `TARGET_WALLETS` env deviennent **`pinned`** (jamais demote-ables — `transition_status` raise `ValueError` sur pinned). Toute décision (`promote/demote/keep/skip`) est loggée structlog ET écrite dans `trader_events` (audit trail). Formule de scoring versionnée via `SCORING_VERSION` — pas de rewrite rétroactif des `trader_scores` historiques. `GOLDSKY_API_KEY` (hypothétique, Goldsky fair-use sans clé à v1) discipline identique à `TELEGRAM_BOT_TOKEN`. Throttle `asyncio.Semaphore(5)` in-process sur `DiscoveryDataApiClient` — pic ≤ ~60 req/min.
- **Telegram M7** : M7 étend M4 — `StartupNotifier`, `HeartbeatScheduler`, `DailySummaryScheduler`, `AlertDigestWindow` co-lancés par le `MonitoringOrchestrator`. Tous opt-in sauf `TELEGRAM_STARTUP_MESSAGE=true` (no-op si pas de token). Parse mode passé à `MarkdownV2` — templates Jinja2 (`src/polycopy/monitoring/templates/`) + `fallback.md.j2` échappent toutes les valeurs user-controlled via `telegram_md_escape`. `autoescape=False` + `StrictUndefined` (HTML escape incompatible MarkdownV2 ; variable manquante crash explicitement). Templates surchargeables via `assets/telegram/*.md.j2` (cascade FileSystemLoader). Bot reste **emitter-only** : aucune commande entrante — décision §13 spec M7 fermée. Aucune persistance DB des messages envoyés (éphémère). Rotation token tous les 6 mois (BotFather `/token`). Aucun secret ne doit apparaître dans un template ou dans un log — vérifié par grep automatisé dans `test_telegram_template_rendering.py`.
- **Logs file M9** : `~/.polycopy/logs/polycopy.log` peut contenir wallets publics, condition_ids, timestamps — **non sensible en soi** mais **à ne pas partager tel quel** (identifie ta stratégie). Permissions 0o600 fichier + 0o700 parent appliquées par `cli/logging_config.py`. Endpoint `/logs/download` accessible uniquement si `DASHBOARD_ENABLED=true` ET `DASHBOARD_LOGS_ENABLED=true` (default true) ET bind localhost (default `127.0.0.1`). Filename hardcodé `polycopy.log` — jamais user-controlled. Filtres `/logs?levels=` validés enum strict (5 levels stdlib uniquement) ; `q` capé à 200 chars (Pydantic) ; `events` cap 20 (validation custom). Aucun secret loggé fichier — vérifié via `test_cli_subprocess_smoke.py::test_no_secret_leak_in_log_file` et grep automatisé.
- **Dry-run M8** : `DRY_RUN_REALISTIC_FILL=true` (opt-in strict, default `false`) active la simulation orderbook FOK via `GET /book` read-only public. **Triple garde-fou M3 préservé intact** + 4ᵉ garde-fou M8 : `assert dry_run is True` avant chaque `_persist_realistic_simulated`. Diff strictement additif sur M3 (zéro ligne modifiée dans `ClobWriteClient`, `_persist_sent_order`, `_assert_capital_available`). Aucune creds consommée par le path M8 (uniquement `/book`, `/midpoint`, Gamma `/markets`). Ségrégation data : `MyOrder.realistic_fill=True` + `MyPosition.simulated=True` + contrainte unique triple `(condition_id, asset_id, simulated)`. **Kill switch JAMAIS en dry-run** (invariant M4 préservé). Alerte `dry_run_virtual_drawdown` INFO only à 50 % du seuil — pas WARNING/CRITICAL, pas de `stop_event.set()`. v1 : SELL sur position virtuelle inexistante → `dry_run_sell_without_position` warning + skip. Marchés `neg_risk` → résolution skipped (`dry_run_resolution_neg_risk_unsupported`), position reste open virtuellement. `DryRunResolutionWatcher` lancé conditionnellement par `ExecutorOrchestrator` (TaskGroup, pas un nouveau top-level module). `VirtualWalletStateReader` alimente `PnlSnapshotWriter` M4 sans refactor. Cache book in-memory TTL 5 s + LRU 500 entries. `Decimal` pour les calculs orderbook, `float` pour la persistance (jamais `Decimal(float)`). Migration `0004` audit manuel (batch_alter_table SQLite-friendly). Aucun secret loggé — vérifié par `test_m8_security_grep.py`.

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
