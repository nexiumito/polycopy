# polycopy

Bot de copy trading pour Polymarket. Surveille l'activité on-chain de wallets cibles et réplique leurs trades sur ton propre wallet, avec sizing, filtres et risk management.

> **Statut** : prototype personnel. Pas un produit. Pas de garantie. Trade à tes risques.

## Pourquoi

Polymarket est entièrement on-chain (Polygon). L'activité de chaque wallet est publique et accessible via la Data API. Un bot peut donc détecter en quasi-temps-réel les trades d'un "smart money" wallet et les répliquer.

## Architecture

5 couches, faiblement couplées :

1. **Sources** — Data API (`/activity`), CLOB WebSocket (prix), Gamma API (métadonnées)
2. **Watcher** — polling des wallets cibles, déduplication, persistance
3. **Strategy** — filtres marché, sizing, slippage check, risk manager
4. **Executor** — construction et signature d'ordres via `py-clob-client`
5. **Monitoring** — logs structurés, métriques, alertes Telegram

Voir `docs/architecture.md` pour le détail.

## Stack

- Python 3.11+ (asyncio)
- `py-clob-client` (SDK officiel Polymarket)
- `polymarket-apis` (wrapper unifié CLOB + Gamma + Data + WS, Pydantic)
- `httpx`, `websockets`
- SQLite + SQLAlchemy 2.0 (Postgres possible plus tard)
- Pydantic Settings pour la config
- pytest pour les tests

## Setup rapide

Environnement de référence : **WSL Ubuntu (bash)**, repo cloné en chemin Linux natif (`~/code/polycopy`). Un seul script bootstrape tout (idempotent) :

```bash
bash scripts/setup.sh
```

Il crée le `.venv/`, installe les deps, copie `.env.example` → `.env`, applique le patch config §0.5 de M1, et lance un smoke test `python -m polycopy --dry-run`.

Ensuite, à chaque nouvelle session :

```bash
source .venv/bin/activate
python -m polycopy --dry-run   # aucun ordre envoyé
# python -m polycopy           # réel — ATTENTION à ton capital
```

Guide complet pas-à-pas (install WSL, édition `.env`, troubleshooting) : [docs/setup.md](docs/setup.md).

## Variables d'environnement

| Variable | Description | Exemple |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | Clé privée du wallet de signature | `0x...` |
| `POLYMARKET_FUNDER` | Adresse du proxy wallet (si Magic/email) | `0x...` |
| `POLYMARKET_SIGNATURE_TYPE` | 0 (EOA), 1 (Magic), 2 (Gnosis Safe) | `1` |
| `TARGET_WALLETS` | Wallets à copier (CSV) | `0xabc...,0xdef...` |
| `COPY_RATIO` | Fraction du trade à répliquer | `0.01` |
| `MAX_POSITION_USD` | Plafond par position | `100` |
| `MIN_MARKET_LIQUIDITY_USD` | Liquidité minimum d'un marché | `5000` |
| `MIN_HOURS_TO_EXPIRY` | Skip les marchés trop proches de l'expiration | `24` |
| `MAX_SLIPPAGE_PCT` | Slippage max accepté vs prix original | `2.0` |
| `KILL_SWITCH_DRAWDOWN_PCT` | Stop tout si drawdown > X% | `20` |
| `DATABASE_URL` | URL DB | `sqlite:///polycopy.db` |
| `TELEGRAM_BOT_TOKEN` | Optionnel | |
| `TELEGRAM_CHAT_ID` | Optionnel | |

## Structure du repo

```
polycopy/
├── src/polycopy/
│   ├── watcher/          # Détection des trades on-chain
│   ├── strategy/         # Filtres, sizing, risk
│   ├── executor/         # CLOB orders
│   ├── storage/          # SQLAlchemy models, repos
│   ├── monitoring/       # Logs, metrics, alerts
│   ├── config.py         # Pydantic Settings
│   └── __main__.py       # Entrypoint
├── tests/
├── scripts/              # Outils ponctuels (backtest, scoring)
├── docs/
└── .claude/              # Commands & config Claude Code
```

## Commandes utiles

```bash
# Tests
pytest

# Lint + format
ruff check . && ruff format .

# Type check
mypy src

# Backtest sur un wallet historique
python scripts/backtest.py --wallet 0x... --from 2026-01-01

# Scorer les top traders du leaderboard
python scripts/score_traders.py --window 30d --min-volume 50000
```

## Roadmap

- [ ] M1 : Watcher + Storage (détection + persistance, pas d'exécution)
- [ ] M2 : Strategy Engine (filtres + sizing, dry-run)
- [ ] M3 : Executor (vrais ordres, mode test sur micro-capital)
- [ ] M4 : Monitoring (Telegram, dashboard PnL)
- [ ] M5 : Scoring de traders + sélection automatique

## Avertissement

Les marchés prédictifs sont risqués. Les performances passées d'un trader ne garantissent rien. Polymarket est inaccessible depuis certaines juridictions (US notamment). Renseigne-toi sur le cadre légal applicable chez toi avant de l'utiliser. Ce code est fourni à titre éducatif.
