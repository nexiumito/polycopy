# MK — Pipeline latency phase 1b (WSS market detection + counters)

**Priorité** : 🟠 P2 (important, parallélisable)
**Charge estimée** : M (3-4 jours)
**Branche suggérée** : `feat/wss-market-detection`
**Prérequis** : aucun (indépendant de MA/MB/MC/MD)
**Bloque** : améliore MF (détection plus rapide → plus de trades capturés pour scoring v2.2)

---

## 1. Objectif business

Faire passer la latence de détection de trade `source_wallet → local DB` de **p50 8-20s (observed 2026-04-24)** à **p50 2-4s** en remplaçant le polling REST `/activity` par la WSS CLOB `market` channel pour la couche détection (déjà utilisée pour SlippageChecker M11). Gain immédiat : moins de slippage vs source wallet fill price (compétition frontrunner 5-10s window réduite). Fix en parallèle les 2 bugs de comptage latence observés (`filtered > enriched`, `watcher_detected_ms` p99 44min conflation backfill/realtime). Ajoute la gestion HTTP 425 matching engine restart pour robustesse ops.

## 2. Contexte & problème observé

### Observations runtime 2026-04-24

- Dashboard `/latence` affiche : `watcher_detected_ms` **p50=13.4s, p95=44.2s, p99=2,675s (44min)** → p99 aberrant.
- `strategy_filtered_ms count=27493 > strategy_enriched_ms count=27418` → théoriquement impossible (filtered = sous-ensemble de enriched).
- Funnel latence : enriched 27418 → filtered 27493 (+75) → sized 13153 (-52%) → risk_checked 1556 (-88%) → submitted 1169. L'inversion enriched/filtered fausse les pourcentages.
- Pipeline current : WalletPoller poll `/activity` toutes les `POLL_INTERVAL_SECONDS=5s` → propagation Data API 1-5s → pipeline 500ms → total 6-10s floor + queue bursts → p50 observé 8-20s.

### Findings référencés

- **[F43] 🟢 3/3** (synthèse §4.2) : WS CLOB `market` > REST polling. **Claude §9 item 9** : "market-channel WSS is **free, already mostly-native in py-clob-client-style libraries**, fits single-process asyncio cleanly via websockets or aiohttp.ws". **Gemini §4.1** : "Shifting data ingress strictly to the RTDS WebSocket will instantly compress data ingress latency to ~100ms". **Perplexity D1** : "WSS Quickstart formalizes CLOB vs RTDS split, introduces market and user channels, strongly pushes discovery pipelines toward WebSocket rather than REST polling".

- **[F42] 🟢 3/3** : floor pratique 250-350ms e2e. Validation que 2-4s target MK est réaliste (bien au-dessus du floor, large marge pour python processing + queue).

- **[F33] ⚠️ contradiction résolue** : WSS `user` channel INADAPTÉ copy-trading. **Claude §7.1** : "Polymarket does not support unsubscribing from channels once subscribed. Subscription is by `markets` (condition IDs) for user channel — meaning **you subscribe to market IDs, and receive events for all your orders in those markets**. You cannot subscribe 'to a wallet' — only to your own wallet's orders. **This breaks the copy-trading use case entirely for the user channel.**". Conclusion : utiliser le channel `market` + filtrer côté client par `maker`/`taker` fields.

- **[F32] 🟡** : WSS market channel pour discovery/detection = bonne idée, déjà partiellement en place M11 pour SlippageChecker. Extension à la détection = MK core.

- **Audit [H-001]** : "strategy_filtered_ms toujours émis même sur early-reject → `count(filtered) > count(enriched)` légal". Location [src/polycopy/strategy/orchestrator.py:128-153](../../src/polycopy/strategy/orchestrator.py#L128-L153) + [pipeline.py:280-335](../../src/polycopy/strategy/pipeline.py#L280-L335). **Cause racine identifiée par audit** : `strategy_filtered_ms` wrappé en `finally` autour de `run_pipeline` → émis même si reject précoce par `TraderLifecycleFilter` ou `EntryPriceFilter`. `strategy_enriched_ms` (stage MarketFilter) émis seulement si boucle atteint le 2e filtre.

- **Backlog** [docs/backlog.md](../../docs/backlog.md) : `watcher_detected_ms` p99 trompeur. Conflate vraie latence temps-réel **et** rattrapage backlog historique d'un wallet fraîchement promu. Fix retenu (option 1) : split en `watcher_realtime_detected_ms` (trades < 5 min) + `watcher_backfill_duration_ms` (cycle total).

- **[F51] 🔵 Perplexity unique** (synthèse §4.1) : Matching engine restart **Tuesday 7AM ET ~90s**. HTTP 425 (Too Early) à gérer avec exponential backoff. Référence [docs.polymarket.com/trading/matching-engine](https://docs.polymarket.com/trading/matching-engine) + [py-clob-client issue 286](https://github.com/Polymarket/py-clob-client/issues/286).

### Session originale mappée

**Session D brouillon** (`docs/bug/session_D_pipeline_metrics_and_ops.md`) items D1 (split watcher) + D2 (filtered>enriched bug) intégrés ici. D3 (shutdown graceful) + D4 (setup script) + D5 (DB queries docs) migrent en **MI** ops hygiene.

### Ce que MK NE fait PAS

- **Pas de migration VPS Dublin** : on reste sur le PC uni-debian. La migration geographical est un item hors scope (Claude §7.3 : Option (d) — accepter 2-3s floor et investir en scoring est le choix optimal pour notre capital $1k-10k political/macro). Si besoin futur, spec dédiée.
- **Pas de RTDS Real-Time Data Stream** : l'endpoint WSS CLOB `market` suffit pour nos besoins. RTDS = alternative payante pour market makers HFT, pas pertinent polycopy.
- **Pas de Polygon RPC eth_subscribe** : Claude §7.1 (c) démontre que c'est worse than (a) pour copy-trading spécifiquement (on_chain settlement trop tard, proxyWallet vs funder context manquant).

## 3. Scope (items détaillés)

### MK.1 — Étendre `ClobMarketWSClient` à la couche détection

- **Location** : [src/polycopy/strategy/clob_ws_client.py](../../src/polycopy/strategy/clob_ws_client.py) (déjà M11 pour SlippageChecker) + [src/polycopy/watcher/wallet_poller.py](../../src/polycopy/watcher/wallet_poller.py) + [src/polycopy/watcher/orchestrator.py](../../src/polycopy/watcher/orchestrator.py)
- **Ce qu'il faut faire** :
  - **Décision D1** : réutiliser `ClobMarketWSClient` existant (pas créer de nouveau client). Ajouter un consommateur dédié `TradeEventConsumer` qui écoute les events `trade` du channel market et émet des `DetectedTrade` DTOs.
  - Nouvelle méthode `ClobMarketWSClient.subscribe_to_trade_events(asset_ids: list[str], callback: Callable[[TradeEvent], None])`.
  - Lister les `asset_ids` à souscrire : union des markets actifs pour les wallets watchés. Dérivé de `MyPosition.asset_id` (positions ouvertes) ∪ `detected_trades.asset_id` last 24h.
  - **Décision D2** : cap dur `STRATEGY_CLOB_WS_MAX_SUBSCRIBED_DETECT=500` (même cap que le cache existant). Si dépassé, LRU eviction (déjà en place M11).
  - Côté `WalletPoller` : mode **hybrid** par défaut : le WSS est la source primaire (détection temps réel), Data API polling reste actif en `POLL_INTERVAL_SECONDS=30s` (au lieu de 5s) comme safety net pour les trades qui seraient manqués par le WSS (déconnexions, backfill historique sur wallets fraîchement promus M5_ter).
  - **Filter côté client** : pour chaque `TradeEvent`, vérifier si `maker` ou `taker` ∈ `list_wallets_to_poll()`. Si oui → émettre `DetectedTrade` vers le strategy pipeline (émit sur la même Queue que Data API path).
  - Dedup par `transactionHash` (clé unique DB déjà en place M1).
  - Fallback tenacity : si WSS down > 30s, re-activer `WalletPoller` polling rapide 5s comme fallback temporaire. Auto-revert au WSS primary à reconnexion.
- **Tests requis** :
  - `test_clob_ws_subscribe_trade_events_filters_by_wallet`
  - `test_clob_ws_deduplicates_by_tx_hash`
  - `test_wallet_poller_hybrid_mode_wss_primary_rest_safety_net`
  - `test_wallet_poller_fallback_on_wss_outage`
  - `test_wallet_poller_reverts_to_wss_on_reconnect`
  - `test_clob_ws_asset_subscription_list_derives_from_active_markets`
- **Sources deep-search** : Claude §7.1 (a) + Gemini §4.1 + Perplexity D1 + F33 arbitrage.
- **Charge item** : 2 jours

### MK.2 — Split `watcher_detected_ms` en realtime + backfill

- **Location** : [src/polycopy/watcher/wallet_poller.py:94-98](../../src/polycopy/watcher/wallet_poller.py#L94-L98) + [src/polycopy/storage/repositories.py](../../src/polycopy/storage/repositories.py) (LatencyRepository) + [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) (latency route)
- **Ce qu'il faut faire** :
  - Migration Alembic 0009 : ajouter stage names dans `trade_latency_samples.stage_name` enum (string-based, pas vrai enum SQL pour SQLite). Pas de migration structurelle, juste nouveaux valeurs acceptées.
  - Nouveau stage `watcher_realtime_detected_ms` : `now() - trade.timestamp` **uniquement si** `now() - trade.timestamp < 300s` (5 min). Trades plus anciens = backfill, pas realtime.
  - Nouveau stage `watcher_backfill_duration_ms` : mesure **la durée totale du cycle `get_trades()`** (pas la latence du trade individuel) lorsqu'un wallet est fraîchement promu et qu'un backlog historique est chargé. Émit 1× par cycle de backfill, pas par trade.
  - Dépréciation `watcher_detected_ms` legacy : conserver les samples historiques, mais stopper l'écriture de nouveaux. Dashboard doit filtrer.
  - Dashboard `/latency` affiche désormais 7 stages (les 6 existants + `watcher_realtime_detected_ms`) et un panel dédié "Backfill events" listant les `watcher_backfill_duration_ms` récents.
  - **Attention** : post-M5_ter watcher live-reload, le backfill arrive à chaque nouveau wallet promu. Fréquence potentielle : 1-5 backfills par jour (promotions). Volume `watcher_backfill_duration_ms` reste low-count.
- **Tests requis** :
  - `test_watcher_emits_realtime_ms_for_recent_trades`
  - `test_watcher_emits_backfill_duration_on_new_wallet_promotion`
  - `test_watcher_does_not_emit_realtime_for_old_trades`
  - `test_dashboard_latency_shows_split_stages`
  - `test_migration_0009_new_stage_names_accepted` (si migration structurelle sur enum — sinon skippable)
- **Sources** : Backlog docs/backlog.md §3 "Latence watcher" + audit D1 + F33.
- **Charge item** : 1 jour

### MK.3 — Fix `strategy_filtered_ms count > strategy_enriched_ms count`

- **Location** : [src/polycopy/strategy/orchestrator.py:128-153](../../src/polycopy/strategy/orchestrator.py#L128-L153) + [src/polycopy/strategy/pipeline.py:280-335](../../src/polycopy/strategy/pipeline.py#L280-L335)
- **Ce qu'il faut faire** :
  - Audit [H-001] cause racine : `strategy_filtered_ms` wrappé en `finally` autour de `run_pipeline` → émis même sur early-reject.
  - **Fix** : émettre `strategy_filtered_ms` **uniquement si** la boucle pipeline atteint au moins le `MarketFilter` (stage 2). Les early-rejects du stage 1 (`TraderLifecycleFilter`) n'émettent pas `filtered_ms`.
  - **Alternative** (recommandé Claude §H-001) : renommer `strategy_filtered_ms` en `strategy_pipeline_total_ms` et documenter que c'est la durée totale du pipeline **quelque soit** le stage où il s'est arrêté. Plus honnête sémantiquement.
  - **Décision D3** : prendre l'alternative (rename). Invariant préservé : `count(pipeline_total_ms) ≥ count(enriched_ms) ≥ count(filtered_ms) ≥ count(sized_ms) ≥ count(risk_checked_ms)`. Monotone décroissant strict.
  - Migration dashboard `/latency` : afficher les 7 stages dans l'ordre de pipeline correct.
- **Tests requis** :
  - `test_pipeline_total_ms_count_monotonic_decreasing_with_stages`
  - `test_early_reject_stage1_does_not_emit_filtered_ms`
  - `test_pipeline_total_ms_emitted_on_every_trade`
  - `test_dashboard_latency_renames_filtered_to_pipeline_total`
- **Sources** : Audit H-001 + Session D D2.
- **Charge item** : 0.5 jour

### MK.4 — Gestion HTTP 425 matching engine restart

- **Location** : [src/polycopy/executor/clob_write_client.py](../../src/polycopy/executor/clob_write_client.py) + [src/polycopy/executor/pipeline.py](../../src/polycopy/executor/pipeline.py)
- **Ce qu'il faut faire** :
  - Perplexity D1 : "Matching engine restart Tuesday 7AM ET ~90s, returns HTTP 425 (Too Early) on order endpoints".
  - Dans `ClobWriteClient.post_order()` : catch `httpx.HTTPStatusError` si `status_code == 425`, appliquer exponential backoff tenacity (déjà en place dans d'autres clients). Retry max 6 fois avec backoff 15s, 30s, 60s, 120s, 240s, 480s (total ~15 min — couvre le restart 90s + marge).
  - Log structlog `clob_write_client_engine_restart_backoff` avec `retry_count`, `next_wait_s`. Niveau INFO (pas WARNING — c'est prévu).
  - Alerter Telegram INFO `matching_engine_restart_detected` au premier HTTP 425 reçu (once per 24h), pour traçabilité ops.
  - **Attention** : pendant le restart, le path dry-run M8 `_persist_realistic_simulated` **n'appelle pas CLOB write** (read-only `/book`). Pas d'impact dry-run, seulement live.
  - Pour le path **read** (Gamma, `/midpoint`, `/book`) — si 425, même backoff pattern.
- **Tests requis** :
  - `test_clob_write_client_retries_on_http_425`
  - `test_clob_write_client_emits_telegram_alert_once_per_day`
  - `test_clob_write_client_eventually_succeeds_after_engine_restart`
- **Sources** : Perplexity D1 + Session D (new item) + Claude §12 key discrepancy.
- **Charge item** : 0.5 jour

### MK.5 — Instrumentation comparative WSS vs REST latency

- **Location** : [src/polycopy/watcher/wallet_poller.py](../../src/polycopy/watcher/wallet_poller.py) + [src/polycopy/dashboard/templates/latency.html](../../src/polycopy/dashboard/templates/latency.html)
- **Ce qu'il faut faire** :
  - Ajouter 2 nouveaux sub-stages dans `watcher_realtime_detected_ms` : `detection_path: Literal["wss", "rest"]` (tag structlog).
  - Dashboard `/latency` ajoute toggle "WSS vs REST comparison" qui plot les deux distributions côte-à-côte.
  - Utile pour **valider empiriquement** que WSS amène bien le gain attendu post-ship.
  - Réponse directe à Q2 (synthèse §11) : "quelle est la vraie latence WSS market channel end-to-end pour polycopy ?".
- **Tests requis** :
  - `test_detection_path_tag_wss`
  - `test_detection_path_tag_rest`
  - `test_dashboard_latency_toggles_wss_vs_rest`
- **Sources** : Q2 synthèse §11 + F43.
- **Charge item** : 0.5 jour

## 4. Architecture / décisions clefs

- **D1** : réutiliser `ClobMarketWSClient` existant (pas créer nouveau client). Justification : déjà robuste M11 (reconnect tenacity, watchdog, LRU cap), cohérent invariant single-process.
- **D2** : cap `STRATEGY_CLOB_WS_MAX_SUBSCRIBED_DETECT=500` (même cap que cache slippage). Justification : probable overlap des `asset_ids` (même markets utilisés pour slippage et detection).
- **D3** : renommer `strategy_filtered_ms` → `strategy_pipeline_total_ms`. Justification : sémantique correcte sans breaking change invasif. Dashboard gère le rename.
- **D4** : mode hybride WSS primary + REST safety net (pas pure WSS). Justification : résilience aux déconnexions WSS, compat M5_ter live-reload (nouveaux wallets peuvent nécessiter backfill REST initial), pas de régression fonctionnelle.
- **D5** : `watcher_backfill_duration_ms` émis 1× par cycle (pas par trade). Justification : c'est une métrique ops, pas une métrique per-trade. Cohérent avec backlog docs/backlog.md "audit pur, pas comparé aux autres stages".
- **D6** : backoff HTTP 425 exponential tenacity max 6 retries (~15 min total). Justification : restart engine ~90s, marge x10 pour imprévus, stop avant de spammer.

## 5. Invariants sécurité

- **Triple garde-fou M3 + 4ᵉ M8** : intact. MK touche uniquement couche détection (watcher) + instrumentation + retry write. Aucune nouvelle surface signature.
- **WSS CLOB `market` = public read-only** : cohérent M11. Aucune creds consommée.
- **Zéro secret loggé** : les nouveaux events structlog (`trade_event_received_wss`, `detection_path=wss`) n'incluent que wallets publics + numeric. Test grep `test_no_secret_leak_in_me_logs`.
- **M5_ter watcher live-reload compat** : le cycle `list_wallets_to_poll()` continue à tourner, émet des updates au consommateur WSS et au REST fallback simultanément. Aucune régression lifecycle M5_bis.
- **Dedup tx_hash préservée** : les trades arrivant via WSS et REST sur le même `tx_hash` ne sont comptabilisés qu'une fois en DB (contrainte unique M1).

## 6. Hypothèses empiriques à valider AVANT ship

- **H-EMP-4** (synthèse §8) : post-ship MK, latence p50 détection passe de 8-20s à 2-4s. **Méthode** : post-ship, 24h d'observation, comparer p50 `watcher_realtime_detected_ms` pre/post. **Seuil go** : p50 post-ship < 5s. Si ≥5s, investiguer (WSS lag, WSL overhead).
- **H-EMP-6** (synthèse §8, Q2) : latence WSS market channel réelle sur notre stack. **Méthode** : instrument `t_ws_message_received → t_detected_trade_persisted`. **Seuil informatif** : si > 1s, le gain MK est mangé par internal processing. Si < 200ms, marge excellente.
- **Q1** (synthèse §11) : "250ms taker delay" vs "250-300ms matching latency" — instrumenter `t_order_sent → t_order_confirmed` sur 100 FOK orders live (post-MK.4 qui ajoute le retry context). Seuil informatif : si delta 250ms stable, c'est probablement le même phénomène. Si très variable, plus complexe.

## 7. Out of scope

- **Migration VPS Dublin / Frankfurt** : hors scope. Claude §7.3 recommande Option (d) — accepter 2-3s floor, invest en scoring. Si besoin futur, spec dédiée.
- **RTDS Real-Time Data Stream intégration** : alternative payante Polymarket (Perplexity C5). Hors scope, WSS market channel suffit.
- **Polygon RPC eth_subscribe OrderFilled** : Claude §7.1 (c) démontre inadéquation copy-trading (settlement on-chain trop tard). Hors scope.
- **Goldsky Turbo Pipelines webhook** : alternative Claude §7.1 (b), coût ~$50/mo, hors scope tant que WSS market suffit.
- **Parallelization strategy pipeline (phase 2)** : si post-MK.1-5 on mesure p95 > 4s, futur M17+. Claude §7.3 : "add market-channel WSS (item 9) only if measured post-fix residual p50 exceeds ~4s".
- **Shutdown graceful timeout 10s** : migre en **MI** (ops hygiene, même famille D3 Session D).
- **HTTP 425 alerting sophistiqué** (histograms, SLA tracking) : v1 simple log + one-time Telegram suffit.

## 8. Success criteria

1. **Tests ciblés verts** : ~14 nouveaux tests unit + 2 integration.
2. **Latence p50 détection post-ship** : sur 24h d'observation, `watcher_realtime_detected_ms` p50 < **5s** (idéalement < 3s).
3. **p99 détection non-aberrant** : `watcher_realtime_detected_ms` p99 < **30s** (vs 44min actuel conflation). `watcher_backfill_duration_ms` reste un metric séparé.
4. **Counter invariants restaurés** : `count(pipeline_total_ms) ≥ count(enriched_ms)` strictement sur 24h samples.
5. **WSS stable** : post-ship, WSS connection uptime > 95% sur 7 jours (mesuré via `ws_connection_status` existant M11).
6. **HTTP 425 handled** : E2E test simulé → post-MK.4, bot survit à un restart engine simulé sans crash, retry propre.
7. **Dashboard `/latency` migré** : affiche 7 stages, panel backfill séparé, toggle WSS vs REST.

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MK.1 | — | D (new major) | F43 (3/3), Claude §9 item 9 + §7.1 | #12 |
| MK.2 | — | D (D1) | Backlog docs/backlog.md §3 | #14 |
| MK.3 | [H-001] | D (D2) | Claude §6 audit mapping | #13 |
| MK.4 | — | D (new) | Perplexity D1 + F51 | #26 |
| MK.5 | — | D (new) | Q2 synthèse §11 | — |

## 10. Prompt de génération de spec

````markdown
# Contexte

Lis `docs/next/MK.md` en entier. C'est le brief actionnable du module MK
(Pipeline latency phase 1b). WSS CLOB market channel extension à la couche
détection + fix counters latence + HTTP 425 backoff.

# Tâche

Produire `docs/specs/M19-latency-phase-1b.md` suivant strictement le format
des specs M1..M17 existantes.

Numéro : M19 (après MA=M14, MB=M15, MC=M16, MD=M17, M18=V2 migration). Note : la lettre `ME` a été utilisée pour le bundle V2 migration M18 ; ce module est désormais identifié `MK` pour éviter la collision.

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Conventions (WSS market = public read-only, cohérent M11) +
  §Sécurité (pas de creds sur WSS market)
- `docs/specs/M11-realtime-pipeline-phase1.md` comme référence contenu WSS CLOB
  (déjà en place pour SlippageChecker, à étendre)
- `docs/specs/M13_dry_run_observability_spec.md` comme template de forme
- `docs/backlog.md` §3 "Latence watcher_detected_ms" (cause racine + fix option 1)
- Audit H-001 (filtered > enriched)
- Synthèse §4.2 F43 WSS > REST + §4.3 F32+F33 channel market vs user
- Perplexity D1 (matching engine restart HTTP 425)

# Contraintes

- Lecture seule src/ + docs
- Écriture uniquement `docs/specs/M19-latency-phase-1b.md`
- Longueur cible : 1000-1300 lignes
- Migration Alembic : **optionnel** (0009 si besoin d'un schéma column,
  sinon pur code change sur `stage_name` valeurs acceptées)
- Ordre commits recommandé : MK.3 (fix counter bug isolé) → MK.2 (split
  watcher) → MK.4 (HTTP 425) → MK.1 (WSS extension, plus gros) → MK.5
  (instrumentation comparative post-MK.1)

# Livrable

- Le fichier `docs/specs/M19-latency-phase-1b.md` complet
- Un ping final ≤ 10 lignes : tests estimés, charge cumulée, hypothèses
  empiriques H-EMP-4/H-EMP-6 à planifier post-ship
````

## 11. Notes d'implémentation

### Piège : WSS market channel subscription par `asset_id`, pas par `wallet`

**Claude §7.1 critique** : "subscribe per asset_id, not per wallet, so you need to know which markets each watched wallet is active in — that dependency means your /holders or /trades discovery still runs underneath; WSS is only the low-latency detection layer on top".

Implication concrète : on ne peut **pas** se désabonner de tous les assets et s'abonner juste aux "wallets watchés" — il faut **d'abord** savoir sur quels asset_ids ces wallets tradent. D'où hybrid mode (D4) : REST polling reste actif low-frequency, WSS capture high-frequency sur les asset_ids déjà connus.

Source dynamique des asset_ids à souscrire :
1. `MyPosition.asset_id` (positions ouvertes — highest priority)
2. `detected_trades.asset_id` last 24h (trades récents des wallets watchés)
3. `strategy_decisions.asset_id` last 24h (évaluation récente)
4. (optionnel) Top markets Gamma `/events` (bootstrap coverage)

Budget de subscription : 500 max (D2). Si dépassé, LRU eviction priorise (3) > (2) > (1) (oldest first).

### Piège : dédup tx_hash entre WSS et REST

Un trade peut arriver via WSS (rapide) puis via REST (lent) sur le même `tx_hash`. Le premier gagne, le second est silencieusement drop via contrainte unique DB. **Confirmer** : le code actuel `DetectedTradeRepository.insert_if_new` retourne `bool` (True si inserted, False si duplicate) — utiliser ce signal pour ne pas émettre `watcher_realtime_detected_ms` 2× pour le même trade.

### Piège : filtrage maker/taker côté client

L'event WSS `trade` contient probablement `{maker: "0xabc", taker: "0xdef", ...}`. On vérifie `maker in watched_wallets or taker in watched_wallets`. **Attention** : dans CTF Polymarket, maker vs taker sont différents contextuellement de DEX standard. Un "maker" ici peut être un ordre limit. Un "taker" est l'ordre FOK qui cross. **Les deux sont intéressants** pour nous (on veut suivre le wallet quel que soit son side dans l'exchange).

### Piège : WSS reconnexion pendant backfill M5_ter

M5_ter live-reload promote un nouveau wallet → `WalletPoller` lance le backfill via REST. Pendant ce temps, le WSS doit ajouter les asset_ids du wallet à la souscription. Race possible : le backfill finit avant que le WSS ait subscribed. **Mitigation** : le WSS subscribe en parallèle du backfill (non-blocking), coûts couvrent les trades pendant la fenêtre.

### Références externes

- **WSS Quickstart Polymarket** : [polymarket mintlify quickstart](https://polymarket-292d1b1b.mintlify.app/quickstart/websocket/WSS-Quickstart). Market + user channels docs officielles.
- **PolytrackHQ WebSocket tutorial** : [polytrackhq.app/blog/polymarket-websocket-tutorial](https://www.polytrackhq.app/blog/polymarket-websocket-tutorial). 500 instruments/connection limit cité.
- **NautilusTrader integration** : [nautilustrader.io/docs/latest/integrations/polymarket](https://nautilustrader.io/docs/latest/integrations/polymarket/). "No unsubscribe support" critical constraint.
- **nevuamarkets/poly-websockets** : [GitHub](https://github.com/nevuamarkets/poly-websockets). Reconnect logic reference.
- **Polymarket/real-time-data-client** : [GitHub](https://github.com/Polymarket/real-time-data-client). Official RTDS (pour reference alternative, hors scope).
- **Matching Engine Restarts docs** : [docs.polymarket.com/trading/matching-engine](https://docs.polymarket.com/trading/matching-engine). Tuesday 7AM ET ~90s.
- **py-clob-client issue 286** : [GitHub issue](https://github.com/Polymarket/py-clob-client/issues/286). HTTP 425 handling reference.

### Questions ouvertes pertinentes à MK

- **Q1** (synthèse §11) : "250ms taker delay" vs "250-300ms matching latency" — instrumentation MK.5 aide à trancher post-ship.
- **Q2** (synthèse §11) : latence WSS market channel sur notre stack — H-EMP-6 planifié post-ship.
- **Q7** (synthèse §11) : Polycop 340ms claim reproductible ? Après MK.1 + MK.5, mesure comparative directe possible. Attente : 2-4s p50 (WSL + non-Dublin) vs 340ms Polycop. OK si Option (d) retenu.
