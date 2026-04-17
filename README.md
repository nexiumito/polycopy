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
5. **Monitoring** — logs structlog JSON ; Telegram + dashboard à venir (M4).

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
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Alertes (M4 — pas encore actif) | — | non |

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

## Structure du repo

```
polycopy/
├── src/polycopy/
│   ├── watcher/          # Détection des trades on-chain
│   ├── strategy/         # Filtres, sizing, risk pipeline
│   ├── executor/         # CLOB orders signés (avec dry-run safeguards)
│   ├── storage/          # SQLAlchemy models + repositories
│   ├── monitoring/       # (M4 — alertes Telegram, dashboard)
│   ├── config.py         # Pydantic Settings
│   └── __main__.py       # Entrypoint asyncio
├── tests/                # Tests unit (mocks) + integration (opt-in réseau réel)
├── specs/                # Specs autoritaires par milestone
├── scripts/              # bash scripts/setup.sh (bootstrap idempotent)
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
- [ ] **M4** : Monitoring (Telegram, dashboard PnL, snapshots PnL périodiques)
- [ ] **M5** : Scoring de traders + sélection automatique

## Avertissement

Les marchés prédictifs sont risqués. Les performances passées d'un trader ne garantissent rien.

Polymarket est inaccessible depuis certaines juridictions (notamment les États-Unis). **Renseigne-toi sur le cadre légal applicable chez toi avant de l'utiliser.**

Ce code est fourni à titre éducatif. **Aucune garantie sur le fonctionnement, la sécurité ou la rentabilité.** Les bugs peuvent coûter du capital réel — toujours commencer en `DRY_RUN=true`, puis avec un `MAX_POSITION_USD` minuscule.
