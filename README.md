<p align="center">
  <img src="assets/Company_Logo_Polymarket.png" alt="Polymarket" width="400">
</p>

# polycopy

> Bot de copy trading pour [Polymarket](https://polymarket.com). Surveille l'activité on-chain de wallets cibles et réplique leurs trades sur ton propre wallet, avec sizing, filtres marché et risk management.

⚠️ **Statut : prototype personnel, pas un produit.** Pas de garantie. Trade à tes risques. Lis l'[Avertissement](#avertissement) avant tout usage réel.

---

## Pourquoi ?

Polymarket est entièrement on-chain (Polygon). L'activité de chaque wallet est publique via la Data API. Un bot peut donc détecter en quasi-temps-réel les trades d'un "smart money" wallet et les répliquer — soumis à des filtres : liquidité minimum, slippage, plafond capital.

## Architecture

5 couches asynchrones, faiblement couplées via des `asyncio.Queue` :

```
[Data API]  [Gamma API]  [CLOB read]
     │           │            │
     ▼           ▼            ▼
   Watcher ──> Storage ──> Strategy ──> Executor ──> Polymarket CLOB
                                                          │
                                                          ▼
                                                       Position tracker
```

1. **Watcher** — polling Data API `/activity`, dédup par tx_hash, persistance.
2. **Storage** — SQLAlchemy 2.0 async (SQLite par défaut, Postgres possible).
3. **Strategy** — pipeline `MarketFilter → PositionSizer → SlippageChecker → RiskManager`.
4. **Executor** — dérive les API creds CLOB (L1/L2), signe et POST les ordres FOK via `py-clob-client`. **Dry-run par défaut.**
5. **Monitoring** — logs structlog JSON, alertes Telegram, snapshots PnL en DB, kill switch drawdown (M4).

Détail technique : [docs/architecture.md](docs/architecture.md).

## Stack

- **Python 3.11+** (asyncio, TaskGroup)
- `py-clob-client` (SDK officiel Polymarket pour la signature CLOB)
- `httpx` (async HTTP), `tenacity` (retry exponentiel)
- `SQLAlchemy 2.0` + `aiosqlite` (Postgres trivial via `DATABASE_URL`)
- `Pydantic v2` + `pydantic-settings` (validation config + DTOs)
- `structlog` (logs JSON)
- `pytest` + `respx` (mock HTTP) + `pytest-asyncio`

## Quickstart

Environnement de référence : **WSL Ubuntu (bash)**, repo cloné en chemin Linux natif (`~/code/polycopy`). Un seul script bootstrape tout (idempotent) :

```bash
bash scripts/setup.sh
```

Il crée le `.venv/`, installe les deps, copie `.env.example` → `.env`, et lance un smoke test `python -m polycopy --dry-run`.

À chaque nouvelle session :

```bash
source .venv/bin/activate
python -m polycopy --dry-run     # aucun ordre envoyé
```

Exemple de logs JSON observés en dry-run :

```json
{"event": "polycopy_starting", "dry_run": true, "targets": ["0x192..."], ...}
{"event": "watcher_started", "wallets": ["0x192..."], "interval": 15, ...}
{"event": "strategy_started", "pipeline_steps": ["MarketFilter", "PositionSizer", ...], ...}
{"event": "executor_started", "mode": "dry_run"}
{"event": "trade_detected", "tx_hash": "0xabc...", "side": "BUY", "price": 0.08, ...}
{"event": "order_approved", "tx_hash": "0xabc...", "my_size": 36.85, "slippage_pct": 0.39}
{"event": "order_simulated", "side": "BUY", "size": 36.85, "price": 0.08, "neg_risk": false}
{"event": "order_rejected", "tx_hash": "0xdef...", "reason": "slippage_exceeded", ...}
```

Guide pas-à-pas (install WSL, édition `.env`, troubleshooting) : [docs/setup.md](docs/setup.md).

## Variables d'environnement

| Variable | Description | Default | Requis |
|---|---|---|---|
| `TARGET_WALLETS` | Wallets à copier (CSV ou JSON array) | — | **toujours** |
| `DRY_RUN` | Mode safe (aucun ordre réel envoyé) | `true` | non |
| `POLL_INTERVAL_SECONDS` | Fréquence de polling Data API | `5` | non |
| `COPY_RATIO` | Fraction du trade source à répliquer | `0.01` | non |
| `MAX_POSITION_USD` | Plafond par position | `100` | non |
| `MIN_MARKET_LIQUIDITY_USD` | Liquidité CLOB minimum | `5000` | non |
| `MIN_HOURS_TO_EXPIRY` | Skip marchés trop proches de l'expiration | `24` | non |
| `MAX_SLIPPAGE_PCT` | Slippage max vs prix source | `2.0` | non |
| `KILL_SWITCH_DRAWDOWN_PCT` | Stop tout si drawdown > X% | `20` | non |
| `RISK_AVAILABLE_CAPITAL_USD_STUB` | Capital dispo stub (M3 partiellement remplacé par lecture wallet) | `1000.0` | non |
| `POLYMARKET_PRIVATE_KEY` | Clé privée du wallet de signature | — | **si `DRY_RUN=false`** |
| `POLYMARKET_FUNDER` | Adresse du proxy wallet (Gnosis Safe / Magic) | — | **si `DRY_RUN=false`** |
| `POLYMARKET_SIGNATURE_TYPE` | `0` EOA, `1` Magic, `2` Gnosis Safe | `1` | non |
| `DATABASE_URL` | URL DB | `sqlite+aiosqlite:///polycopy.db` | non |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` | non |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Alertes Telegram (bypass silencieux si vide) | — | non (pour alertes) |
| `PNL_SNAPSHOT_INTERVAL_SECONDS` | Période entre 2 snapshots PnL | `300` | non |
| `ALERT_LARGE_ORDER_USD_THRESHOLD` | Seuil USD au-dessus duquel un fill déclenche `order_filled_large` | `50.0` | non |
| `ALERT_COOLDOWN_SECONDS` | Anti-spam Telegram par event_type (in-memory) | `60` | non |
| `TELEGRAM_STARTUP_MESSAGE` | Envoie un message de démarrage au boot (M7) | `true` | non |
| `TELEGRAM_HEARTBEAT_ENABLED` | Active les heartbeats périodiques (M7) | `false` | non |
| `TELEGRAM_HEARTBEAT_INTERVAL_HOURS` | Intervalle entre 2 heartbeats (1–168 h) | `12` | non |
| `TELEGRAM_DAILY_SUMMARY` | Envoie un résumé quotidien (M7) | `false` | non |
| `TG_DAILY_SUMMARY_HOUR` | Heure locale d'envoi du résumé (0–23) | `9` | non |
| `TG_DAILY_SUMMARY_TIMEZONE` | TZ IANA du résumé quotidien | `Europe/Paris` | non |
| `TELEGRAM_DIGEST_THRESHOLD` | Alertes/heure pour batch digest (M7) | `5` | non |
| `TELEGRAM_DIGEST_WINDOW_MINUTES` | Fenêtre de comptage digest | `60` | non |
| `DASHBOARD_ENABLED` | Active le dashboard local (M4.5, opt-in) | `false` | non |
| `DASHBOARD_HOST` | Bind (localhost par défaut, ⚠️ `0.0.0.0` = expose au LAN) | `127.0.0.1` | non |
| `DASHBOARD_PORT` | Port TCP local du dashboard | `8787` | non |
| `DASHBOARD_THEME` | Thème initial dashboard `dark` / `light` (toggle persiste en localStorage) | `dark` | non |
| `DASHBOARD_POLL_INTERVAL_SECONDS` | Fréquence rafraîchissement HTMX des partials (2–60 s) | `5` | non |
| `DISCOVERY_ENABLED` | Active la découverte auto de traders (M5, opt-in) | `false` | non |
| `DISCOVERY_INTERVAL_SECONDS` | Cadence d'un cycle scoring (1h–7j) | `21600` | non |
| `DISCOVERY_CANDIDATE_POOL_SIZE` | Pool de candidats scannés par cycle | `100` | non |
| `DISCOVERY_TOP_MARKETS_FOR_HOLDERS` | Marchés top-liquidité scannés via `/holders` | `20` | non |
| `MAX_ACTIVE_TRADERS` | Plafond DUR sur les traders `active` | `10` | non |
| `BLACKLISTED_WALLETS` | Wallets jamais ajoutés (CSV ou JSON array) | — | non |
| `SCORING_VERSION` | Version de la formule de scoring | `v1` | non |
| `SCORING_MIN_CLOSED_MARKETS` | Seuil cold start (sous → score=0, low_confidence) | `10` | non |
| `SCORING_LOOKBACK_DAYS` | Fenêtre glissante metrics (jours) | `90` | non |
| `SCORING_PROMOTION_THRESHOLD` | Score ≥ seuil → candidat promotion | `0.65` | non |
| `SCORING_DEMOTION_THRESHOLD` | Score < seuil pendant K cycles → demote | `0.40` | non |
| `SCORING_DEMOTION_HYSTERESIS_CYCLES` | K cycles sous seuil avant demote | `3` | non |
| `TRADER_SHADOW_DAYS` | Jours d'observation 'shadow' avant 'active' | `7` | non |
| `DISCOVERY_SHADOW_BYPASS` | Bypass shadow si `TRADER_SHADOW_DAYS=0` | `false` | non |
| `DISCOVERY_BACKEND` | `data_api` (default), `goldsky`, `hybrid` | `data_api` | non |
| `GOLDSKY_POSITIONS_SUBGRAPH_URL` | URL subgraph pnl/positions (opt-in) | voir `.env.example` | non |
| `GOLDSKY_PNL_SUBGRAPH_URL` | URL subgraph PnL (opt-in) | voir `.env.example` | non |

## Going live (passage du dry-run au mode réel)

⚠️ **Par défaut `DRY_RUN=true`.** Aucun ordre n'est jamais envoyé sans bascule explicite.

1. **Récupère tes credentials Polymarket** (depuis ton compte connecté à polymarket.com) :
   - `POLYMARKET_PRIVATE_KEY` : ta clé privée Ethereum (jamais commit, jamais partagée).
   - `POLYMARKET_FUNDER` : ton **proxy wallet** (Gnosis Safe créé automatiquement par Polymarket quand tu connectes MetaMask). Tu le trouves sur ton profil Polymarket → settings → "Deposit address" ou "Proxy address".
   - `POLYMARKET_SIGNATURE_TYPE=2` (Gnosis Safe) si tu utilises MetaMask connecté à polymarket.com (cas le plus courant).
2. **Édite `.env`** avec ces 3 valeurs + force un plafond de sécurité strict :
   ```
   POLYMARKET_PRIVATE_KEY=0x<ta_clé_privée>
   POLYMARKET_FUNDER=0x<ton_proxy_address>
   POLYMARKET_SIGNATURE_TYPE=2
   DRY_RUN=false
   MAX_POSITION_USD=1                  # 1 USD max pour ton 1er run réel
   ```
3. **Lance le bot** :
   ```bash
   python -m polycopy
   ```
4. **Surveille** les logs `order_filled` / `order_rejected` ; vérifie chaque transaction sur polymarket.com (onglet "Activity" de ton profil).
5. **Augmente progressivement** `MAX_POSITION_USD` quand tu es satisfait.

Si le bot démarre sans `--dry-run` ET sans clés, il **refuse de démarrer** avec un message clair (`RuntimeError`) — par sécurité.

## Alertes Telegram (optionnel)

Les alertes sont **entièrement optionnelles**. Sans token, le bot log les événements localement et ne POST rien — aucun crash, aucun blocage.

Pour les activer (5 min) :

1. Sur Telegram, cherche `@BotFather` (compte officiel vérifié) → envoie `/newbot`.
2. Choisis un nom (ex: `polycopy local bot`) puis un username finissant par `bot`.
3. BotFather répond avec un token `123456789:ABC...` → copie-le dans `.env` : `TELEGRAM_BOT_TOKEN=...`.
4. Ouvre la conversation de ton bot, envoie-lui `/start`.
5. Ouvre dans un navigateur `https://api.telegram.org/bot<TON_TOKEN>/getUpdates`.
6. Repère `"chat": {"id": 12345678, ...}` → copie dans `.env` : `TELEGRAM_CHAT_ID=12345678`.
7. Redémarre le bot — tu verras `telegram_enabled` dans les logs.

Événements qui déclenchent une alerte :
- `kill_switch_triggered` (CRITICAL) — drawdown ≥ seuil en mode réel.
- `executor_auth_fatal` (CRITICAL) — CLOB auth rejetée.
- `executor_error` (ERROR) — exception SDK / POST ordre.
- `pnl_snapshot_drawdown` (WARNING) — drawdown ≥ 75 % du seuil kill switch.
- `order_filled_large` (INFO) — fill taker ≥ `ALERT_LARGE_ORDER_USD_THRESHOLD`.

Anti-spam : cooldown in-memory de `ALERT_COOLDOWN_SECONDS` par `cooldown_key` (reset au boot).

### M7 : bot Telegram conversationnel (templates, heartbeat, résumé quotidien, digest)

Depuis M7, le bot passe d'une **alarme silencieuse** à un **compagnon structuré** :

- **Startup message** (ON par défaut si token configuré) : à chaque `python -m polycopy`, un message avec version, mode, wallets pinned, modules actifs, lien dashboard.
- **Heartbeat périodique** (opt-in) : toutes les 12 h, un ping "💚 polycopy actif" — utile pour détecter une panne silencieuse (plus d'heartbeat depuis 24 h → process mort).
- **Résumé quotidien** (opt-in, TZ-aware) : à `TG_DAILY_SUMMARY_HOUR` heure locale, un digest des trades 24 h, décisions, ordres, PnL, discovery, alertes.
- **Digest anti-spam** : ≥ `TELEGRAM_DIGEST_THRESHOLD=5` alertes du même type en `TELEGRAM_DIGEST_WINDOW_MINUTES=60` → batch en 1 seul message.
- **Templates Jinja2 soignés** dans `src/polycopy/monitoring/templates/`, **surchargeables** via `assets/telegram/*.md.j2` sans fork (voir `assets/telegram/README.md`).
- **Shutdown message** : à l'extinction propre, un bref "🛑 polycopy arrêté" (durée + version).

Pour tout activer :

```env
TELEGRAM_BOT_TOKEN=<ton_token>
TELEGRAM_CHAT_ID=<ton_chat>
TELEGRAM_STARTUP_MESSAGE=true
TELEGRAM_HEARTBEAT_ENABLED=true
TELEGRAM_DAILY_SUMMARY=true
TG_DAILY_SUMMARY_HOUR=9
TG_DAILY_SUMMARY_TIMEZONE=Europe/Paris
```

Les defaults (`STARTUP_MESSAGE=true`, le reste `false`) garantissent qu'un user M4/M5 qui met à jour `main` sans toucher son `.env` ne sera pas spammé d'un coup. Bot reste **emitter-only** — aucune commande entrante.

Détails et surcharge templates : `docs/setup.md` §16 + `specs/M7-telegram-enhanced.md`.

## Découverte automatique de traders (optionnel, M5)

Polycopy peut découvrir et scorer des wallets Polymarket publics automatiquement, puis promouvoir les meilleurs en cibles actives. **Opt-in strict** : par défaut, le bot ne suit que les wallets listés dans `TARGET_WALLETS`.

⚠️ **Pré-requis bloquant** : lance le backtest avant d'activer en prod.

```bash
python scripts/score_backtest.py \
  --wallets-file specs/m5_backtest_seed.txt \
  --as-of 2026-01-15 \
  --observe-days 30 \
  --output backtest_v1_report.html
```

Tu dois obtenir une corrélation Spearman ≥ 0.30 entre `score_at_T` et `observed_roi_t_to_t30`. Sinon → ne pas activer M5 en prod (la formule v1 sous-performe, ouvrir une issue pour itérer en `SCORING_VERSION=v2`).

### Activation

```env
DISCOVERY_ENABLED=true
DISCOVERY_INTERVAL_SECONDS=21600   # 6 h
MAX_ACTIVE_TRADERS=10              # plafond dur
TRADER_SHADOW_DAYS=7               # observation avant promotion
SCORING_VERSION=v1
```

### Comment ça marche

1. Toutes les `DISCOVERY_INTERVAL_SECONDS`, M5 scanne les top-holders des marchés Polymarket actifs (`/holders` fan-out sur les 20 marchés les plus liquides) + le feed global `/trades`.
2. Pour chaque candidat, fetch `/positions` + `/activity`, calcule un score ∈ [0, 1] avec la formule v1 : `0.30·win_rate + 0.30·roi_norm + 0.20·diversity + 0.20·volume_norm`.
3. Wallets avec score ≥ `SCORING_PROMOTION_THRESHOLD` passent en `status='shadow'` (observation `TRADER_SHADOW_DAYS` jours) puis promus en `status='active'` (le watcher les copie).
4. Wallets `active` avec score < `SCORING_DEMOTION_THRESHOLD` pendant `SCORING_DEMOTION_HYSTERESIS_CYCLES` cycles consécutifs passent en `paused` (plus copiés).
5. Tes `TARGET_WALLETS` restent **`pinned`** — jamais retirés par M5, immuables.

Le `.env` n'est **pas** modifié automatiquement ; tous les wallets auto-découverts vivent en DB (`target_traders.status`). Tu gardes le contrôle par `MAX_ACTIVE_TRADERS` (cap dur) + `BLACKLISTED_WALLETS` (exclusion absolue) + édition manuelle SQL si besoin.

### Observer M5 en live

Dashboard M4.5 doit être actif. Ouvre `http://127.0.0.1:8787/traders` : table avec scores, statuts, timestamps. Auto-refresh 10 s.

`http://127.0.0.1:8787/backtest` : statut du rapport backtest.

Logs structurés émis par cycle : `discovery_starting`, `discovery_cycle_started`, `discovery_candidates_built`, `score_computed`, `trader_promoted`, `trader_demoted`, `discovery_cycle_completed`, `discovery_stopped`.

Alertes Telegram (si config M4 active) : `trader_promoted` (INFO), `trader_demoted` (WARNING), `discovery_cap_reached` (WARNING), `discovery_cycle_failed` (ERROR).

## Dashboard local (optionnel, M4.5 + M6)

Dashboard web **read-only** pour superviser live détections, décisions, ordres, positions, PnL et traders. M6 (2026) : refonte UX moderne — sidebar persistante, cards KPI avec sparkline SVG, jauge score SVG sur la page Traders, area chart + overlay drawdown + timeline milestones sur PnL, footer health Gamma/Data API. Dark-first avec toggle light persistant.

Opt-in via `.env` :

```
DASHBOARD_ENABLED=true
DASHBOARD_HOST=127.0.0.1   # ⚠️ localhost-only par défaut — ne change que si tu sais
DASHBOARD_PORT=8787
DASHBOARD_THEME=dark            # ou "light"
DASHBOARD_POLL_INTERVAL_SECONDS=5
```

Lance le bot puis ouvre `http://127.0.0.1:8787/` dans ton navigateur.

Pages : Home (KPIs + sparklines + Discovery + derniers trades) · Détection · Stratégie · Exécution · Positions · PnL (area chart + milestones) · Traders (jauge score SVG) · Backtest · Logs (stub M9).

Aucune action d'écriture n'est exposée — uniquement des `SELECT`. Pas d'auth : le bind localhost suffit pour un bot mono-utilisateur local. **Changer `DASHBOARD_HOST` à `0.0.0.0` expose le dashboard sur tout le LAN : à tes risques.** Aucun secret (clé privée, token Telegram, creds CLOB) n'apparaît dans le HTML/JSON rendu — vérifié par grep automatisé.

## Rapport PnL

Le writer écrit un snapshot en DB toutes les `PNL_SNAPSHOT_INTERVAL_SECONDS` (5 min par défaut). Pour générer un rapport HTML lisible avec sparkline SVG :

```bash
source .venv/bin/activate
python scripts/pnl_report.py --since 7 --output html
# → génère pnl_report.html, ouvrir dans un navigateur
```

Autres formats : `--output stdout` (table plain text) ou `--output csv`. Par défaut les snapshots `is_dry_run=true` sont filtrés (utilise `--include-dry-run` pour les inclure).

## Structure du repo

```
polycopy/
├── src/polycopy/
│   ├── watcher/          # Détection des trades on-chain
│   ├── strategy/         # Filtres, sizing, risk pipeline
│   ├── executor/         # CLOB orders signés (avec dry-run safeguards)
│   ├── storage/          # SQLAlchemy models + repositories
│   ├── monitoring/       # Telegram, snapshots PnL, kill switch (M4)
│   ├── dashboard/        # FastAPI + HTMX + Chart.js, localhost-only (M4.5)
│   ├── config.py         # Pydantic Settings
│   └── __main__.py       # Entrypoint asyncio
├── alembic/              # Migrations DB (M4+)
├── tests/                # Tests unit (mocks) + integration (opt-in réseau réel)
├── specs/                # Specs autoritaires par milestone
├── scripts/              # bash scripts/setup.sh, pnl_report.py
├── docs/                 # architecture.md, setup.md
└── assets/               # Logos, screenshots
```

## Commandes utiles

```bash
pytest                                          # tests unitaires (mocks, pas de réseau)
pytest -m integration                           # tests réseau réels (opt-in, lents)
ruff check . && ruff format .                   # lint + format
mypy src                                        # type check strict
python -m polycopy --dry-run                    # bot en mode safe
```

## Roadmap

- [x] **M1** : Watcher + Storage (détection + persistance)
- [x] **M2** : Strategy Engine (filtres + sizing pipeline)
- [x] **M3** : Executor (signature CLOB + POST, dry-run par défaut)
- [x] **M4** : Monitoring (Telegram, snapshots PnL, kill switch, Alembic, rapport HTML)
- [x] **M4.5** : Dashboard local (FastAPI + HTMX + Chart.js, read-only, opt-in)
- [x] **M5** : Scoring de traders + sélection automatique (opt-in, read-only)
- [x] **M6** : Dashboard 2026 (refonte UX, sidebar, cards KPI, jauge score, timeline PnL)
- [x] **M7** : Bot Telegram enrichi (startup, heartbeat, résumé quotidien, templates, digest)

### Après M7 (roadmap UX/expérience, pas de nouveau module fonctionnel)

- Mode `--dry-run` "semi-réel" : simule les fills sur la profondeur orderbook
  comme s'il postait pour de vrai, de sorte à observer la perf sur 2-3 jours sans
  capital engagé
- README plus accueillant (tutorial interactif, captures, comparaison avec
  d'autres bots Polymarket)

## Avertissement

Les marchés prédictifs sont risqués. Les performances passées d'un trader ne garantissent rien.

Polymarket est inaccessible depuis certaines juridictions (notamment les États-Unis). **Renseigne-toi sur le cadre légal applicable chez toi avant de l'utiliser.**

Ce code est fourni à titre éducatif. **Aucune garantie sur le fonctionnement, la sécurité ou la rentabilité.** Les bugs peuvent coûter du capital réel — toujours commencer en `DRY_RUN=true`, puis avec un `MAX_POSITION_USD` minuscule.
