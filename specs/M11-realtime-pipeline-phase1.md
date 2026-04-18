# M11 — Pipeline temps réel phase 1 (WebSocket CLOB + cache adaptatif + instrumentation latence)

Spec d'implémentation du **bundle "latence phase 1"**. M10 vient d'aligner dry-run et live côté observabilité. M11 attaque la latence end-to-end : le pipeline actuel consomme ~10-15 s entre détection on-chain d'un trade source et persistance d'un ordre chez nous (mesure synthèse §4 + architecture.md), au-delà de la fenêtre utile pour la plupart des signaux smart money. M11 phase 1 cible **2-3 s** par trois leviers additifs, faiblement couplés :

1. **WebSocket CLOB `market` channel** en remplacement du polling HTTP `/midpoint` dans `SlippageChecker` (gain ~500-1500 ms par trade).
2. **Cache Gamma adaptatif** par segment de marché (résolu / proche résolution / actif / inactif) en remplacement du TTL 60 s uniforme (gain ~400 ms par trade, hit rate ~50 % → ~85 %).
3. **Instrumentation latence par étape** via `structlog.bind_contextvars(trade_id=...)` + table `trade_latency_samples` + onglet dashboard `/latency` p50/p95/p99.

Source de vérité conception : `docs/development/M10_synthesis_reference.md` §4 (tranche 2-3 s retenu Perplexity, 347 ms Gemini rejeté), §4.2 (phase 1 = M11), §4.3 (phase 2 = reportable), §6.6 (RTDS open question). Deep-searches référencées : `gemini_deep_search_v2_and_more.md` §4 (cartographie latence), `perplexity_deep_search_v2_and_more.md` §4.1-4.5 (3 phases, tableau avant/après). Conventions : `CLAUDE.md`. Spec de référence format : `specs/M10-parity-and-log-hygiene.md` (dernière en date).

> ⚠️ **M11 ne touche pas aux invariants M10.** Kill switch, alertes, 4 garde-fous M3/M8, badge mode Telegram, processor `filter_noisy_endpoints`, exclusion `dashboard_request` côté `/logs` — tout reste intact. M11 est **additif** sur l'observability layer. Les contrats dry-run/live/simulation restent strictement conformes à §3.3-3.5 spec M10.

---

## 0. Résumé exécutif

- **Scope** : trois livrables couplés dans un seul bundle "latence". (A) `ClobMarketWSClient` + cache in-memory mid-price alimentant `SlippageChecker` (fallback HTTP transparent si WS down ou flag off). (B) Refactor `GammaApiClient._cache` derrière une interface + fonction pure `compute_ttl(market, now) -> int` (TTL 0 pour résolu, 10 s proche résolution, 300 s actif, 3600 s inactif). (C) `trade_id` contextvar en tête de pipeline + 6 stages loggés `stage_duration_ms` + table DB `trade_latency_samples` avec migration 0005 + dashboard onglet `/latency` (bar chart p50/p95/p99 Chart.js).
- **Motivation** : synthèse §4 tranche — à 10-15 s de latence, on perd l'edge sur les signaux smart money (fenêtre utile documentée 30-90 s dans les guides d'arbitrage Polymarket, compétitive à ~1-3 s). Prérequis direct de **M12 scoring v2** (synthèse §7.2 : "on ne peut pas valider la qualité d'un wallet scoré si le bot rate les trades à cause de la latence").
- **Cible** : **2-3 s** end-to-end post-M11. Cible Gemini 347 ms (multi-process workers + Goldsky Turbo + colocalisation VPS) **rejetée v1** — synthèse §4.1 : "single-VPS Python asyncio ne justifie pas l'effort HFT-grade, gain marginal non démontré". Reportable phase 3 si mesure latence post-M11 montre >10 % d'opportunités ratées.
- **Gain attendu par sous-feature** (ordres de grandeur Perplexity §4.4) : WS mid-price **~500-1500 ms** → **<50 ms** cache lookup ; cache adaptatif **~400 ms** gain moyen (hit rate 50 → 85 %) ; instrumentation = **observabilité** pour confirmer les gains et identifier les résiduels (coût CPU p95 à mesurer, cf. risque §11.3).
- **Invariants de sécurité préservés** : M11 n'ouvre **aucune** nouvelle surface d'attaque. WS CLOB `market` channel = **read-only public** (pas de creds L1/L2, pas d'auth, pas de signature, pas de POST). Fallback HTTP `/midpoint` = comportement M2 strict si flag off ou WS down. Zéro migration sur tables `my_orders` / `my_positions` / `pnl_snapshots` / `target_traders`.
- **Hors scope strict** : pas de parallélisation `asyncio.gather` sur filtres (phase 2), pas de watcher WS user channel (phase 2), pas de Goldsky Turbo / Bitquery streaming (phase 3), pas de multi-process workers (phase 3, rejeté v1), pas de pré-signature ordres (phase 3), pas de colocalisation VPS, pas de scoring v2 (M12), pas de fees dynamiques (M13). Ces hors scopes sont **référencés en §13 open questions + §14 ROADMAP** pour ne rien oublier — ils ne sont pas perdus, juste reportés.
- **Effort estimé** : ~2 semaines 1 dev (cohérent roadmap synthèse §0, §7.3).
- **Risque principal** : WebSocket Polymarket instable en production → fallback HTTP doit être transparent + metric de reconnect exposée pour détection (cf. §11.1).

---

## 1. Contexte

### 1.1 État M1..M10 (rappel)

M1..M10 ont livré :

- Watcher → Strategy → Executor → Storage (M1-M3, boucle end-to-end, polling Data API).
- Monitoring M4 + M7 + M10 : `PnlSnapshotWriter`, kill switch 3 modes, badge Telegram.
- Discovery M5 : scoring v1, shadow period.
- Dashboard M4.5 / M6 / M9 : pages + `/logs` avec preset.
- Dry-run M8 : realistic fill sur `/book` read-only.
- CLI silencieux M9 + rotation fichier.
- M10 : parité 3 modes (SIMULATION/DRY_RUN/LIVE), badge mode templates, processor `filter_noisy_endpoints`, exclusion `/logs` default.

Latence actuelle (estimation architecture.md + mesure synthèse §4.2) :

| Étape | Implémentation actuelle | Ordre de grandeur |
|---|---|---|
| Watcher : polling Data API `/activity` | `poll_interval_seconds=5` + pagination + `sortDirection=ASC` | ~5-7 s (intervalle + fetch) |
| Strategy : lookup Gamma | `GammaApiClient._cache` TTL 60 s uniforme, hit rate ~50 % | ~1-3 s (miss = HTTP round-trip) |
| Strategy : lookup midpoint CLOB | `ClobReadClient.get_midpoint` HTTP synchrone par trade | ~500-1500 ms |
| Strategy : filtres (4 séquentiels) | `run_pipeline` chaîne `MarketFilter` → `PositionSizer` → `SlippageChecker` → `RiskManager` | ~200-500 ms |
| Executor : POST ordre ou simulation M8 | `ClobWriteClient.post_order` (live) ou `_persist_realistic_simulated` (dry-run M8) | ~300-800 ms |
| **Total observé** | — | **~10-15 s** |

### 1.2 Pourquoi cette spec maintenant

Le brainstorming 2026-04-18 + les deux deep-searches + la synthèse §4 + §7.2 convergent sur un prérequis dur : **M12 scoring v2 ne peut pas être validé sur un pipeline lent**. Si un wallet auto-promu à M12 ne produit pas de trades copiés parce que le bot rate la fenêtre d'exécution, on ne peut pas distinguer "scoring mauvais" de "bot trop lent". Donc M12 nécessite M11 mergé et observable.

M11 est aussi **rapide** (~2 semaines) et **additif-dominant** : un nouveau module (`ClobMarketWSClient`), un nouveau fichier `_cache_policy.py`, un nouveau contextvar + 6 appels `bind` + une nouvelle table DB, un nouvel onglet dashboard. Les modules existants sont modifiés à la marge (lookup cache + fallback dans `SlippageChecker`, substitution `_cache` derrière interface dans `GammaApiClient`, 6 points d'instrumentation dans `pipeline.py` et `data_api_client.py`). **Zéro refactor M3 / M8 / M10**.

### 1.3 Références externes

- **Perplexity DeepResearch §4.1-4.5** (architecture data Polymarket, axes d'amélioration, tableau latence avant/après, recommandation phase 1 → phase 2 → phase 3).
- **Gemini DeepResearch §4** (cartographie latence HFT-grade, 5 composantes, vecteurs de compression — référence pour phase 3 future seulement).
- **Synthèse §4.1** : décision 2-3 s vs 347 ms.
- **Synthèse §4.2** : contenu exact phase 1 (ce doc).
- **Synthèse §4.3** : contenu phase 2 (reportable M11-bis).
- **Synthèse §4.4** : hors scope initial (phase 3).
- **Synthèse §6.6** : RTDS Polymarket — open question (cf. §13.1).
- **Polymarket docs WS** : `wss://ws-subscriptions-clob.polymarket.com/ws/market` (référencé `CLAUDE.md:67`). Le schéma exact des messages sera capturé en action §1 du plan d'implémentation (§8).

---

## 2. Objectifs et non-objectifs

### 2.1 Objectifs

**A. WebSocket CLOB `market` channel pour `SlippageChecker`**

- Nouveau module `src/polycopy/strategy/clob_ws_client.py` : classe `ClobMarketWSClient`.
- Connection persistante `wss://ws-subscriptions-clob.polymarket.com/ws/market`.
- Souscription lazy au channel `market` sur les `token_id` vus en cours de pipeline (évite tracker 500+ tokens inutilement).
- Cache in-memory `dict[token_id, (mid_price: float, last_update_ts: datetime)]` alimenté par les messages WS.
- `SlippageChecker.check` consulte le cache **avant** de tomber sur le fallback HTTP `/midpoint` (latence lookup ≈ 0 ms si cache hit).
- Reconnect logic : backoff exponentiel via `tenacity` (déjà utilisé dans le projet), max 10 retries, health check ping/pong 30 s.
- Garbage collection : unsub auto après 5 min d'inactivité sur un token.
- Metric `ws_connection_status` : `up|reconnecting|down`, exposée via log `structlog` + endpoint dashboard.
- Feature flag `STRATEGY_CLOB_WS_ENABLED=true` (default). Si `false` → comportement M2..M10 strict (HTTP polling uniquement).
- Fallback HTTP transparent : si WS down OU token jamais vu OU flag off, `SlippageChecker` consomme `ClobReadClient.get_midpoint` comme avant — **zéro régression**.

**B. Cache Gamma adaptatif**

- Refactor `GammaApiClient._cache` derrière une interface `_CacheEntry(market, cached_at, ttl_seconds)`.
- Nouveau module `src/polycopy/strategy/_cache_policy.py` exposant la **fonction pure** `compute_ttl(market: MarketMetadata, now: datetime) -> int`.
- Segmentation :
  - **Marchés résolus** (`closed=True` OU `archived=True`) : TTL **infini** (= `_TTL_RESOLVED_SENTINEL`, modélisé comme `int` très grand, cf. §4.2).
  - **Proches résolution** (`end_date - now < 1h` ET `closed=False`) : TTL **10 s**.
  - **Actifs** (`volume_24h_usd > 100` OU `liquidity_clob > 1000`) : TTL **300 s**.
  - **Inactifs** (par défaut) : TTL **3600 s**.
- Metric `gamma_cache_hit_rate` : compteur `hits / (hits + misses)` fenêtre 5 min, loggé à chaque tick.
- Feature flag `STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED=true` (default). Si `false` → TTL 60 s uniforme M2 (fallback de sécurité).

**C. Instrumentation latence pipeline**

- `trade_id: str = uuid4().hex` posé en tête de pipeline par le `WalletPoller` au moment de `insert_if_new(dto)` (ligne ~70 de `wallet_poller.py`).
- `structlog.contextvars.bind_contextvars(trade_id=trade_id)` propagé dans le `WalletPoller._publish` puis le `StrategyOrchestrator._handle_trade` via le DTO (cf. §5.1).
- 6 stages loggés `stage_duration_ms` :
  1. `watcher_detected_ms` = `ingested_at - trade.timestamp_onchain` (Data API → ingestion locale).
  2. `strategy_enriched_ms` = durée `GammaApiClient.get_market` (lookup Gamma).
  3. `strategy_filtered_ms` = durée `MarketFilter.check` + `PositionSizer.check`.
  4. `strategy_sized_ms` = durée `PositionSizer.check` (sous-section de 3 — logged séparément pour granularité).
  5. `strategy_risk_checked_ms` = durée `RiskManager.check`.
  6. `executor_submitted_ms` = durée jusqu'à POST CLOB (live) ou `_persist_realistic_simulated` (M8) ou stub M3 (dry-run/simulation).

> **Note** : les stages 3 et 4 se chevauchent car `strategy_filtered_ms` = `MarketFilter + PositionSizer` et `strategy_sized_ms` = `PositionSizer` seul. Ce chevauchement est intentionnel (meilleure granularité dashboard) et documenté — voir §5.2.

- Nouvelle table `trade_latency_samples` : `id, trade_id, stage_name, duration_ms, timestamp`. Append-only.
- Migration Alembic `0005_m11_latency_samples.py` (SQLite-friendly, pas de `batch_alter_table` nécessaire car table nouvelle).
- Rétention 7 jours : purge query au boot (+ quotidien) — cf. §5.6.
- Nouveau onglet dashboard `/latency` : bar chart p50/p95/p99 par stage, filtre `?since=1h|24h|7d` (réutilise `parse_since` queries.py:56).

### 2.2 Non-objectifs

- Pas de parallélisation strategy (`asyncio.gather` sur filtres indépendants) — c'est phase 2 / reportable (cf. §14 ROADMAP).
- Pas de watcher WS user channel (remplacement polling Data API) — complexité multi-wallet reconnect, c'est phase 2 (cf. §14).
- Pas de Goldsky Turbo / Bitquery streaming — c'est phase 3 (cf. §14).
- Pas de multi-process workers — rejeté v1 par synthèse §4.1 ("asyncio mono-thread suffit pour 50 wallets, GIL pas bloquant sur I/O-bound").
- Pas de pré-signature ordres en batch — phase 3.
- Pas de colocalisation VPS / optimisation réseau L3 — hors scope projet.
- Pas de WebSocket CLOB **user channel** — uniquement `market` channel à M11.
- Pas de RTDS (Real-Time Data Socket) — alternative évaluée en open question §13.1, décision reportée post-M11 mesure.
- Pas de scoring v2 (M12), pas de taker fees dynamiques (M13).
- Pas de migration DB sur `my_orders`, `my_positions`, `pnl_snapshots`, `target_traders`, `trader_scores` — seule nouvelle table `trade_latency_samples`.
- Pas de refactor M3 live path, M8 path, M4/M7/M10 observability layer.
- Pas de nouveau template Telegram (les stages ne déclenchent aucune alerte à M11, observabilité locale uniquement).

---

## 3. Design A : WebSocket CLOB `market` channel

### 3.1 Protocole WS Polymarket — schéma message à capturer

**Action obligatoire avant code** (§8 étape 1) : capturer au moins 10 messages réels du channel `market` pour **1 token_id** en souscription lazy. Deux approches :

1. **Via skill Claude Code `/polymarket:polymarket`** : préférer si le skill expose un helper WS. À vérifier au moment de l'implémentation.
2. **Connexion directe en script standalone** : `scripts/capture_clob_ws_fixture.py` (nouveau script, hors du package `polycopy`, intégré seulement au repo pour documenter le schéma). Connexion `websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market")` puis envoi sub `{"type": "market", "assets_ids": ["<token_id>"]}` (syntaxe attendue — à confirmer par capture).

Fixtures attendues à enregistrer dans `tests/fixtures/clob_ws_market_sample.jsonl` :

- 1 message `book` (snapshot complet orderbook au sub).
- 3 messages `price_change` (updates incrémentales).
- 1 message `last_trade_price` (trade executed).
- 1 message `best_bid_ask` (meilleur bid/ask updated).
- 1 message `market_resolved` (marché résolu — détection fin de vie pour unsub).

Ces événements sont cités dans la synthèse §4 (Perplexity §4.1) comme les 5 types émis par le channel `market`. Le schéma exact est à figer par la capture avant d'écrire le DTO.

**Si la capture révèle un schéma incompatible** avec cette spec (ex: `price_change` ne contient pas le mid-price directement, uniquement le bid/ask à recalculer), **STOP et adapter** la spec avant de coder — ne pas deviner.

### 3.2 Module `ClobMarketWSClient` — connection lifecycle

Nouveau fichier `src/polycopy/strategy/clob_ws_client.py`.

```python
class ClobMarketWSClient:
    """Client WebSocket sur wss://ws-subscriptions-clob.polymarket.com/ws/market.

    Responsabilités :
    - Connection persistante + reconnect exponentiel tenacity.
    - Souscription lazy channel `market` sur les token_ids demandés.
    - Cache in-memory {token_id → (mid_price, last_update_ts)}.
    - GC unsub après 5 min d'inactivité.
    - Health check ping/pong 30 s.
    - Metric ws_connection_status (up|reconnecting|down).
    """

    URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    _INACTIVITY_UNSUB_SECONDS = 300  # 5 min
    _HEALTH_CHECK_INTERVAL_SECONDS = 30
    _MAX_RECONNECT_ATTEMPTS = 10
    _CACHE_TTL_WS_SECONDS = 60  # sanity expiration (si plus de push, stale)

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._cache: dict[str, tuple[float, datetime]] = {}
        self._subscribed: set[str] = set()
        self._last_seen: dict[str, datetime] = {}
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._status: Literal["up", "reconnecting", "down"] = "down"

    async def run(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : connect → listen → reconnect on error."""
        ...

    async def subscribe(self, token_id: str) -> None:
        """Lazy sub sur un token_id. No-op si déjà subscribed."""
        ...

    async def get_mid_price(self, token_id: str) -> float | None:
        """Retourne le mid_price depuis le cache, ou None si absent/stale."""
        ...

    async def _health_check_loop(self, stop_event: asyncio.Event) -> None:
        """Ping/pong toutes les 30 s ; déclenche reconnect si pong manquant."""
        ...

    async def _gc_loop(self, stop_event: asyncio.Event) -> None:
        """Unsub les token_ids inactifs depuis > 5 min."""
        ...
```

Reconnect backoff via `tenacity` (déjà utilisé, cohérent avec `GammaApiClient._fetch` et `DataApiClient._fetch_page`) :

```python
@retry(
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(_MAX_RECONNECT_ATTEMPTS),
    before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
    retry=retry_if_exception_type(websockets.ConnectionClosed),
    reraise=True,
)
async def _connect(self) -> None:
    self._ws = await websockets.connect(self.URL, ...)
    self._status = "up"
    log.info("clob_ws_connected")
```

### 3.3 Intégration `SlippageChecker` — cache lookup → fallback HTTP

Modif `src/polycopy/strategy/pipeline.py:107-125` (classe `SlippageChecker`) :

```python
class SlippageChecker:
    """Compare le mid CLOB courant au prix source ; rejette si > MAX_SLIPPAGE_PCT.

    M11 : lookup WS cache EN PREMIER ; fallback HTTP /midpoint si WS indisponible
    (flag off, token jamais vu, WS down, cache stale > CACHE_TTL_WS_SECONDS).
    """

    def __init__(
        self,
        clob_client: ClobReadClient,
        settings: "Settings",
        ws_client: ClobMarketWSClient | None = None,
    ) -> None:
        self._clob = clob_client
        self._settings = settings
        self._ws = ws_client

    async def check(self, ctx: PipelineContext) -> FilterResult:
        mid: float | None = None
        if self._ws is not None and self._settings.strategy_clob_ws_enabled:
            await self._ws.subscribe(ctx.trade.asset_id)  # lazy sub
            mid = await self._ws.get_mid_price(ctx.trade.asset_id)
        if mid is None:
            # fallback HTTP (M2 behavior)
            mid = await self._clob.get_midpoint(ctx.trade.asset_id)
        if mid is None:
            return FilterResult(passed=False, reason="no_orderbook")
        ctx.midpoint = mid
        ...  # comportement M2 inchangé
```

**Invariant préservé** : le contrat `FilterResult(passed=False, reason="no_orderbook")` est identique à M2 — si WS ET HTTP retournent `None`, rejet. Aucun test existant sur `test_strategy_pipeline.py` (actuellement 3 tests `test_slippage_checker_*`) ne doit casser.

### 3.4 Lazy subscription / GC des tokens inactifs

Flux :

1. `SlippageChecker.check(ctx)` appelle `ws.subscribe(token_id)` avant lookup.
2. Si `token_id` pas dans `self._subscribed`, envoyer message WS `{"type": "market", "assets_ids": [token_id]}` (syntaxe à confirmer par fixture §3.1).
3. `self._last_seen[token_id] = now()`.
4. Chaque message `price_change` / `last_trade_price` pour `token_id` met à jour `self._cache[token_id]` **et** `self._last_seen[token_id]`.
5. Un `_gc_loop` tournant toutes les 60 s scanne `self._last_seen` : tout token dont `now - last_seen > _INACTIVITY_UNSUB_SECONDS` est unsub (`{"type": "unsubscribe", "assets_ids": [token_id]}` — syntaxe à confirmer) et retiré de `self._subscribed`, `self._cache`, `self._last_seen`.
6. Cap dur (anti-leak mémoire) : **500 tokens max subscribés simultanément**. Au-delà, unsub le plus ancien en LRU. Documenté §11.6.

### 3.5 Health check + metric `ws_connection_status`

Le `_health_check_loop` envoie un ping websocket (`websockets.ping()`) toutes les 30 s. Si aucun pong reçu en 10 s → `self._status = "down"` → déclenche reconnect via `_connect` tenacity-wrapped.

Metric exposée de 3 façons (v1 M11) :

- **Log structlog** : `log.info("ws_connection_status_change", status="up|reconnecting|down", last_change=ts)` à chaque transition.
- **Attribut `self._status`** lisible par `StrategyOrchestrator` qui peut l'exposer au `DashboardOrchestrator` via un singleton léger (ex: `app.state.ws_client` cf. M4.5 pattern).
- **Endpoint dashboard** `/api/ws-status` (JSON) : `{"status": "up", "cache_entries": 42, "subscribed": 42, "last_change": "..."}` — reportable à M11.1 si complexité (cf. §13.4 open question).

v1 M11 : **log only + attribut lisible via `app.state`**. Pas d'endpoint JSON dédié.

### 3.6 Feature flag + fallback HTTP strict si désactivé

Env var `STRATEGY_CLOB_WS_ENABLED: bool = True` (cf. §6). Si `False` :

- `ClobMarketWSClient` **pas instancié** par `StrategyOrchestrator` (lazy init, cohérent pattern M3/M4/M8 que le code suive).
- `SlippageChecker.__init__(..., ws_client=None)` — path HTTP strict M2.
- Aucune connexion WS ouverte, aucune metric exposée.

Test obligatoire : `test_slippage_checker_fallback_to_http_when_feature_flag_disabled` (§9.3). Un user qui désactive le flag doit retrouver **exactement** le comportement M2..M10. Non-régression absolue.

---

## 4. Design B : cache Gamma adaptatif

### 4.1 Segmentation des marchés

Décision retenue synthèse §4.2.2 (Perplexity §4.3 #4 combiné à l'audit interne) :

| Segment | Condition | TTL |
|---|---|---|
| **Résolu** | `closed=True` OR `archived=True` | **infini** (`_TTL_RESOLVED_SENTINEL = 31_536_000` = 1 an, sentinel représentant "immuable jusqu'à purge cache") |
| **Proche résolution** | `end_date - now < 1h` AND `closed=False` AND `archived=False` | **10 s** (re-check agressif sur les dernières minutes) |
| **Actif** | default si `volume_24h_usd > 100` OR `liquidity_clob > 1000` | **300 s** (vs 60 s M2) |
| **Inactif** | reste | **3600 s** |

**Rationale** :

- **Résolu** : un marché `closed=True` est immuable côté outcome — safe de cacher longtemps. L'only-case où ça change = replay de données mal ingérées, traité hors scope.
- **Proche résolution** : la dernière heure avant résolution concentre beaucoup de volatilité liquidité/état → re-check 10 s est un bon compromis.
- **Actif** : 300 s au lieu de 60 s M2 car le `MarketFilter` consomme surtout `liquidity_clob`, `end_date`, `accepting_orders`, `closed`, `archived` — champs qui ne bougent pas à la seconde.
- **Inactif** : 3600 s = on ne regarde ces marchés que sporadiquement (pas de trades source donc peu d'entrées dans `detected_trades`).

### 4.2 Fonction pure `compute_ttl(market, now) -> int`

Nouveau fichier `src/polycopy/strategy/_cache_policy.py`.

```python
"""Politique de TTL adaptatif pour GammaApiClient (M11 §4.1)."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.strategy.dtos import MarketMetadata

_TTL_RESOLVED_SENTINEL: int = 31_536_000  # 1 an (immuable effectif)
_TTL_NEAR_RESOLUTION_SECONDS: int = 10
_TTL_ACTIVE_SECONDS: int = 300
_TTL_INACTIVE_SECONDS: int = 3600

_NEAR_RESOLUTION_WINDOW = timedelta(hours=1)
_ACTIVE_VOLUME_24H_USD: float = 100.0
_ACTIVE_LIQUIDITY_USD: float = 1000.0


def compute_ttl(market: "MarketMetadata", now: datetime) -> int:
    """Retourne le TTL cache en secondes pour un marché donné.

    Pure function — aucun I/O, aucun state. Testable isolément.
    """
    if market.closed or market.archived:
        return _TTL_RESOLVED_SENTINEL
    # Proche résolution
    end = _resolve_end_datetime(market)  # dupe de pipeline.py — à extraire en helper commun
    if end is not None and (end - now) < _NEAR_RESOLUTION_WINDOW:
        return _TTL_NEAR_RESOLUTION_SECONDS
    # Actif
    if (market.volume_24h_usd or 0.0) > _ACTIVE_VOLUME_24H_USD:
        return _TTL_ACTIVE_SECONDS
    if (market.liquidity_clob or 0.0) > _ACTIVE_LIQUIDITY_USD:
        return _TTL_ACTIVE_SECONDS
    return _TTL_INACTIVE_SECONDS
```

**Note** : `MarketMetadata` actuel (`src/polycopy/strategy/dtos.py`) n'expose peut-être pas `volume_24h_usd` directement — à confirmer à l'implémentation. Si absent, étendre le DTO pour parser ce champ depuis Gamma `/markets` (champ `volume24hr` ou équivalent, à identifier par capture Gamma).

### 4.3 Impact mémoire cache

Option A (retenue v1) : **TTL expire uniquement, pas de LRU**. Un cache Gamma typique contient < 1000 entrées (nombre de marchés observés par le bot). À 1 Ko par entrée (Python object + MarketMetadata), ~1 Mo max. Acceptable.

Option B (rejetée v1) : LRU bounded à 500. Complexité en plus pour un gain mémoire marginal. Reportable si observation mémoire anormale en prod.

Le cache est scoped **par instance `GammaApiClient`** → par process → limité par l'usage réel. Pas d'éviction forcée en v1.

### 4.4 Metric `gamma_cache_hit_rate`

Nouveau compteur dans `GammaApiClient` :

```python
self._hits: int = 0
self._misses: int = 0
self._last_log_at: datetime = self._now()
```

À chaque `get_market` :

```python
if cache_hit:
    self._hits += 1
else:
    self._misses += 1
# Log rolling ratio toutes les 5 min
if self._now() - self._last_log_at > timedelta(minutes=5):
    total = self._hits + self._misses
    ratio = self._hits / total if total else 0.0
    log.info("gamma_cache_hit_rate", hit_rate=ratio, hits=self._hits, misses=self._misses)
    self._last_log_at = self._now()
    self._hits = 0
    self._misses = 0
```

Cible attendue post-M11 : **~85 %** (vs ~50 % estimé M2). À valider empiriquement.

### 4.5 Feature flag + fallback TTL 60s

Env var `STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED: bool = True`. Si `False` :

- `GammaApiClient` utilise le TTL uniforme `CACHE_TTL = timedelta(seconds=60)` M2 (code actuel inchangé, route de fallback explicite).
- `_cache_policy.compute_ttl` n'est pas appelé.

Non-régression absolue si flag off → tests `test_gamma_client.py` existants continuent à passer sans adaptation.

---

## 5. Design C : instrumentation latence

### 5.1 `trade_id` contextvar + binding

Le `trade_id` est généré au **moment de l'insertion du `DetectedTradeDTO`** par `WalletPoller._poll_once` (à `wallet_poller.py:70` sous la ligne `inserted = await self._repo.insert_if_new(dto)` — remplacer par une version qui retourne aussi le trade_id).

```python
# wallet_poller.py (M11)
import uuid
import structlog

async def _poll_once(self, since: datetime) -> None:
    trades = await self._client.get_trades(self._wallet, since=since)
    for trade in trades:
        dto = self._to_dto(trade)
        inserted = await self._repo.insert_if_new(dto)
        if inserted:
            trade_id = uuid.uuid4().hex
            # M11 : attacher trade_id au DTO pour propagation downstream.
            dto = dto.model_copy(update={"trade_id": trade_id})
            structlog.contextvars.bind_contextvars(trade_id=trade_id)
            # Stage 1 : watcher_detected_ms
            detected_ms = (self._now() - trade.timestamp_utc).total_seconds() * 1000.0
            self._log.info(
                "trade_detected",
                stage_duration_ms=detected_ms,
                stage_name="watcher_detected_ms",
                tx_hash=trade.transaction_hash,
                ...,
            )
            await self._latency_repo.insert(trade_id, "watcher_detected_ms", detected_ms)
            self._publish(dto)
```

Le contextvar propage automatiquement le `trade_id` à tous les `log.info(...)` downstream dans la **même task asyncio**. Le passage cross-queue (Watcher → Strategy → Executor) passe par **le DTO** (champ `trade_id` ajouté à `DetectedTradeDTO`, propagé dans `OrderApproved`). Chaque orchestrator rebind explicitement :

```python
# strategy/orchestrator.py (M11, dans _handle_trade)
structlog.contextvars.bind_contextvars(trade_id=trade.trade_id)
```

### 5.2 Liste exhaustive des 6 stages + points d'injection `file:line`

| # | Stage | Sémantique | Fichier + fonction |
|---|---|---|---|
| 1 | `watcher_detected_ms` | `now - trade.timestamp` à l'ingestion locale | `src/polycopy/watcher/wallet_poller.py:70-80` (après `insert_if_new`) |
| 2 | `strategy_enriched_ms` | Durée `GammaApiClient.get_market` (cache hit ou miss) | `src/polycopy/strategy/pipeline.py:36-54` (wrapper autour du call dans `MarketFilter.check`) |
| 3 | `strategy_filtered_ms` | Durée cumulative `MarketFilter + PositionSizer + SlippageChecker` | `src/polycopy/strategy/orchestrator.py:74-87` (wrapper autour de `run_pipeline`) |
| 4 | `strategy_sized_ms` | Durée `PositionSizer.check` seul | `src/polycopy/strategy/pipeline.py:90-104` (wrapper dans `PositionSizer`) |
| 5 | `strategy_risk_checked_ms` | Durée `RiskManager.check` | `src/polycopy/strategy/pipeline.py:139-150` (wrapper dans `RiskManager`) |
| 6 | `executor_submitted_ms` | Durée depuis `OrderApproved` reçu par executor jusqu'à POST ou simulate_fill terminé | `src/polycopy/executor/orchestrator.py:148-160` (wrapper autour `execute_order`) |

**Chevauchement intentionnel** stages 3 et 4 : `strategy_filtered_ms` inclut `strategy_sized_ms` (PositionSizer). C'est voulu — le dashboard `/latency` permet de voir :

- **Stage 4** (`strategy_sized_ms`) : coût DB seul (`select MyPosition where closed_at is None`).
- **Stage 3** - Stage 4 : coût `MarketFilter + SlippageChecker` (détection où le goulot WS reste).

Cf. §8 étape 10 pour l'ordre d'implémentation.

**Mesure** : chaque stage utilise `time.perf_counter_ns()` pour précision sub-ms :

```python
import time
...
t0 = time.perf_counter_ns()
result = await filter.check(ctx)
duration_ms = (time.perf_counter_ns() - t0) / 1e6
log.info("stage_complete", stage_name="strategy_filtered_ms", stage_duration_ms=duration_ms)
await latency_repo.insert(trade_id, "strategy_filtered_ms", duration_ms)
```

### 5.3 Table `trade_latency_samples` + migration Alembic 0005

Modèle SQLAlchemy ajouté dans `src/polycopy/storage/models.py` (nouveau, après `PnlSnapshot`) :

```python
class TradeLatencySample(Base):
    """Échantillon latence par stage du pipeline (M11).

    Append-only, purgé à 7 jours. 6 rows par trade (1 par stage).
    """

    __tablename__ = "trade_latency_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_utc,
        index=True,
        nullable=False,
    )

    __table_args__ = (
        Index("ix_trade_latency_samples_stage_ts", "stage_name", "timestamp"),
    )
```

Migration `alembic/versions/0005_m11_latency_samples.py` :

```python
"""M11 pipeline latency samples.

Ajoute la table `trade_latency_samples` (append-only, purge 7 jours) pour
l'instrumentation par stage introduite en M11. Pas de modification sur les
tables existantes.

Revision ID: 0005_m11_latency_samples
Revises: 0004_m8_dry_run_realistic
Create Date: 2026-XX-XX
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_m11_latency_samples"
down_revision: str | Sequence[str] | None = "0004_m8_dry_run_realistic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_latency_samples",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.String(32), nullable=False),
        sa.Column("stage_name", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.Float, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_trade_latency_samples_trade_id",
        "trade_latency_samples",
        ["trade_id"],
    )
    op.create_index(
        "ix_trade_latency_samples_stage_ts",
        "trade_latency_samples",
        ["stage_name", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_latency_samples_stage_ts",
        table_name="trade_latency_samples",
    )
    op.drop_index(
        "ix_trade_latency_samples_trade_id",
        table_name="trade_latency_samples",
    )
    op.drop_table("trade_latency_samples")
```

Pas de `batch_alter_table` — table nouvelle, pas de contrainte à recréer. Downgrade trivial.

### 5.4 Endpoint dashboard `/latency` + queries p50/p95/p99

Nouvelle route `/latency` dans `src/polycopy/dashboard/routes.py` (build_pages_router) :

```python
@router.get("/latency", response_class=HTMLResponse)
async def latency_page(
    request: Request,
    sf: SFDep,
    since: str = "24h",
) -> HTMLResponse:
    """Onglet M11 — histogramme latence par stage p50/p95/p99."""
    percentiles = await queries.compute_latency_percentiles(
        sf,
        since=queries.parse_since(since),
    )
    return _render(
        request,
        "latency.html",
        {
            "since": since,
            "percentiles": percentiles,
        },
    )
```

Query SQL (`src/polycopy/dashboard/queries.py`) :

```python
async def compute_latency_percentiles(
    sf: async_sessionmaker[AsyncSession],
    *,
    since: timedelta,
) -> dict[str, dict[str, float]]:
    """Retourne {stage_name: {p50, p95, p99, count}} sur la fenêtre.

    SQLite n'a pas de PERCENTILE_CONT natif → query Python côté client.
    Acceptable pour volume ~6 rows × (trades / window) — qq milliers max.
    """
    async with sf() as session:
        cutoff = datetime.now(tz=UTC) - since
        stmt = select(
            TradeLatencySample.stage_name,
            TradeLatencySample.duration_ms,
        ).where(TradeLatencySample.timestamp >= cutoff)
        rows = (await session.execute(stmt)).all()
    by_stage: dict[str, list[float]] = {}
    for stage_name, ms in rows:
        by_stage.setdefault(stage_name, []).append(ms)
    result: dict[str, dict[str, float]] = {}
    for stage, samples in by_stage.items():
        samples.sort()
        n = len(samples)
        result[stage] = {
            "p50": _percentile(samples, 0.50),
            "p95": _percentile(samples, 0.95),
            "p99": _percentile(samples, 0.99),
            "count": float(n),
        }
    return result


def _percentile(sorted_samples: list[float], p: float) -> float:
    if not sorted_samples:
        return 0.0
    idx = min(int(p * len(sorted_samples)), len(sorted_samples) - 1)
    return sorted_samples[idx]
```

### 5.5 Template `latency.html` + Chart.js bar chart

Nouveau template `src/polycopy/dashboard/templates/latency.html` (cohérent M6 Tailwind CDN + Chart.js CDN déjà présent dans `base.html`).

Structure :

- Header : titre `Latence pipeline — M11` + switch `?since=` (1h / 24h / 7d) cohérent pattern M6 `/pnl`.
- Tableau récap : stages × p50/p95/p99/count (6 lignes × 4 colonnes).
- Bar chart Chart.js : 1 bar group par stage, 3 bars (p50, p95, p99) par groupe.
- Lien doc : rappel des 6 stages + sémantique.

Pas de polling HTMX temps réel — v1 reload manuel (`?since=...` change). Reportable si besoin.

### 5.6 Rétention 7 jours (purge scheduler)

Stratégie v1 : **query purge au boot + quotidienne**.

- **Au boot** (`cli/runner.py` après `init_db`) : `await TradeLatencyRepository(session_factory).purge_older_than(days=7)`.
- **Quotidien** : nouveau scheduler léger `LatencyPurgeScheduler` lancé dans le TaskGroup principal (cf. §7.15). Boucle `sleep 24h → purge`.

Alternative (rejetée v1) : scheduler dédié intégré au `MonitoringOrchestrator`. Complexité non justifiée — purge est une opération isolée sans couplage observability.

Volume estimé : 6 stages × (trades/jour). À 50 trades/jour = 300 rows/j × 7 = 2100 rows actifs. Négligeable DB. Purge SQL simple :

```python
async def purge_older_than(self, *, days: int) -> int:
    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    async with self._session_factory() as session:
        stmt = delete(TradeLatencySample).where(TradeLatencySample.timestamp < cutoff)
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount or 0
```

---

## 6. Configuration — env vars

### 6.1 Ajoutées

| Env var | Champ Settings | Type | Default | Description |
|---|---|---|---|---|
| `STRATEGY_CLOB_WS_ENABLED` | `strategy_clob_ws_enabled` | `bool` | `True` | Active le client WebSocket CLOB `market` channel pour `SlippageChecker`. Si `false` → fallback HTTP `/midpoint` strict (comportement M2..M10). |
| `STRATEGY_CLOB_WS_URL` | `strategy_clob_ws_url` | `str` | `"wss://ws-subscriptions-clob.polymarket.com/ws/market"` | Override URL WS (test / staging). |
| `STRATEGY_CLOB_WS_MAX_SUBSCRIBED` | `strategy_clob_ws_max_subscribed` | `int [50, 5000]` | `500` | Cap dur nombre de tokens subscribés simultanément (anti-leak mémoire). Au-delà, LRU unsub le plus ancien. |
| `STRATEGY_CLOB_WS_INACTIVITY_UNSUB_SECONDS` | `strategy_clob_ws_inactivity_unsub_seconds` | `int [60, 3600]` | `300` | Unsub auto après N secondes d'inactivité sur un token (GC mémoire). |
| `STRATEGY_CLOB_WS_HEALTH_CHECK_SECONDS` | `strategy_clob_ws_health_check_seconds` | `int [5, 300]` | `30` | Période ping/pong health check WS. |
| `STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED` | `strategy_gamma_adaptive_cache_enabled` | `bool` | `True` | Active le cache Gamma à TTL adaptatif (§4). Si `false` → TTL 60 s uniforme M2. |
| `LATENCY_SAMPLE_RETENTION_DAYS` | `latency_sample_retention_days` | `int [1, 90]` | `7` | Rétention des rows `trade_latency_samples`. Purge au boot + quotidienne. |
| `LATENCY_INSTRUMENTATION_ENABLED` | `latency_instrumentation_enabled` | `bool` | `True` | Active l'instrumentation latence globale (6 stages + insert DB). Si `false` → pipeline fonctionne sans latence loggée (secours si surcharge CPU — cf. risque §11.3). |

### 6.2 Modifiées

Aucune env var existante modifiée sémantiquement. La constante `GammaApiClient.CACHE_TTL = timedelta(seconds=60)` M2 reste en tant que **fallback** quand `STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED=false`.

### 6.3 `.env.example` — mise à jour

Ajouter un bloc dédié (après la section `# --- Monitoring` et avant `# --- Logs`) :

```dotenv
# --- Pipeline temps réel (M11, opt-in par défaut) ---
# WebSocket CLOB `market` channel pour SlippageChecker.
# false → fallback HTTP /midpoint strict (comportement M2..M10).
STRATEGY_CLOB_WS_ENABLED=true
# STRATEGY_CLOB_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
# STRATEGY_CLOB_WS_MAX_SUBSCRIBED=500
# STRATEGY_CLOB_WS_INACTIVITY_UNSUB_SECONDS=300
# STRATEGY_CLOB_WS_HEALTH_CHECK_SECONDS=30

# Cache Gamma TTL adaptatif par segment (résolu / proche résolution / actif / inactif).
STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED=true

# Instrumentation latence pipeline (6 stages, DB append-only, purge 7 jours).
# LATENCY_INSTRUMENTATION_ENABLED=true
# LATENCY_SAMPLE_RETENTION_DAYS=7
```

---

## 7. Changements module par module (file:line)

### 7.1 `src/polycopy/config.py`

**Contexte actuel** : `Settings` class ligne 16+. Section `# --- Mode ---` ligne 76+, env vars M8 lignes 79-122, M10 mid-file.

**Diff M11** : ajouter un nouveau bloc `# --- Pipeline temps réel (M11) ---` **après** le bloc Dashboard M10 et **avant** `# --- Discovery (M5) ---`. 8 nouveaux champs avec `Field(...)` + `description`. Validators Pydantic sur les bornes int listées en §6.1.

### 7.2 `src/polycopy/strategy/clob_ws_client.py` NOUVEAU

Nouveau fichier, ~200-300 lignes. Contenu détaillé §3.2-3.5 (pseudo-code). Ne pas oublier :

- Import `websockets` (déjà dépendance projet via `executor.dry_run_resolution_watcher` ? À vérifier au début de l'implémentation — ajouter au `pyproject.toml` si absent).
- Import `structlog`, `tenacity`, `asyncio`, `uuid`, `datetime`.
- Classe `ClobMarketWSClient` + dataclass `_CacheEntry` (frozen) + constantes privées `_INACTIVITY_UNSUB_SECONDS`, etc.
- Méthodes : `__init__`, `run`, `subscribe`, `unsubscribe` (privé), `get_mid_price`, `_connect`, `_listen_loop`, `_health_check_loop`, `_gc_loop`.

### 7.3 `src/polycopy/strategy/pipeline.py` (lignes 107-125 `SlippageChecker` + wrappers latence)

**Diff M11** :

- `SlippageChecker.__init__` : accepte `ws_client: ClobMarketWSClient | None = None` en plus de `clob_client`, `settings`.
- `SlippageChecker.check` : essaie d'abord WS cache, fallback HTTP si `None`.
- Ajouter wrappers `time.perf_counter_ns` autour de `MarketFilter.check`, `PositionSizer.check`, `RiskManager.check` pour instrumenter stages 3/4/5 (ou plus proprement : wrapper externe dans `run_pipeline`). Cf. §5.2.
- Signature `run_pipeline` : ajouter param optionnel `latency_repo: TradeLatencyRepository | None = None`. Si `None`, instrumentation no-op (désactivable via flag).

### 7.4 `src/polycopy/strategy/gamma_client.py` (lignes 23-52 `get_market`)

**Diff M11** :

- Import `from polycopy.strategy._cache_policy import compute_ttl`.
- Nouvelle dataclass privée `_CacheEntry(market, cached_at, ttl_seconds)`.
- Refactor `self._cache: dict[str, _CacheEntry]` (au lieu de `tuple[datetime, MarketMetadata]`).
- `get_market` : lookup hit si `now - cached_at < entry.ttl_seconds`.
- À l'insertion cache, calculer `ttl = compute_ttl(market, now)` si `settings.strategy_gamma_adaptive_cache_enabled`, sinon `ttl = self.CACHE_TTL.total_seconds()` (fallback 60s uniforme).
- Ajout compteurs `_hits`, `_misses`, log `gamma_cache_hit_rate` toutes les 5 min (§4.4).
- **Invariant préservé** : `CACHE_TTL = timedelta(seconds=60)` reste la constante M2 (fallback).

### 7.5 `src/polycopy/strategy/_cache_policy.py` NOUVEAU

Nouveau fichier, ~50 lignes. Contenu complet §4.2 (pure function `compute_ttl`). Plus helpers privés `_resolve_end_datetime` (à mutualiser avec `pipeline.py:56-72` dans une phase ultérieure — v1 M11 on duplique, cf. risque §11.5).

### 7.6 `src/polycopy/strategy/pipeline.py` (instrumentation)

Voir §7.3 — mêmes modifications.

### 7.7 `src/polycopy/strategy/orchestrator.py` (lignes 45-72 `run_forever` + lines 74-87 `_handle_trade`)

**Diff M11** :

- Ligne 52 (dans `async with httpx.AsyncClient() as http_client`) : instancier `ClobMarketWSClient` conditionnellement si `self._settings.strategy_clob_ws_enabled`.
- TaskGroup : si `ws_client` instancié, `tg.create_task(ws_client.run(stop_event))` en parallèle de la boucle de consommation.
- `SlippageChecker` construit via `run_pipeline` doit recevoir le `ws_client` — passer le client en paramètre de `run_pipeline(..., ws_client=ws_client)`.
- `_handle_trade` : bind `trade_id` contextvar dès réception du DTO (si `trade.trade_id` est présent — cohérent avec §5.1).
- Instrumenter `strategy_filtered_ms` autour de l'appel `run_pipeline`.

### 7.8 `src/polycopy/watcher/data_api_client.py` (ligne 32-60 `get_trades`)

**Diff M11** : aucun changement direct. L'instrumentation stage 1 (`watcher_detected_ms`) se fait dans `wallet_poller.py` (§7.9), pas dans le client HTTP.

### 7.9 `src/polycopy/watcher/wallet_poller.py` (lignes 66-82 `_poll_once`)

**Diff M11** :

- Générer `trade_id = uuid.uuid4().hex` pour chaque trade **nouvellement inséré** (c'est-à-dire `if inserted:`).
- Mesurer `watcher_detected_ms = (now - trade.timestamp_utc).total_seconds() * 1000.0`.
- `bind_contextvars(trade_id=trade_id)`.
- Log `trade_detected` enrichi : `stage_name="watcher_detected_ms"`, `stage_duration_ms=detected_ms`.
- Insert `TradeLatencySample(trade_id, "watcher_detected_ms", detected_ms)` via repo.
- Ajouter champ `trade_id` au DTO propagé par `_publish` pour permettre la suite du pipeline à rebind le contextvar.

### 7.10 `src/polycopy/storage/models.py` (+ `TradeLatencySample`)

**Diff M11** : ajouter la classe `TradeLatencySample` après `PnlSnapshot` (cf. §5.3 code complet).

### 7.11 `src/polycopy/storage/repositories.py` (`TradeLatencyRepository`)

**Diff M11** : ajouter une nouvelle classe `TradeLatencyRepository` (pattern cohérent avec `PnlSnapshotRepository` existante) :

```python
class TradeLatencyRepository:
    """CRUD pour `trade_latency_samples` (M11). Append-only + purge."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert(
        self,
        trade_id: str,
        stage_name: str,
        duration_ms: float,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                TradeLatencySample(
                    trade_id=trade_id,
                    stage_name=stage_name,
                    duration_ms=duration_ms,
                )
            )
            await session.commit()

    async def purge_older_than(self, *, days: int) -> int:
        cutoff = datetime.now(tz=UTC) - timedelta(days=days)
        async with self._session_factory() as session:
            stmt = delete(TradeLatencySample).where(TradeLatencySample.timestamp < cutoff)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0
```

### 7.12 `src/polycopy/storage/dtos.py` (+ `trade_id` sur `DetectedTradeDTO` et `OrderApproved`)

**Diff M11** :

- `DetectedTradeDTO` : ajouter champ `trade_id: str | None = None` (nullable pour backward-compat tests existants).
- `OrderApproved` (strategy/dtos.py:...) : ajouter champ `trade_id: str | None = None`.

Le champ `trade_id` est **métier observability**, pas persisté en DB (les tables `detected_trades` et `my_orders` ne gagnent pas de colonne — seule `trade_latency_samples` porte la liaison).

### 7.13 `src/polycopy/dashboard/routes.py` (+ onglet `/latency`)

**Diff M11** :

- Nouvelle route `@router.get("/latency")` dans `build_pages_router()` (§5.4).
- Ajouter lien `/latency` dans la sidebar (`base.html` ou partial `sidebar.html` cohérent M6).

### 7.14 `src/polycopy/dashboard/queries.py` (p50/p95/p99)

**Diff M11** : ajouter fonction `compute_latency_percentiles(session_factory, *, since)` (§5.4 code complet) + helper `_percentile`.

### 7.15 `src/polycopy/dashboard/templates/latency.html` NOUVEAU

Nouveau template ~80-120 lignes Jinja2 + HTML Tailwind + Chart.js inline. Structure §5.5. Cohérent avec `pnl.html` / `home.html` M6.

### 7.16 `alembic/versions/0005_m11_latency_samples.py` NOUVEAU

Migration §5.3 code complet.

### 7.17 `src/polycopy/cli/runner.py` (purge latency au boot + scheduler quotidien)

**Diff M11** :

- Après `init_db(engine, session_factory, settings.target_wallets)` (ligne ~109) : `await TradeLatencyRepository(session_factory).purge_older_than(days=settings.latency_sample_retention_days)`.
- Dans le TaskGroup principal : `tg.create_task(latency_purge_scheduler.run(stop_event))` (nouveau scheduler léger 24h, ~30 lignes).

### 7.18 `src/polycopy/dashboard/templates/base.html` (sidebar link)

**Diff M11** : ajouter une entrée `/latency` dans la sidebar nav, entre `/pnl` et `/logs` (cohérent ordre d'importance : observability fine-grained).

---

## 8. Plan d'implémentation

Ordre séquentiel, chaque étape testable isolément. Estimé ~2 semaines 1 dev.

### Étape 1 — Capturer fixture message WS CLOB `market` (jour 1 matin)

**Bloquant.** Avant tout code. Deux approches :

- Invoquer skill `/polymarket:polymarket` pour obtenir le schéma officiel messages WS market.
- Ou `scripts/capture_clob_ws_fixture.py` : script standalone qui souscrit 1 token_id (ex: un marché US Elections liquide), enregistre 30 messages dans `tests/fixtures/clob_ws_market_sample.jsonl`, affiche les types observés.

Livrable : `tests/fixtures/clob_ws_market_sample.jsonl` + schéma Pydantic draft dans `src/polycopy/strategy/clob_ws_client.py`.

**STOP si** le schéma diffère significativement de ce que la spec anticipe (ex: pas de `mid` direct, uniquement bid/ask → recalcul côté client) → mettre à jour spec avant de continuer.

### Étape 2 — Migration Alembic 0005 + model + repository (jour 1 après-midi)

- `alembic/versions/0005_m11_latency_samples.py` (§5.3).
- `TradeLatencySample` dans `storage/models.py` (§7.10).
- `TradeLatencyRepository` dans `storage/repositories.py` (§7.11).
- Test `test_m11_alembic_migration_applies_and_rolls_back` (§9.3).

### Étape 3 — Ajouter `trade_id` aux DTOs (jour 2 matin)

- `DetectedTradeDTO.trade_id: str | None = None` (storage/dtos.py).
- `OrderApproved.trade_id: str | None = None` (strategy/dtos.py).
- Non-régression : tests existants doivent passer avec valeur default `None`.

### Étape 4 — `_cache_policy.compute_ttl` pure function + tests (jour 2 matin)

- `src/polycopy/strategy/_cache_policy.py` (§4.2).
- Tests `test_cache_policy_*` (4 tests — §9.3).

### Étape 5 — Refactor `GammaApiClient._cache` derrière interface (jour 2 après-midi)

- Dataclass `_CacheEntry` privée dans `gamma_client.py`.
- Substitution `self._cache: dict[str, _CacheEntry]`.
- `compute_ttl` call selon flag.
- Compteurs `_hits`, `_misses`, log `gamma_cache_hit_rate` 5 min.
- Tests `test_gamma_client_uses_adaptive_ttl_when_enabled`, `test_gamma_client_fallback_to_uniform_ttl_when_disabled` (§9.3).
- Non-régression : `test_gamma_client.py` existant doit passer sans modification (flag default = true).

### Étape 6 — `ClobMarketWSClient` connection + sub/unsub + cache (jour 3-4)

- Nouveau fichier `src/polycopy/strategy/clob_ws_client.py` (§3.2 + §3.4).
- Mock WS serveur via `websockets.serve` sur port local pour tests.
- Tests `test_clob_ws_client_connect_and_subscribe`, `test_clob_ws_client_unsub_after_inactivity`, `test_ws_cache_mid_price_lookup` (§9.3).

### Étape 7 — Reconnect backoff + health check (jour 4-5)

- `_connect` tenacity-wrapped.
- `_health_check_loop` ping/pong 30 s.
- Test `test_clob_ws_client_reconnect_on_disconnect` (kill serveur mock, vérifier reconnect).
- Test `test_ws_connection_status_metric_exposed` (vérifier log `ws_connection_status_change`).

### Étape 8 — Intégration `SlippageChecker` (cache lookup + fallback) (jour 5 matin)

- Modif `SlippageChecker.__init__` + `check` (§3.3).
- Modif `run_pipeline` signature pour accepter `ws_client`.
- Modif `StrategyOrchestrator.run_forever` pour instancier + lancer ws client (§7.7).
- Tests `test_slippage_checker_uses_ws_cache_when_available`, `test_slippage_checker_fallback_to_http_when_ws_down`, `test_slippage_checker_fallback_to_http_when_feature_flag_disabled` (§9.3).
- Non-régression : `test_strategy_pipeline.py::test_slippage_checker_*` (3 tests) doivent passer sans adaptation (flag off ou `ws_client=None`).

### Étape 9 — Feature flags env + validators (jour 5 matin)

- 8 nouveaux champs `Settings` (§7.1, §6.1).
- Tests : valider enum values, bornes int, défauts.
- Tests `test_config.py` étendus.

### Étape 10 — Bind `trade_id` + logging 6 stages (jour 6-7)

- Modif `wallet_poller.py` (§7.9) : stage 1.
- Wrappers `time.perf_counter_ns` autour de `MarketFilter`, `PositionSizer`, `SlippageChecker`, `RiskManager` dans `pipeline.py` (§7.3) : stages 2-5.
- Wrapper dans `executor/orchestrator.py` : stage 6.
- Tests `test_trade_id_bound_in_pipeline_context`, `test_stage_duration_ms_logged_for_each_boundary`, `test_latency_sample_inserted_in_db` (§9.3).

### Étape 11 — Dashboard `/latency` route + query + template (jour 8-9)

- Route `latency_page` dans `routes.py` (§5.4).
- Query `compute_latency_percentiles` (§5.4).
- Template `latency.html` (§5.5).
- Sidebar link dans `base.html` (§7.18).
- Tests `test_latency_page_renders_percentiles`, `test_latency_page_since_filter`, `test_latency_query_p50_p95_p99_computation` (§9.3).

### Étape 12 — Purge 7 jours (boot + scheduler quotidien) (jour 9 après-midi)

- Modif `cli/runner.py` (§7.17).
- Nouveau `LatencyPurgeScheduler` dans `storage/` ou standalone (décision à l'implémentation).
- Test `test_latency_sample_purge_after_7_days` (§9.3).

### Étape 13 — Tests A (WS) complets (jour 10)

Cf. §9.3 sous-section A.

### Étape 14 — Tests B (cache adaptatif) complets (jour 10)

Cf. §9.3 sous-section B.

### Étape 15 — Tests C (instrumentation + dashboard) complets (jour 11)

Cf. §9.3 sous-section C.

### Étape 16 — Smoke test latence réelle (1h d'observation) (jour 11 fin)

- Lancer le bot en dry-run avec `EXECUTION_MODE=dry_run`, `DRY_RUN_REALISTIC_FILL=true`, feature flags tous `true`.
- Observer 1h sur un wallet cible ayant ~10-30 trades/heure.
- Vérifier dashboard `/latency` : p50 < 3 s, p95 < 5 s, p99 < 10 s attendus.
- Vérifier que `gamma_cache_hit_rate` > 70 %.
- Vérifier que le WS n'a pas déconnecté > 3 fois en 1h.
- **STOP et investiguer** si p95 > 5 s — probablement bug dans l'instrumentation (timer pas positionné au bon endroit) ou WS pas effectivement consommé par `SlippageChecker`.

### Étape 17 — README + CLAUDE.md + docs/architecture.md updates (jour 12)

- README section "Architecture & stack" : ajouter mention WS CLOB + lien `/latency`.
- CLAUDE.md §APIs Polymarket : étendre bullet "CLOB WebSocket" (cf. §10).
- CLAUDE.md Conventions : ajouter bullet "Instrumentation latence (M11+)".
- docs/architecture.md §Module Strategy : mentionner cache adaptatif + WS.
- docs/setup.md §18 : smoke test `/latency`.
- `.env.example` : bloc §6.3.

Commit final unique : `feat(strategy,dashboard,storage): M11 real-time pipeline phase 1 (WS CLOB + adaptive cache + latency instrumentation)`.

---

## 9. Tests

### 9.1 À protéger (existants à ne pas casser)

Liste des tests existants qui **doivent continuer à passer** sans modification :

| Fichier | Test | Raison |
|---|---|---|
| `tests/unit/test_config.py:*` | tous | Non-régression config (nouveaux champs optionnels) |
| `tests/unit/test_gamma_client.py:*` | tous | Cache gamma non-régression avec flag default=true (adaptive passe les mêmes assertions pour TTL 300s > 60s) |
| `tests/unit/test_gamma_client_top_markets.py:*` | tous | `list_top_markets` inchangé |
| `tests/unit/test_clob_read_client.py:*` (si existe) | tous | Fallback HTTP inchangé |
| `tests/unit/test_strategy_pipeline.py:184-214` | `test_slippage_checker_no_orderbook`, `test_slippage_checker_exceeded`, `test_slippage_checker_pass` | **Critique** : comportement `SlippageChecker` sans `ws_client` (None) doit être strictement M2 |
| `tests/unit/test_strategy_pipeline.py:*` | `test_market_filter_*`, `test_position_sizer_*`, `test_risk_manager_*` | Non-régression pipeline chain |
| `tests/unit/test_strategy_orchestrator.py:*` (si existe) | tous | Orchestration non-régression |
| `tests/unit/test_watcher_*.py:*` | tous | Watcher non-régression (trade_id champ optionnel nullable) |
| `tests/unit/test_dashboard_routes.py:*` | tous | Routes existantes inchangées |
| `tests/unit/test_dashboard_logs_route.py:*` | tous | M10 preset + exclusion inchangés |
| `tests/unit/test_pnl_snapshot_writer.py:*` + `test_pnl_writer_m8_mode.py:*` + `test_pnl_writer_m10_parity.py:*` | tous | **Invariant M10 kill switch 3 modes** — M11 ne touche pas |
| `tests/unit/test_executor_orchestrator.py:*` | tous | 4 garde-fous M3/M8/M10 préservés |
| `tests/unit/test_pipeline_m8_branch.py:*` | tous | Branche M8 realistic inchangée |
| `tests/unit/test_dashboard_security.py` + `test_dashboard_security_m6.py` | tous | Invariants sécurité dashboard inchangés |
| `tests/unit/test_middleware_log_filter.py:*` | tous | Processor M10 inchangé |
| `tests/unit/test_telegram_badge.py:*` + `test_telegram_template_rendering.py:*` | tous | Badges M10 inchangés (M11 n'ajoute aucun template) |

### 9.2 À adapter (signature evolutions)

Tests qui doivent être mis à jour car la signature d'un constructeur ou helper change :

| Test | Changement |
|---|---|
| `test_strategy_pipeline.py::test_slippage_checker_*` (3 tests) | `SlippageChecker(clob_client, settings)` → accepte **en plus** `ws_client: ClobMarketWSClient | None = None` (default None). Les appels existants marchent tels quels (kwarg optionnel). Pas d'adaptation nécessaire en théorie — **vérifier en implémentation**. |
| `test_strategy_pipeline.py::test_run_pipeline_*` | `run_pipeline(trade, ..., settings)` gagne un kwarg optionnel `ws_client: ClobMarketWSClient | None = None`. Backward-compat si None (fallback HTTP). |
| `tests/unit/test_strategy_orchestrator.py` (si existe) | `StrategyOrchestrator` instancie le `ClobMarketWSClient` conditionnellement. Tests existants qui monkey-patch `ClobReadClient` doivent vérifier que le ws_client n'est pas instancié si `strategy_clob_ws_enabled=false` (ou injecter un mock). |

### 9.3 À ajouter (nouveaux) — inventaire exhaustif

Tests nouveaux M11 regroupés par sous-feature.

#### 9.3.A — WebSocket CLOB (7 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_clob_ws_client_connect_and_subscribe` | `tests/unit/test_clob_ws_client.py` (nouveau) | Mock WS serveur local, ws client se connecte, envoie message sub, reçoit `book` snapshot, cache alimenté |
| `test_clob_ws_client_reconnect_on_disconnect` | idem | Serveur mock ferme la connection, client retry avec backoff, status transitionne `up→reconnecting→up` |
| `test_clob_ws_client_unsub_after_inactivity` | idem | `last_seen[token_id]` > 5 min, gc loop envoie unsub, cache retiré |
| `test_ws_cache_mid_price_lookup` | idem | Push message `price_change` sur token_id X, `get_mid_price(X)` retourne la valeur poussée |
| `test_ws_cache_stale_returns_none` | idem | Pas de push depuis > `_CACHE_TTL_WS_SECONDS`, `get_mid_price` retourne None → fallback HTTP enclenché |
| `test_ws_connection_status_metric_exposed` | idem | Transition `up→down` log `ws_connection_status_change` avec `status="down"` |
| `test_ws_max_subscribed_cap_enforced` | idem | `STRATEGY_CLOB_WS_MAX_SUBSCRIBED=3`, sub 4 tokens → le plus ancien (LRU) unsub |

#### 9.3.B — SlippageChecker intégration WS (3 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_slippage_checker_uses_ws_cache_when_available` | `tests/unit/test_strategy_pipeline.py` (ajout) | `ws_client.get_mid_price` retourne 0.08 → `SlippageChecker.check` utilise 0.08, n'appelle pas `ClobReadClient.get_midpoint` |
| `test_slippage_checker_fallback_to_http_when_ws_down` | idem | `ws_client.get_mid_price` retourne None (WS down) → `ClobReadClient.get_midpoint` appelé, résultat utilisé |
| `test_slippage_checker_fallback_to_http_when_feature_flag_disabled` | idem | `settings.strategy_clob_ws_enabled=False`, `ws_client` passé quand même → ignoré, HTTP direct |

#### 9.3.C — Cache Gamma adaptatif (6 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_cache_policy_resolved_market_infinite_ttl` | `tests/unit/test_cache_policy.py` (nouveau) | `market.closed=True` → TTL `_TTL_RESOLVED_SENTINEL` |
| `test_cache_policy_near_resolution_short_ttl` | idem | `end_date - now = 30min` → TTL 10 s |
| `test_cache_policy_active_market_medium_ttl` | idem | `volume_24h_usd = 500` → TTL 300 s |
| `test_cache_policy_inactive_market_long_ttl` | idem | default → TTL 3600 s |
| `test_gamma_client_uses_adaptive_ttl_when_enabled` | `tests/unit/test_gamma_client.py` (ajout) | `settings.strategy_gamma_adaptive_cache_enabled=True` + marché résolu → 2ᵉ call pas de HTTP refetch même après 61 s |
| `test_gamma_client_fallback_to_uniform_ttl_when_disabled` | idem | Flag off → TTL 60 s M2 (HTTP refetch après 61 s peu importe le segment) |

#### 9.3.D — Instrumentation latence (7 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_trade_id_bound_in_pipeline_context` | `tests/unit/test_latency_instrumentation.py` (nouveau) | `wallet_poller` génère `trade_id`, propagé au DTO, rebind dans `strategy/orchestrator`, présent dans tous les logs du trade |
| `test_stage_duration_ms_logged_for_each_boundary` | idem | Run un trade end-to-end (stub), vérifier 6 logs `stage_complete` avec `stage_name ∈ {...}` |
| `test_latency_sample_inserted_in_db` | idem | Après 1 trade, `SELECT COUNT(*) FROM trade_latency_samples` = 6 |
| `test_latency_sample_purge_after_7_days` | idem | Insert sample avec `timestamp = now - 8d`, `purge_older_than(days=7)` → retiré |
| `test_latency_page_renders_percentiles` | `tests/unit/test_dashboard_latency_route.py` (nouveau) | Route `/latency` renvoie HTML avec p50/p95/p99 sur au moins 1 stage |
| `test_latency_page_since_filter` | idem | `?since=1h` vs `?since=24h` : fenêtres différentes produisent counts différents |
| `test_latency_query_p50_p95_p99_computation` | idem | Query retourne valeurs correctes sur dataset fixture (10 samples, vérifier percentiles exacts) |

#### 9.3.E — Migration + config (2 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_m11_alembic_migration_applies_and_rolls_back` | `tests/integration/test_m11_migration.py` (nouveau, opt-in) | `alembic upgrade 0005` crée la table, `alembic downgrade 0004` la supprime |
| `test_m11_config_defaults` | `tests/unit/test_config.py` (ajout) | Tous les nouveaux env vars ont les défauts attendus |

**Total** : 25 nouveaux tests unit + 1 intégration. Couverture cible ≥ 80 % sur `strategy/clob_ws_client.py`, `strategy/_cache_policy.py`, `storage/repositories.py` (partie `TradeLatencyRepository`), `dashboard/queries.py` (partie `compute_latency_percentiles`).

---

## 10. Impact CLAUDE.md — texte de remplacement exact

Cf. synthèse §8.4 et §8.5. 3 passages à insérer / modifier.

### 10.1 Section "APIs Polymarket utilisées"

**Actuel (lignes 67-68)** :

```
- **CLOB WebSocket** : `wss://ws-subscriptions-clob.polymarket.com`
  - Channel `market` pour les prix temps réel
```

**Remplacer par** :

```
- **CLOB WebSocket** : `wss://ws-subscriptions-clob.polymarket.com`
  - Channel `market` pour les prix temps réel.
  - **Utilisé à M11** par `SlippageChecker` via `ClobMarketWSClient` (cache in-memory mid-price, lazy sub sur les `token_id` candidats, unsub après 5 min d'inactivité, health check 30 s, reconnect tenacity). Fallback HTTP `/midpoint` transparent si WS down ou `STRATEGY_CLOB_WS_ENABLED=false`. Read-only public — aucune creds touchée.
  - Types de messages consommés : `book` (snapshot orderbook), `price_change`, `last_trade_price`, `best_bid_ask`, `market_resolved`. Schéma exact capturé dans `tests/fixtures/clob_ws_market_sample.jsonl`.
```

### 10.2 Section "Conventions de code"

**Ajouter** après le bullet "Modes d'exécution (M10+)" :

```
- **Instrumentation latence (M11+)** : `structlog.contextvars.bind_contextvars(trade_id=...)` en tête de pipeline. 6 stages mesurés : `watcher_detected_ms`, `strategy_enriched_ms`, `strategy_filtered_ms`, `strategy_sized_ms`, `strategy_risk_checked_ms`, `executor_submitted_ms`. Persistance append-only dans `trade_latency_samples` (purge 7 jours). Dashboard `/latency` rend les p50/p95/p99. Feature flag `LATENCY_INSTRUMENTATION_ENABLED=false` désactive si surcharge CPU mesurée.
```

### 10.3 Section "Sécurité — RÈGLES STRICTES"

**Ajouter** (après bullet M10 "Dry-run M8") :

```
- **Pipeline temps réel M11** : le `ClobMarketWSClient` consomme **exclusivement** le channel `market` (public, read-only) — pas de canal `user`, pas d'auth L1/L2, pas de signature. Les creds CLOB restent confinées au chemin live M3. Si `STRATEGY_CLOB_WS_ENABLED=false`, aucune connexion WS ouverte, aucune nouvelle surface. Le cache Gamma adaptatif (`_cache_policy.compute_ttl`) est 100 % en mémoire, aucune creds impliquée. La table `trade_latency_samples` contient uniquement `trade_id` (uuid hex interne, pas une adresse wallet), `stage_name`, `duration_ms`, `timestamp` — **aucun secret, aucun PII**, vérifiable via `test_latency_no_secret_leak.py` (à ajouter §9.3).
```

### 10.4 Section "Architecture (rappel)"

**Ajouter** un bullet dans le diagramme (après `monitoring/`) :

```
├── strategy/
│   ├── clob_ws_client.py   WebSocket CLOB `market` cache (M11)
│   └── _cache_policy.py    TTL adaptatif Gamma (M11)
```

(ou mettre à jour la vue texte du package strategy de manière concise).

---

## 11. Risques et mitigations

### 11.1 Risque critique — WebSocket Polymarket instable

**Scénario** : Polymarket WS tombe en production (maintenance non annoncée, congestion, rate limit non documenté) → `ClobMarketWSClient` bascule en `status="down"` → `SlippageChecker` fallback HTTP → latence regresse à 500-1500 ms par trade → gain M11 partiellement annulé.

**Impact** : latence régresse vers ~5-7 s au lieu de 2-3 s. Toujours mieux qu'avant M11. Pas de perte fonctionnelle.

**Mitigations** :

1. Reconnect backoff exponentiel tenacity (§3.2) : récupère automatiquement après une déconnexion.
2. Metric `ws_connection_status_change` loggée à chaque transition → visible dans `/logs`.
3. Si la déconnexion est durable (> 1h), un user peut désactiver le flag `STRATEGY_CLOB_WS_ENABLED=false` et revenir au comportement M2..M10 strict.
4. Documentation `docs/setup.md` §18 : section "Diagnostiquer un WS down".
5. Alerte Telegram reportable M11.1 si besoin (pas dans scope v1).

### 11.2 Risque moyen — cache TTL trop long → stale price → slippage réel diverge du check

**Scénario** : marché segmenté "actif" à TTL 300 s, prix bouge de 5 % entre 2 checks → `SlippageChecker` valide sur prix obsolète → ordre POST à un prix qui a dérivé → slippage réel > `MAX_SLIPPAGE_PCT` constaté par CLOB → ordre rejeté ou fill dégradé.

**Impact** : **faux négatif** (ordre validé qui n'aurait pas dû l'être). Comportement dégradé mais pas catastrophique — la CLOB protège via FOK (ordre non filled si prix dérive trop).

**Mitigations** :

1. **Segmentation "proche résolution"** : les 60 dernières minutes avant résolution (TTL 10 s) sont précisément la fenêtre où la volatilité est max → resserre le cache automatiquement.
2. WS mid-price court-circuite le cache Gamma (stage `strategy_enriched_ms` concerne Gamma metadata, pas les prix).
3. Test de stress à imaginer en M12 backtest : comparer rejet CLOB avec/sans cache adaptatif.
4. Documentation `CLAUDE.md` §APIs Polymarket : mention explicite que le cache Gamma ne porte PAS les prix, seulement les metadata marché.

### 11.3 Risque moyen — instrumentation latence = overhead CPU → pipeline plus lent qu'avant

**Scénario** : 6 `time.perf_counter_ns` + 6 `log.info` + 6 `session.add(TradeLatencySample)` par trade ajoutent du CPU. À 50 trades/heure = 300 logs + 300 inserts/h, négligeable. À 1000 trades/h = 6000 logs + 6000 inserts/h, potentiellement mesurable.

**Impact** : p95 du pipeline lui-même peut monter de quelques ms à cause de l'instrumentation.

**Mitigations** :

1. **Mesurer** avec et sans `LATENCY_INSTRUMENTATION_ENABLED=true` sur le smoke test §8 étape 16.
2. Si overhead > 50 ms p95 : batcher les inserts `TradeLatencySample` via une queue in-memory + un task consumer qui flush toutes les 10 s (au lieu d'un insert synchrone par stage).
3. Flag off si surcharge : le bot fonctionne sans instrumentation (observability perdue mais pipeline non impacté).
4. Documenter dans CLAUDE.md §Conventions "Instrumentation latence (M11+)" la possibilité de désactiver le flag.

### 11.4 Risque faible — migration 0005 sur DB user préexistante

**Scénario** : un user avec DB M10 pull `main` post-M11 → `init_db` applique `alembic upgrade head` → 0005 crée la table.

**Impact** : aucun, migration additive (pas de schema change sur tables existantes).

**Mitigations** :

1. Test d'intégration `test_m11_alembic_migration_applies_and_rolls_back` (§9.3.E).
2. Downgrade proprement testé.
3. Documentation `docs/setup.md` §18 : mention de la migration.

### 11.5 Risque faible — duplication `_resolve_end_datetime` entre `pipeline.py` et `_cache_policy.py`

**Scénario** : le helper privé `_resolve_end_datetime(end_date, end_date_iso)` de `pipeline.py:56-72` est dupliqué dans `_cache_policy.py` (§4.2). À terme drift entre les deux = bug subtil.

**Impact** : entretien (ne pas oublier de modifier les deux si un champ Gamma change).

**Mitigations** :

1. v1 M11 : dupliquer délibérément (rule of three pas atteinte selon CLAUDE.md convention, 2 usages OK).
2. v1.1 / M12 : extraire dans `strategy/dtos.py` comme méthode `MarketMetadata.resolve_end_datetime(self) -> datetime | None`.
3. Documenter en TODO inline dans le code.

### 11.6 Risque faible — feature flag off → régression M2/M3 possible si code mal factorisé

**Scénario** : `STRATEGY_CLOB_WS_ENABLED=false` + `STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED=false` + `LATENCY_INSTRUMENTATION_ENABLED=false` → doit reproduire exactement le comportement M2..M10.

**Impact** : si le refactor `GammaApiClient._cache` a introduit un bug subtil (ex: compteurs `_hits`/`_misses` incrémentés même en flag off), la non-régression M2..M10 n'est pas garantie.

**Mitigations** :

1. Tests `test_gamma_client_fallback_to_uniform_ttl_when_disabled`, `test_slippage_checker_fallback_to_http_when_feature_flag_disabled` (§9.3).
2. Faire tourner le sous-ensemble de tests M2..M10 avec tous les flags off en pre-merge check.
3. Documenter le contrat "flag off = M2..M10 strict" dans le code (docstrings).

### 11.7 Risque faible — 500 tokens subscribés simultanément → mémoire WS client

**Scénario** : un user avec 50 wallets cibles × ~10 tokens distincts en moyenne / heure → jusqu'à 500 tokens vus/heure. Si chacun reste dans `self._subscribed` pendant 5 min, pic à ~250 simultanés. Acceptable.

**Impact** : mémoire ws_client ≈ 250 × ~200 octets = ~50 Ko. Négligeable.

**Mitigations** :

1. Cap dur `STRATEGY_CLOB_WS_MAX_SUBSCRIBED=500` (§6.1).
2. LRU unsub au-delà (§3.4 étape 6).
3. Test `test_ws_max_subscribed_cap_enforced` (§9.3.A).

### 11.8 Risque faible — SIMULATION mode et instrumentation latence

**Scénario** : `EXECUTION_MODE=simulation` → pipeline tourne sur fixtures (pas de réseau). Les stages `watcher_detected_ms` et `strategy_enriched_ms` mesurent des timings locaux (lecture fixture vs HTTP réel) — valeurs artificielles.

**Impact** : samples collectés en SIMULATION mélangés aux samples DRY_RUN / LIVE dans `trade_latency_samples`.

**Mitigations** :

1. Ajouter colonne `execution_mode` à `trade_latency_samples` ? **Non v1** (scope strict, cf. open question §13.2). Le dashboard `/latency` peut filtrer a posteriori si besoin.
2. Documenter : "samples en SIMULATION sont à interpréter comme relative, pas absolu".

---

## 12. Rollout / migration

### 12.1 Séquence

Cohérent avec synthèse §7.2 (ordre M10 → M11 → M12).

1. **T0** — Merge spec M11 sur `main` (sans code).
2. **T0 + 7-10 jours** — PR code M11 mergée derrière les 3 feature flags (tous `true` par défaut). Kill switch rollback dispo : `STRATEGY_CLOB_WS_ENABLED=false`, `STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED=false`, `LATENCY_INSTRUMENTATION_ENABLED=false` → comportement M2..M10 strict.
3. **T0 + 10 jours** — README + CLAUDE.md + docs/architecture.md + docs/setup.md dans le **même** commit.
4. **T0 + 2 semaines** — Smoke test 24h avec WS actif en prod user, monitor reconnect rate (< 3/h attendu) + p95 latence < 5 s. Dashboard `/latency` analysé pour valider les gains.
5. **T0 + 3-4 semaines** — Si aucune régression signalée, conserver les 3 flags en `true` par défaut.
6. **Version+2 (T0 + ~6 semaines)** — Décision sur le retrait des flags ET du fallback HTTP polling (si WS fiable, on peut supprimer le path HTTP M2 dans `SlippageChecker`). Reporté post-M11 si feedback utilisateur demande la prudence.

### 12.2 Rollback

Si régression critique post-merge :

- **Option A (runtime)** : flip les 3 flags à `false` via `.env` → restaure M2..M10 strict. Aucun redéploiement code.
- **Option B (git)** : revert du commit M11. Migration 0005 reste appliquée (table vide inutilisée, pas de problème).

**Décision** : privilégier option A pour T0+10j à T0+3 sem., passer à option B au-delà si bug structurel.

### 12.3 Communication

- CHANGELOG entry détaillé.
- Section README "Breaking changes M10 → M11" : **aucune breaking change** — ajouts additifs uniquement.
- Warning CLI au 1er boot post-M11 : "**M11** : latency instrumentation + WS CLOB enabled. Dashboard `/latency` pour observer p50/p95/p99. Flags : STRATEGY_CLOB_WS_ENABLED, STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED, LATENCY_INSTRUMENTATION_ENABLED (all default true)".

---

## 13. Open questions

Questions dont la réponse n'est pas critique pour démarrer l'implémentation mais à trancher avant cutover final.

1. **RTDS vs WebSocket CLOB classique ?** Synthèse §6.6 : RTDS (Real-Time Data Socket, canaux `prices`, `activity`, `clob_market`) est une alternative à `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Potentiellement plus large (breadth), potentiellement moins latent sur orderbook pur. **Décision v1 M11** : CLOB WS classique (canal `market`), RTDS reporté. Benchmark post-M11 sur données réelles pour décider. Si RTDS gagne, spec M11.1 migration.
2. **Instrumentation stages en SIMULATION mode** : v1 oui (observabilité backtest), pas de filtre côté `trade_latency_samples`. Reportable si trop verbeux (ajout colonne `execution_mode`).
3. **Purge 7 jours : scheduler dédié ou query boot + quotidien ?** v1 query boot + scheduler quotidien léger (§5.6, §7.17). Pas de scheduler dédié top-level.
4. **Metric `ws_connection_status` : exposée via `/api/metrics` JSON ou juste log ?** v1 log only + attribut lisible `app.state`. `/api/metrics` dashboard reportable.
5. **Mid-price WS update rate (fréquence push Polymarket)** : à mesurer post-capture fixture §8 étape 1. Dimensionne le `_CACHE_TTL_WS_SECONDS=60` — si Polymarket push rarement (ex: toutes les 30 s), 60 s est OK ; si push pas stable (ex: 5 min silence sur marché calme), relever à 300 s avec alerte "ws_price_stale".
6. **Cap `STRATEGY_CLOB_WS_MAX_SUBSCRIBED` vs couverture** : 500 = sécurité, pourrait être relevé si le bot suit 100+ wallets. À valider en prod.
7. **Batching des inserts `TradeLatencySample`** : v1 synchrone (1 insert par stage). Si overhead mesuré §11.3 > 50 ms p95, batcher via queue. Reportable.
8. **Fixture WS capture : mock offline ou test live ?** v1 fixture JSONL offline capturé une fois en début d'étape 1. Test intégration live opt-in (marqué `@pytest.mark.integration`). Décision validée.
9. **`trade_id` persisté dans `detected_trades` en plus de `trade_latency_samples` ?** v1 non — le `trade_id` est pur observability, pas métier. Si M12 / M13 en a besoin pour join cross-table, envisager migration 0006 qui ajoute `trade_id` à `detected_trades`, `my_orders`, `my_positions`.
10. **Phase 2 activation** (parallélisation pipeline + WS user channel) : décision post-M11 mesure. Si p95 > 2 s persistant, engager phase 2. Si p95 < 1.5 s, prioriser M12 scoring v2.

---

## 14. ROADMAP des améliorations latence reportées

À **ne pas oublier** (référence pour planification M12+). Ces hors scopes sont validés par la synthèse mais non inclus dans M11 par scope strict.

### 14.1 Phase 2 — M11-bis (optionnel, ~1 mois effort)

Cf. synthèse §4.3. Déclencheur : p95 post-M11 > 2 s durable OU M12 montre edge sur trades < 1 s.

- **Parallélisation strategy pipeline** (`asyncio.gather` sur `MarketFilter` + `SlippageChecker cache-lookup` + `PositionSizer` — `RiskManager` reste séquentiel car dépend du size). Gain : 0.5-1 s.
- **Watcher WebSocket user channel** : remplace polling Data API `/activity` 5 s. Gain : ~5-6 s (énorme). Complexité : multi-wallet connection pool, reconnection robuste, backoff par wallet.

### 14.2 Phase 3 — M14+ (optionnel, ~2 mois effort)

Cf. synthèse §4.4. Déclencheur : analyse post-M11-bis montre > 10 % d'opportunités ratées à cause de latence résiduelle + edge démontré sur trades sub-seconde.

- **Goldsky Turbo Pipelines** : webhooks directs Polygon RPC. Gain stage 1 : 5-7 s → < 50 ms. **Coût : $$$ (abonnement payant).**
- **Bitquery Kafka streaming** : alternative Goldsky Turbo. Similar gain, différent vendor.
- **Multi-process workers** : asyncio → multiprocessing Python sur plusieurs cœurs. Rejeté v1 par synthèse §4.1. Réévaluer si CPU-bound avéré.
- **Colocalisation VPS** (QuantVPS ou équivalent) : latence réseau → matching engine Polymarket réduite. Coût : VPS premium.
- **Pré-signature ordres en batch** : précalcul signature EOA hors chemin critique. Gain ~85 ms → ~2 ms stage "signature". Refactor executor.
- **Taker speed bump incompressible** : ~250 ms imposés par Polymarket, hors de notre contrôle.

### 14.3 Autres pistes vues mais hors scope latence

- **MEV defense Polygon** (Gemini §6.1, synthèse §6.4) : protection contre sandwich / frontrun par un builder / relayer privé. M18+.
- **Market making Avellaneda-Stoikov** (Gemini §6.3, synthèse §6.5) : modèle académique gestion inventaire marché multi-side. Sophistication institutionnelle, M18+.
- **Taker fees dynamiques** (Gemini §6.2, synthèse §6.1) : M13 — spec dédiée. Protège l'EV quand Polymarket active les fees sur neg-risk / HFT markets.
- **Apify Leaderboard Scraper** (Gemini §1.1, synthèse §6.3) : discovery M5 alternatif. Évaluation coût vs current `/holders` + `/trades` reverse-engineering en début de M12.

Ces pistes sont **référencées** ici pour ne pas les perdre. Elles ne font partie d'aucune spec active — chaque activation nécessite une spec dédiée quand le contexte le justifie.

---

## 15. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src --strict
pytest --cov=src/polycopy/strategy --cov=src/polycopy/storage --cov=src/polycopy/dashboard \
  --cov=src/polycopy/watcher --cov=src/polycopy/cli \
  --cov-report=term-missing   # ≥ 80% sur modules touchés
pytest -m integration          # inclut test_m11_alembic_migration

# Smoke test M11 (latence réelle)
EXECUTION_MODE=dry_run DRY_RUN_REALISTIC_FILL=true \
STRATEGY_CLOB_WS_ENABLED=true \
STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED=true \
LATENCY_INSTRUMENTATION_ENABLED=true \
DASHBOARD_ENABLED=true \
python -m polycopy --verbose &
sleep 3600   # 1h observation
# Ouvrir http://127.0.0.1:8787/latency : vérifier p95 < 5s, p99 < 10s
# Vérifier logs : grep gamma_cache_hit_rate > 0.7
# Vérifier : grep ws_connection_status_change → peu de transitions
kill %1 && wait

# Smoke rollback (flags off)
STRATEGY_CLOB_WS_ENABLED=false \
STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED=false \
LATENCY_INSTRUMENTATION_ENABLED=false \
python -m polycopy --verbose
# Vérifier : comportement M2..M10 strict (pas de connexion WS ouverte,
# pas d'insert dans trade_latency_samples, TTL Gamma 60 s uniforme)
```

---

## 16. Critères d'acceptation

- [ ] `ClobMarketWSClient` se connecte à `wss://ws-subscriptions-clob.polymarket.com/ws/market`, sub / unsub proprement, cache mid-price alimenté par messages `price_change` / `last_trade_price`, reconnect tenacity, health check 30 s, unsub après 5 min inactivité.
- [ ] Fixture `tests/fixtures/clob_ws_market_sample.jsonl` capturée et ≥ 7 types de messages documentés.
- [ ] `SlippageChecker` consulte WS cache en priorité, fallback HTTP si cache vide / flag off / WS down. Non-régression `test_slippage_checker_*` M2.
- [ ] `_cache_policy.compute_ttl` est une fonction pure, 4 segments testés isolément (résolu / proche résolution / actif / inactif).
- [ ] `GammaApiClient` utilise TTL adaptatif si `STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED=true`, fallback TTL 60 s sinon. Metric `gamma_cache_hit_rate` loggé toutes les 5 min.
- [ ] `trade_id` généré dans `wallet_poller._poll_once` pour chaque nouveau trade, propagé via DTO, bindé contextvars dans strategy + executor.
- [ ] 6 logs `stage_complete` avec `stage_name` et `stage_duration_ms` émis par trade end-to-end. 6 rows `trade_latency_samples` insérées.
- [ ] Migration Alembic `0005_m11_latency_samples` applique et rolls back.
- [ ] Table `trade_latency_samples` purgée après 7 jours (scheduler quotidien + query boot).
- [ ] Dashboard `/latency` rend bar chart p50/p95/p99 par stage. Filtre `?since=1h|24h|7d` fonctionne.
- [ ] `ws_connection_status_change` loggé à chaque transition (`up → reconnecting → up`). Attribut `app.state.ws_client` lisible par le dashboard.
- [ ] Cap `STRATEGY_CLOB_WS_MAX_SUBSCRIBED=500` enforced, LRU unsub au-delà.
- [ ] 3 feature flags (`STRATEGY_CLOB_WS_ENABLED`, `STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED`, `LATENCY_INSTRUMENTATION_ENABLED`) tous `true` par défaut. Si tous `false` → comportement M2..M10 strict (non-régression absolue).
- [ ] **Invariants M10 préservés** : kill switch 3 modes, badge Telegram, processor `filter_noisy_endpoints`, exclusion `/logs` default, 4 garde-fous M3/M8. Tests `test_pnl_writer_m10_parity.py`, `test_telegram_badge.py`, `test_middleware_log_filter.py`, `test_dashboard_logs_route.py` passent inchangés.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src --strict` : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `src/polycopy/strategy/clob_ws_client.py`, `_cache_policy.py`, `storage/repositories.py` (partie M11), `dashboard/queries.py` (partie latence). Non-régression M1..M10 ≥ 80 %.
- [ ] Smoke test 1h : p95 latence < 5 s, hit rate Gamma > 70 %, < 3 reconnects WS.
- [ ] Doc updates §10 (CLAUDE.md) + README `/latency` + `docs/architecture.md` M11 + `docs/setup.md` §18 smoke + `.env.example` bloc §6.3 dans le **même** commit.
- [ ] Commit final unique : `feat(strategy,dashboard,storage): M11 real-time pipeline phase 1 (WS CLOB + adaptive cache + latency instrumentation)`.

---

## 17. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M11

Suis specs/M11-realtime-pipeline-phase1.md à la lettre. Invocation skill
/polymarket:polymarket REQUISE pour capturer le schéma des messages WS
`market` avant le moindre code (§8 étape 1).

Avant tout code, actions obligatoires :

1. Capturer fixture WS CLOB market :
   - Option A : skill /polymarket:polymarket
   - Option B : scripts/capture_clob_ws_fixture.py (1 token liquide, 30 messages)
   - Livrable : tests/fixtures/clob_ws_market_sample.jsonl + schéma Pydantic
     draft dans src/polycopy/strategy/clob_ws_client.py

2. Vérifier que les feature flags M10 sont toujours présents :
   grep -E "EXECUTION_MODE|filter_noisy_endpoints|_DEFAULT_EXCLUDED_EVENTS" src/polycopy/ -r

3. Capturer l'état des tests existants :
   pytest tests/unit/test_gamma_client.py tests/unit/test_strategy_pipeline.py
     tests/unit/test_dashboard_routes.py tests/unit/test_config.py -v
     | tee /tmp/m11_baseline.txt

Ensuite suis l'ordre §8 (17 étapes séquentielles).

Contraintes non négociables :

- 3 feature flags tous `true` par défaut. Flag off = comportement M2..M10 strict
  (non-régression absolue).
- Invariants M10 préservés : kill switch 3 modes, badge templates, processor
  filter_noisy_endpoints, exclusion /logs default, 4 garde-fous M3/M8.
- Fixture WS capturée AVANT le code client (§8 étape 1 bloquant).
- Stages latence : 6 exactement, pas de dérive. `time.perf_counter_ns` pour précision.
- Rétention 7 jours stricte (purge boot + quotidien).
- `trade_latency_samples` = nouvelle table, zéro modif sur existantes.
- Migration 0005 additive, downgrade propre.
- WS CLOB = read-only public, aucune creds touchée.
- `STRATEGY_CLOB_WS_MAX_SUBSCRIBED=500` cap dur avec LRU.
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog,
  docstrings FR / code EN, pas de print).
- mypy --strict propre, ruff propre, pytest ≥ 80% coverage sur modules
  modifiés. Non-régression M1..M10 ≥ 80%.

Demande-moi confirmation avant tout patch sensible :
- config.py : ajout 8 nouveaux champs Settings.
- strategy/pipeline.py : modif signature SlippageChecker + run_pipeline.
- strategy/gamma_client.py : refactor cache derrière _CacheEntry.
- watcher/wallet_poller.py : génération trade_id + stage 1.
- Migration Alembic 0005 (nouvelle table).
- Dashboard route /latency + sidebar link dans base.html.
- CLAUDE.md : 3 remplacements §10.

Si une zone §13 open question devient bloquante (ex: fixture WS montre un
schéma incompatible, overhead latence > 50 ms, cap subscribed pas
supporté), STOP et signale.

Smoke test final obligatoire avant merge :
- 1h observation dry-run : p95 < 5s, p99 < 10s.
- gamma_cache_hit_rate > 70% dans les logs.
- < 3 reconnects WS / heure.
- Rollback : 3 flags false → comportement M2..M10 strict confirmé.
- Migration 0005 apply + downgrade OK.

Commit unique : feat(strategy,dashboard,storage): M11 real-time pipeline
phase 1 (WS CLOB + adaptive cache + latency instrumentation)
```
