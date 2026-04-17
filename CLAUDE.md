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
- **Logs structurés** via `structlog`, jamais de `print()` en dehors des scripts CLI

## Architecture (rappel)

```
src/polycopy/
├── watcher/      Détection trades on-chain (Data API polling)
├── strategy/     Filtres, sizing, risk manager
├── executor/     Construction & envoi ordres CLOB
├── storage/      Models SQLAlchemy + repositories
├── monitoring/   Logs, metrics, alertes
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

## Sécurité — RÈGLES STRICTES

- **JAMAIS** committer `.env`, clé privée, ou API credentials (vérifier `.gitignore`)
- La clé privée vit uniquement dans une env var, jamais en dur dans le code, jamais loggée
- `polymarket_private_key` et `polymarket_funder` sont **optionnels** au niveau config — ils ne sont consommés que par l'Executor (M3), qui devra refuser de démarrer si `DRY_RUN=false` et l'une des deux est absente.
- Tous les ordres passent par le `RiskManager.check()` avant `OrderExecutor.send()`. Pas d'exception.
- Le mode `--dry-run` doit être respecté partout : si `settings.dry_run is True`, l'executor log l'ordre mais ne l'envoie pas
- Le kill switch (`KILL_SWITCH_DRAWDOWN_PCT`) coupe tout : ferme le watcher, n'envoie plus d'ordres, alerte Telegram
- À M2 la strategy est **read-only** (Gamma + CLOB midpoint, pas de signature, pas de POST). `settings.dry_run` n'a pas d'effet sur la strategy. Le garde-fou `dry_run` kicks in à M3 quand l'Executor le lit avant d'envoyer un ordre.

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
