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

- **Logs** : `structlog` JSON, tous les events importants (trade détecté, filtré, exécuté, erreur)
- **Métriques** : compteurs simples en mémoire exposés via un endpoint HTTP `/metrics` au format Prometheus (optionnel)
- **Alertes Telegram** : envoi async sur events critiques (kill switch, ordre échoué, gros gain/perte)
- **Dashboard PnL** : script `scripts/pnl_report.py` qui lit `pnl_snapshots` et génère un rapport HTML

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
