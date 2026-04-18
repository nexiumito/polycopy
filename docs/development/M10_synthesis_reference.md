# Synthèse M10 — Référence de conception (scoring v2, latence, parité, hygiène)

Consolidation des deux deep-searches (`gemini_deep_search_v2_and_more.md` et
`perplexity_deep_search_v2_and_more.md`) augmentée de l'audit code interne
réalisé en session brainstorming du 2026-04-18.

**Ce document tranche là où les deux sources divergent** et sert de référence
unique pour draftère les specs M13+. Quand un point est débattable, la décision
retenue est **signalée explicitement** ; les alternatives sont listées pour
que le contre-argument reste disponible.

Ce document n'est pas une spec. C'est la charpente qui permettra d'écrire les
specs M13, M14, M15 (et au-delà) en partant d'une base alignée.

---

## 0. Executive summary

### Priorités d'implémentation retenues

| # | Livrable | Spec cible | Effort | Impact | Prérequis |
|---|---|---|---|---|---|
| 1 | **Log hygiene + dry-run/live parity** (bundle) | M15 | S (1 sem.) | Dégage le terrain pour tout le reste : unblock debug, unblock tests v2 | Aucun |
| 2 | **WebSocket CLOB + instrumentation latence** (phase 1) | M14 | M (2 sem.) | Latence 10-15 s → ~3-4 s. Base pour valider scoring v2 sur pipeline rapide | M15 mergé |
| 3 | **Scoring v2** (formule consolidée, shadow period) | M13 | M (2-3 sem.) | Cœur du produit : qualité des wallets suivis | M14 mergé (backtest sur pipeline rapide) |
| 4 | Parallélisation strategy + workers dédiés (phase 2) | M14-bis | M | Latence ~3 s → <1 s | M14 phase 1 |
| 5 | Taker fees dynamiques + RTDS | M16 | S | Préserve l'EV quand Polymarket active les fees sur neg-risk/HFT markets | M14 |
| 6 | Goldsky Turbo / Bitquery streaming | M17+ | L | Détection on-chain <100 ms, justifie arbitrages spatiaux | M14 |
| 7 | MEV defense + market making Avellaneda-Stoikov | M18+ | XL | Sophistication institutionnelle | Tout le reste |

### Décisions clés prises dans ce doc

1. **Formule scoring v2 hybride** (ni la version "agressive" de Gemini ni la
   version "prudente" de Perplexity — voir §1.2).
2. **Trois modes d'exécution** (SIMULATION / DRY_RUN / LIVE) au lieu du
   binaire actuel (voir §3.1).
3. **Latence cible phase 1 : 2-3 s** (et non 347 ms prescrit par Gemini — un
   single-VPS Python asyncio ne justifie pas l'effort HFT-grade ; voir §4.1).
4. **Approche logs hygiene : filtrage middleware (c) + exclusion lecteur (d)
   combinés**, via `structlog.DropEvent` en amont des processors coûteux.
5. **Gates durs avant scoring** (anti-zombie, anti-Sybil, PnL net > 0,
   trade count minimal — voir §1.3).
6. **Versioning strict du scoring** via `SCORING_VERSION` pour permettre
   coexistence v1/v2 pendant shadow period, puis deprecation propre.

---

## 1. Scoring v2 — formule finale et rationale

### 1.1 Contradictions Gemini vs Perplexity — comment on tranche

**Gemini** propose un modèle "SEARS" agressif (Score d'Expertise Ajusté au
Risque et à la Spécialisation) :

```
Score_v2 = 0.45·(Sortino / MaxDD)_norm
         + 0.35·(TimingAlpha / BrierScore)
         + 0.20·(C_spec × (1 - P_zombie))
```

Reposant sur deux divisions qui **amplifient le bruit quand le dénominateur
tend vers zéro** (ex : un wallet jeune avec 0 drawdown a un score Sortino/DD
infini). Privilégie les traders sophistiqués, mais fragile
mathématiquement.

**Perplexity** propose une combinaison additive plus classique :

```
Score_v2 = 0.30·Sharpe/Sortino + 0.20·ROI + 0.20·consistency
         + 0.15·specialization + 0.15·informed
```

Additif, robuste, lisible — mais trop prudent : le terme `0.20·ROI` est
exactement ce qu'on critique dans v1 (rewarde les one-shots).

**Décision retenue** : formule additive (robustesse mathématique) avec des
facteurs issus de la littérature Mitts-Ofir + Sortino/Calmar académiques, mais
**sans ROI nominal** (remplacé par des métriques risk-adjusted). Pondération
à valider empiriquement par backtest au cours de la shadow period.

### 1.2 Formule retenue

**Préambule obligatoire** : tous les sous-scores sont **normalisés via
winsorisation aux percentiles 5-95 du pool actif** puis rescalés 0-1. Cette
normalisation évite qu'un outlier single-market (ex : Fredi9999 +$48M sur
l'élection 2024) écrase la distribution des autres wallets.

```
Score_v2 = 0.25·risk_adjusted    # Sortino 0.6× + Calmar 0.4×
         + 0.20·calibration      # 1 - brier / brier_baseline_pool
         + 0.20·timing_alpha     # Mitts-Ofir pre-event signal
         + 0.15·specialization   # HHI sur tags Gamma
         + 0.10·consistency      # fraction mois PnL>0 sur 90j
         + 0.10·discipline       # (1 - zombie_ratio) × sizing_stability
```

**Rationale par bloc** :

- **risk_adjusted (0.25)** : Sortino prioritaire (2/3 du sous-score, Calmar
  complémentaire 1/3). Gemini et Perplexity convergent sur
  "Sortino > Sharpe pour distributions asymétriques binaires". Calmar apporte
  la résilience aux drawdowns extrêmes.
- **calibration (0.20)** : Brier-skill score = `1 - brier_wallet /
  brier_baseline_pool`. Baseline = Brier d'un wallet qui rentre toujours au
  midpoint du pool. Un wallet avec score > 0 est mieux calibré que la
  moyenne. Seuil académique *skill* = Brier < 0.22, *expert* = < 0.15.
- **timing_alpha (0.20)** : fraction du PnL généré par trades entrés dans
  une fenêtre courte **avant** un mouvement de prix significatif ou une
  annonce publique. Signal Mitts-Ofir le plus directement actionnable.
- **specialization (0.15)** : `1 - HHI(volume par catégorie Gamma)`. Récompense
  les wallets concentrés ≥70 % sur 1-2 catégories — corrélat empirique de
  l'avantage informationnel (arxiv 2603.03136).
- **consistency (0.10)** : `fraction de mois avec PnL > 0 sur 90j`. Filtre
  les one-shots. Poids faible car partiellement corrélé à risk_adjusted.
- **discipline (0.10)** : `(1 - zombie_ratio) × sizing_stability`. Le
  `zombie_ratio` (Gemini, §1.1 source) = capital immobilisé dans des
  positions < 2¢ jamais liquidées. `sizing_stability` = inverse du
  coefficient de variation des tailles de trade.

### 1.3 Gates durs (pré-scoring, tous requis)

Un wallet qui échoue n'importe quel gate est **exclu** du pool avant même le
calcul du score. Ces gates tuent les plus gros faux positifs observés sur
Polymarket.

| Gate | Seuil | Rationale |
|---|---|---|
| `cash_pnl_90d > 0` | net profit positif sur 90j | Reichenbach-Walther : 70 % des traders perdent. Pré-filtrage trivial très efficace |
| `trade_count_90d ≥ 50` | au moins 50 trades résolus ou actifs sur 90j | Évite les wallets insiders one-shot (Fredi9999-like) |
| `days_active ≥ 30` | wallet actif depuis au moins 30 jours | Anti-sybil basique (Polymarket Sybil criteria) |
| `zombie_ratio < 0.40` | < 40 % du capital dans positions < 2¢ non liquidées | Gemini : manipulation win rate via positions non closes |
| `not blacklisted` | absent de `BLACKLISTED_WALLETS` env | Règle M5 existante, conservée |
| `not in_wash_cluster` | absent des clusters wash identifiés | Sirolly et al. 2025, 25 % du volume Polymarket est wash. Optionnel v1, required v2 |

### 1.4 Versioning et coexistence

- Colonne `TraderScore.scoring_version: str` déjà prévue en M5 — étendre de
  `"1"` à `"2"`.
- Config `SCORING_VERSION=1|2` (default : `1` jusqu'au cutover).
- Shadow period : les deux formules calculent en parallèle, la v2 n'affecte
  pas `status` (shadow/active) pendant `SCORING_V2_SHADOW_DAYS` (défaut 14).
- Dashboard `/traders` affiche les deux scores côte-à-côte pendant la
  shadow period (colonne `score_v1 | score_v2 | delta_rank`).
- Après shadow period : decision manuelle (A/B backtest doit montrer gain
  sur Brier aggregate du pool promu) avant flip.

### 1.5 Implémentation Discovery M5 — impact

- **Module à étendre** : `src/polycopy/discovery/trader_scoring.py`
- **Nouvelles dépendances data** : `timing_alpha` nécessite timestamps
  précis sur `/activity` + cache des moves de midpoint (WebSocket
  `wss://ws-subscriptions-clob.polymarket.com` channel `market`).
  **Prérequis : M14 mergé pour que le calcul soit tractable**.
- **Brier / Sortino** : nécessitent reconstruction d'une equity curve
  par wallet. Impose une nouvelle table `TraderDailyPnl` (snapshot quotidien
  de l'equity) — migration Alembic 0005.
- **Nouveau facteur `zombie_ratio`** : scan des positions via Data API
  `/positions`, sum des lignes avec `currentValue < 0.02 × initialValue`,
  rapport au capital total.

### 1.6 Sources bootstrap data

- **Apify Polymarket Leaderboard Scraper** (Gemini §1.1) : API payante mais
  dump Polymarket leaderboard propre (proxyWallet, pnl, vol, filtre temporel).
  Accélère le bootstrap pool M5 sans requêter `/holders` + `/trades` en mode
  reverse-engineering — à évaluer (coût vs gain).
- **Kreo / KreoPoly** : ranking metrics convergent avec v1, pas d'API
  publique. **Ne pas essayer de scraper** — fragilité.
- **PolyVision** : API nécessite registration ; score composite closed.
  Utilisable comme "oracle externe" pour cross-check de notre v2, pas pour
  en dépendre.

---

## 2. Log hygiene — plan d'implémentation M15

### 2.1 Approche retenue : (c) + (d) combinées, via `structlog.DropEvent`

Gemini et Perplexity convergent vers la même conclusion : filtrer au
middleware par path + status, ET exclure côté lecteur par défaut.

**Stratégie en 3 étages** :

#### Étage 1 — Middleware (`src/polycopy/dashboard/middleware.py`)

Processor structlog custom `filter_noisy_endpoints`, inséré **avant**
`TimeStamper` et `JSONRenderer` (économise le coût CPU du formatage) :

```python
# Pseudocode (pas d'implémentation ici, voir spec M15)
def filter_noisy_endpoints(logger, method_name, event_dict):
    if event_dict.get("event") != "dashboard_request":
        return event_dict
    path = event_dict.get("path", "")
    status = event_dict.get("status", 200)
    # Toujours logger les erreurs, peu importe le path
    if status >= 400:
        return event_dict
    # Blacklist : paths de polling haute fréquence
    noisy_patterns = [
        r"^/api/health-external$",
        r"^/partials/.*$",
        r"^/api/version$",
    ]
    if any(re.match(p, path) for p in noisy_patterns):
        raise structlog.DropEvent
    return event_dict
```

L'exception `structlog.DropEvent` annule l'émission atomiquement, sans
coût de formatage. **Les erreurs 4xx/5xx passent toujours** → observabilité
préservée sur les crashes middleware.

#### Étage 2 — Lecteur `/logs` (`src/polycopy/dashboard/log_reader.py`)

Constante `_DEFAULT_EXCLUDED_EVENTS = ["dashboard_request"]` appliquée
automatiquement à la route `/logs` et `/partials/logs-tail`. Opt-in explicite
via input UI `events=dashboard_request` si l'utilisateur veut tout voir.

#### Étage 3 — Template `/logs` (`templates/logs.html`)

Bouton preset "Business events only" (par défaut actif) vs "Include HTTP
access" (opt-in). Choix persisté en `localStorage` (cohérent M6 theme).

### 2.2 Tests à protéger et à ajouter

**À protéger** (ne doivent pas régresser) :
- `test_logs_filter_by_level` (`tests/unit/test_dashboard_logs_route.py:104`)
- `test_logs_filter_by_q_substring` (ibid:118)
- `test_partials_logs_tail_returns_fragment` (ibid:168)
- `test_download_serves_file_contents` (`test_dashboard_logs_download.py:44`)

**À ajouter** :
- `test_logs_default_hides_dashboard_request` — page `/logs` sans filtre ne
  montre aucun `dashboard_request`.
- `test_middleware_drops_noisy_endpoint_success` — une GET 200 sur
  `/partials/kpis` ne produit aucune ligne dans le fichier log.
- `test_middleware_keeps_noisy_endpoint_error` — une GET 500 sur
  `/partials/kpis` produit bien une ligne (erreur préservée).
- `test_logs_opt_in_shows_dashboard_request` — filtre UI
  `events=dashboard_request` restore l'affichage.

### 2.3 Impact quantitatif attendu

- Fichier `~/.polycopy/logs/polycopy.log` : **ratio bruit/signal actuel
  28:1** sur Home active. Post-M15 : ~0.5:1 (seuls errors 4xx/5xx).
- Rotation du fichier `RotatingFileHandler(10 MB × 10)` — actuellement
  rotation ~1×/jour sur usage dashboard actif. Post-M15 : ~1×/semaine.

---

## 3. Dry-run / live parity M15 — 3 modes

### 3.1 Taxonomie retenue (inspirée Perplexity)

Passage de binaire (`dry_run: bool`) à 3 modes explicites :

| Mode | Description | Exécution ordres | Kill switch | Alertes Telegram | Canal |
|---|---|---|---|---|---|
| `SIMULATION` | Backtest offline, replay historique | Stub (pas de réseau) | Noop (mode offline) | Aucune | N/A |
| `DRY_RUN` | Online, pipeline complet, simulation FOK réaliste via `/book` | Simulée (M8 realistic fill) | **Actif, identique live** | **CRITICAL identique live**, tag `mode=dry-run` | Canal prod, badge 🟢 DRY-RUN |
| `LIVE` | Exécution réelle on-chain | `py-clob-client` real POST | Actif | CRITICAL | Canal prod, badge 🔴 LIVE |

**Le changement majeur vs actuel** : `DRY_RUN` devient un miroir fidèle
de `LIVE`, pas un bac à sable silencieux.

### 3.2 Migration config

- Remplacer `DRY_RUN: bool` par `EXECUTION_MODE: Literal["simulation",
  "dry_run", "live"]`. Default : `"dry_run"`.
- Deprecation `DRY_RUN=true` supported avec warning 1 version (lecture →
  `EXECUTION_MODE=dry_run`).
- Deprecation `DRY_RUN_REALISTIC_FILL=true` : ça devient le seul mode de
  dry-run existant (plus de choix entre stub M3 et realistic M8 — realistic
  gagne par défaut, M3 stub passe en `SIMULATION`).

### 3.3 Code à modifier (5 fichiers)

| Fichier | Changement |
|---|---|
| `src/polycopy/config.py` | Remplace `dry_run: bool` par `execution_mode: Literal[...]` + validator backward-compat |
| `src/polycopy/monitoring/pnl_writer.py:108-110` | Supprime `_maybe_push_dry_run_drawdown()` ; kill switch fire identiquement en SIMULATION/DRY_RUN/LIVE |
| `src/polycopy/monitoring/telegram_notifier.py` | Injecte `mode` dans contexte template systématiquement |
| `src/polycopy/monitoring/templates/*.md.j2` | Ajout badge `{{ mode | upper }}` header chaque alerte |
| `src/polycopy/executor/pipeline.py` | Branches `if settings.execution_mode == "live"` remplacent `if not dry_run` |

**Migrations DB** : zéro. Colonnes `MyOrder.is_dry_run`, `MyPosition.simulated`
restent telles quelles pour la ségrégation data (audit trail, affichage
dashboard). C'est correct : la parité concerne le *comportement runtime*, pas
le *stockage* (Gemini §3.1 confirme cette séparation).

### 3.4 Tests à réécrire (~8-12)

**À inverser** (assument dry-run silencieux, deviennent faux) :
- `tests/unit/test_pnl_writer.py::test_dry_run_does_not_trigger_kill_switch`
  → `test_dry_run_triggers_kill_switch_like_live`.
- `tests/unit/test_telegram_notifier.py::test_dry_run_downgrades_alert_level`
  → `test_dry_run_alert_level_matches_live`.

**À ajouter** :
- `test_telegram_alert_shows_mode_badge_dry_run` — le message contient `🟢 DRY-RUN`.
- `test_telegram_alert_shows_mode_badge_live` — le message contient `🔴 LIVE`.
- `test_kill_switch_stops_executor_orchestrator_in_dry_run` — `stop_event.set()`
  effectivement déclenché.
- `test_execution_mode_live_requires_keys_like_before` — garde-fou 2 M3
  préservé (RuntimeError si LIVE sans `POLYMARKET_PRIVATE_KEY`).

### 3.5 Impact utilisateur (UX)

**Attention** : si un utilisateur a un backtest dry-run qui tourne sur
`KILL_SWITCH_DRAWDOWN_PCT=20` et que le backtest atteint 20 % de drawdown
virtuel, le bot va **se couper**. C'est le comportement voulu (parité) mais
doit être documenté clairement dans `README.md` + alerte CLI au premier boot
post-migration :

```
⚠️  execution_mode=dry_run now mirrors live kill switch. Set
   KILL_SWITCH_DRAWDOWN_PCT=100 if you want unlimited simulation.
```

---

## 4. Latence M14 — cibles réalistes par phase

### 4.1 Contradiction Gemini (347 ms) vs Perplexity (1-3 s) — décision

**Gemini** cible 347 ms end-to-end via :
- WebSocket CLOB (~15 ms)
- Goldsky Turbo Pipelines webhook (~45 ms)
- Multi-process workers (<10 ms scoring)
- Pré-signature ordres (~2 ms)
- Colocalisation AWS (~25 ms)
- Taker speed bump natif (~250 ms incompressible)

**Perplexity** cible 1-3 s via :
- WebSocket CLOB (0.1-0.5 s)
- Cache Gamma agressif (0.2-0.5 s)
- Parallélisation asyncio (0.2-0.5 s)
- Optimisation HTTP pool (0.2-0.5 s)

**Décision retenue** : cible Perplexity **phase 1** (2-3 s), cible Gemini
**phase 3** (si jamais on y va — reste à justifier par un ROI clair).

**Rationale** :
- Polycopy est conçu pour **smart money following** (edge durée = heures),
  pas pour HFT/arbitrage (edge durée = secondes). À ~2-3 s on capture déjà
  l'essentiel de l'edge.
- Passer de 2-3 s à 347 ms coûte : Goldsky Turbo ($$$), multi-process
  (complexité asyncio → multiprocessing Python = piège), colocalisation
  (VPS premium $$$), pré-signature (refactor executor).
- Gain marginal non démontré empiriquement. À mesurer avant d'engager.

### 4.2 Phase 1 (spec M14, ~2 semaines)

**Objectif : passer de 10-15 s à 2-3 s**.

#### 4.2.1 WebSocket CLOB pour SlippageChecker

- Nouveau module `src/polycopy/strategy/clob_ws_client.py`.
- Connection `wss://ws-subscriptions-clob.polymarket.com/ws/market`.
- Souscription channel `market` sur les `token_id` candidats (lazy sub
  quand un trade est détecté, unsub après 5 min d'inactivité).
- `SlippageChecker` consulte un cache in-memory mis à jour par le WS
  (latence lookup : ~0 ms) au lieu d'un HTTP GET `/midpoint` (~500-1500 ms).
- Fallback HTTP `/midpoint` si le WS est déconnecté (tenacity retry).
- Tests : mock WS via `pytest-asyncio` + `websockets.serve` sur port local.

#### 4.2.2 Cache Gamma adaptatif

Actuellement TTL 60 s uniforme (`GammaApiClient._cache`,
`src/polycopy/strategy/gamma_client.py:28`).

Remplacer par TTL adaptatif :
- Marchés résolus (`closed=true`) : TTL infini (immuable).
- Marchés proches résolution (`endDate - now < 1h`) : TTL 10 s.
- Marchés actifs : TTL 300 s (au lieu de 60).
- Marchés inactifs > 7j (`volume < $100/day`) : TTL 3600 s.

Gain estimé : hit rate actuel ~50 %, cible ~85 %, économie ~400 ms par trade.

#### 4.2.3 Instrumentation latence par étape

Ajouter `structlog.bind_contextvars(trade_id=...)` au début du pipeline et
logger `stage_duration_ms` à chaque frontière :
- `watcher_detected_ms` (temps entre `trade.timestamp` Polymarket et
  ingestion locale)
- `strategy_enriched_ms`
- `strategy_filtered_ms`
- `strategy_sized_ms`
- `strategy_risk_checked_ms`
- `executor_submitted_ms`

Dashboard `/latency` nouveau onglet : histogramme p50/p95/p99 par étape.

**Prérequis validation scoring v2 §1** : on ne peut pas mesurer la qualité
d'un wallet scoré si le bot rate les trades à cause de la latence. M14 est
donc un prérequis de M13.

### 4.3 Phase 2 (spec M14-bis, optionnelle, ~1 mois)

Si phase 1 a dégagé les gains évidents mais qu'on veut aller sous 1 s :

#### 4.3.1 Parallélisation strategy pipeline

`src/polycopy/strategy/pipeline.py:163-173` actuellement séquentiel. Refactor
vers `asyncio.gather()` pour les 3 filtres indépendants (MarketFilter,
SlippageChecker cache-lookup, PositionSizer) — RiskManager reste séquentiel
en aval (dépend de la taille calculée).

Gain : 0.5-1 s.

#### 4.3.2 Watcher WebSocket user channel

Remplacer le polling `/activity` (5 s intervalle, +1-2 s fetch) par WS
channel `user` sur les wallets cibles. Détection quasi-instantanée des
trades on-chain.

Gain : ~5-6 s (énorme — la piste dominante en valeur).

**Complexité** : Le channel `user` de Polymarket WS nécessite souscription
par wallet avec reconnection logic robuste (>20 wallets = pool de
connexions, backoff exponentiel, health checks). Non-trivial.

### 4.4 Phase 3 (hors scope initial)

- Goldsky Turbo Pipelines (webhook direct <50 ms depuis Polygon RPC).
- Bitquery Kafka streaming (alternative).
- Multi-process (à évaluer seulement si asyncio devient CPU-bound — peu
  probable sur 50 wallets).
- Colocalisation VPS.
- Pré-signature ordres en batch.

**Déclencheurs** pour passer en phase 3 : si l'analyse latence post-M14
montre qu'on rate >10 % des opportunités à cause de latence résiduelle, ET
que le backtest v2 montre un edge significatif sur les trades <1 s
d'exécution.

---

## 5. Smart money patterns — set retenu pour M13

Parmi les 16 patterns catalogués en brainstorming + ceux ajoutés par les
deep searches, **6 retenus pour v2**, 3 en observation (features futures) :

### 5.1 Retenus (intégrés dans la formule v2)

| Pattern | Où dans la formule | Source principale |
|---|---|---|
| Timing alpha pre-event | `timing_alpha` (0.20) | Mitts-Ofir 2026 |
| Category specialization ≥70 % | `specialization` (0.15) | arxiv 2603.03136 |
| Multi-month rolling consistency | `consistency` (0.10) | Polytreasury pattern, laikalabs |
| Sortino + Calmar risk-adjusted | `risk_adjusted` (0.25) | XBTO, FXCM crypto guides |
| Brier calibration | `calibration` (0.20) | Gneiting-Raftery, Metaculus |
| Zombie ratio (anti-manipulation) | `discipline` via `(1 - zombie_ratio)` | Gemini §1.1, Reddit 1297 traders analysis |

### 5.2 Retenus en gate dur (§1.3)

- Wallet age ≥ 30 j
- Cash PnL 90j > 0
- Trade count 90j ≥ 50
- Not in wash cluster (optionnel v1, required v2)

### 5.3 En observation (features futures, M17+)

| Pattern | Pourquoi différé |
|---|---|
| Closing Line Value (CLV) | Nécessite orderbook snapshots time-series. Storage cost élevé, gain incertain |
| Maker/taker ratio (liquidity provider) | Signal de sophistication, pas d'edge directionnel — à exclure du copy plutôt qu'à copier |
| Fractional Kelly sizing | Nécessite proxy d'edge bruité. Feature engineering lourde |
| Iceberg slicing detection | Algo complexe (pattern matching sur séquences d'ordres), gain non démontré |

### 5.4 Patterns d'exclusion (ne pas copier)

Flags ajoutés à `Trader.flags: list[str]` :
- `insider_suspect` — trade isolé massif pré-event sans historique
  (Mitts-Ofir composite ≥ p99)
- `arbitrageur` — positions simultanées Yes+No sommant >1.00 sur neg-risk
- `market_maker_pure` — ratio maker/(maker+taker) > 0.7, PnL directionnel
  quasi-nul
- `wash_cluster_member` — inclus dans un cluster détecté par graphe
  on-chain

Les wallets flaggés **ne sont pas copiés** (exclusion du `status=active`)
mais restent trackés pour analyse/audit.

---

## 6. Découvertes importantes à ne pas oublier

### 6.1 Taker fees dynamiques (Gemini §6.2) — **critique court terme**

Polymarket a récemment introduit des frais dynamiques sur les ordres Taker,
notamment sur les contrats à rotation rapide (crypto, sports 5-15 min).
Endpoint `GET /fee-rate?tokenID=<id>` à intégrer dans le moteur d'EV.

**Risque** : si notre Sizer calcule un EV sans soustraire les fees actuels,
on envoie des ordres structurellement perdants dès l'activation.

**Action** : ajouter à spec M16 (ou inclure dans M14 phase 1 si rapide) :
- Nouveau client `FeeRateClient` dans `executor/` avec cache TTL 60 s.
- `Sizer.calculate()` soustrait `fee_rate × notional` de l'EV avant de
  valider une position.
- Test : simuler un fee rate à 2 %, vérifier que les ordres EV < 2 % sont
  rejetés.

### 6.2 Zombie positions (Gemini §1.1) — intégré scoring v2

Pattern de manipulation : un trader gonfle son win rate apparent en gardant
des positions perdantes ouvertes indéfiniment (valeur ~0.01-0.02 USDC
jamais liquidées). Win rate `wins / (wins + closed_losses)` reste élevé.

**Définition opérationnelle retenue** :
```
zombie_ratio = sum(position.current_value
                   for p in positions
                   if p.current_value < 0.02 * p.initial_value)
             / total_capital
```

Gate dur : `zombie_ratio < 0.40`. Inclus dans `discipline` score.

### 6.3 Apify Leaderboard Scraper (Gemini §1.1)

Source `apify.com/saswave/polymarket-leaderboard-scraper` : API payante qui
dump le leaderboard Polymarket officiel (proxyWallet, pnl, vol, filtre
catégorie + période). **Évaluer** pour accélérer le bootstrap pool M5 :
- Coût : ~$5-20/mois (à vérifier pricing Apify).
- Alternative : continuer avec le path `/holders` + `/trades`
  reverse-engineering (gratuit, mais fragile).
- Décision : attendre M13 en cours pour voir si le bottleneck M5 bootstrap
  justifie le coût.

### 6.4 MEV risk Polygon (Gemini §6.1) — **long terme uniquement**

Quand polycopy envoie un ordre rentable sur la mempool Polygon publique,
des searchers MEV peuvent front-run avec un gas prix bumped. Mitigation :
relais Flashbots équivalent Polygon (private transaction). **Non urgent** —
la taille des trades polycopy (`MAX_POSITION_USD` par défaut ~$10-100) est
probablement sous le radar économique des searchers. Réévaluer si position
size moyenne dépasse $500.

### 6.5 Avellaneda-Stoikov market making (Gemini §6.3) — M18+

Le prix qu'on observe au CLOB n'est pas nécessairement la "vraie"
probabilité consensuelle, mais le reservation price d'un market maker
algorithmique. Pour passer de price-taker à price-maker avec un modèle
Avellaneda-Stoikov, il faudrait :
- Modéliser l'inventaire.
- Estimer la volatilité du marché.
- Optimiser quotes bid/ask asymmetrical.

Grosse refonte. **Pas avant M18**. Mais note : si M13-M15 valident que nos
wallets scorés sont vraiment bons, la question "doit-on coter nous-mêmes"
devient pertinente.

### 6.6 RTDS (Real-Time Data Socket) Polymarket (Perplexity §4.1)

Alternative au WebSocket CLOB classique. Canaux : `prices`, `activity`,
`clob_market`. Documenté comme recommandé pour
dashboards/analytics. À évaluer vs `wss://ws-subscriptions-clob.polymarket.com`
au moment de M14 — probablement RTDS gagne sur breadth, CLOB WS gagne sur
latence orderbook pure.

### 6.7 Mitts-Ofir 5 signaux (pair-level vs wallet-level)

**Nuance importante** (Perplexity §1.2) : le score composite Mitts-Ofir
s'applique à des paires (wallet, market), pas à un wallet global. Notre
score v2 `timing_alpha` doit donc **agréger** les scores pair-level :
- `timing_alpha_wallet = mean(pair_scores weighted by position size)` sur
  les N dernières positions résolues.
- Pondération par taille (gros trades pèsent plus).
- Fenêtre : 90 jours.

---

## 7. Mapping vers les specs à produire

### 7.1 Renumérotation proposée

Les prompts M10/M11/M12 existants dans `specs/` (watcher live-reload,
backtest scheduler, discovery auto-lockout) restent à leur place. Les
nouvelles specs issues du brainstorming + deep search commencent à M13.

| Spec | Titre | Priorité | Dépend de |
|---|---|---|---|
| M13 | Scoring v2 (SEARS-hybrid formula) | 3 | M14 |
| M14 | Real-time pipeline phase 1 (WebSocket CLOB + cache adaptatif + instrumentation latence) | 2 | M15 |
| M14-bis | Real-time pipeline phase 2 (parallélisation + WS user channel) | 5 | M14 |
| M15 | Log hygiene + dry-run/live parity + 3 modes | 1 | Aucun |
| M16 | Taker fees dynamiques intégrés au Sizer EV | 6 | M14 |
| M17 | Goldsky Turbo / Bitquery streaming (optionnel) | 7 | M14 |
| M18+ | MEV defense + market making Avellaneda-Stoikov | Futur | Tout |

### 7.2 Ordre d'exécution suggéré

```
M15 (parité + logs) ─┐
                     ├──> M14 phase 1 (WebSocket + instrumentation) ─┬──> M13 (scoring v2)
                     │                                                │
                     │                                                └──> M16 (fees)
                     │
                     └──> [décision business : continuer avec M14-bis ou consolider ?]
```

**Rationale de l'ordre** :
1. M15 d'abord : débruite les logs et aligne dry-run/live, ce qui permet de
   tester M14 et M13 sur un pipeline observable et prévisible.
2. M14 phase 1 ensuite : le pipeline doit être rapide avant de valider un
   scoring v2 (sinon on ne sait pas si un wallet n'est pas copié parce que
   sa qualité est douteuse, ou parce que le bot était trop lent).
3. M13 enfin : c'est le cœur produit, mais il profite des deux précédents.
4. M16 en parallèle avec M13 : indépendant, court, "defensive" (protège l'EV
   actuel face à une évolution de Polymarket).

### 7.3 Estimation totale

- M15 : ~1 semaine (1 dev)
- M14 phase 1 : ~2 semaines
- M13 : ~2-3 semaines (backtest + shadow period = calendrier étalé sur
  6 semaines avant cutover)
- M16 : ~2-3 jours
- M14-bis : ~2-4 semaines (optionnel)

**Calendrier réaliste** : ~8-10 semaines pour M15+M14+M13+M16. Puis
période de validation backtest + shadow avant cutover scoring v2.

---

## 8. Deltas CLAUDE.md à propager

À faire en même temps que les specs respectives, **pas avant** (sinon
l'invariant documenté ne correspond pas au code).

### 8.1 Section "Monitoring M4"

**Actuel (ligne ~97)** :
> kill switch déclenché EXCLUSIVEMENT par PnlSnapshotWriter, **jamais en
> dry-run** (sécurité critique)

**Nouveau (post-M15)** :
> kill switch déclenché EXCLUSIVEMENT par PnlSnapshotWriter sur
> `KILL_SWITCH_DRAWDOWN_PCT`, **identique dans les 3 modes**
> (SIMULATION/DRY_RUN/LIVE). Dry-run utilise capital virtuel
> `dry_run_virtual_capital_usd` et positions simulées pour le calcul du
> drawdown. Les alertes Telegram en dry-run portent un badge visuel
> `🟢 DRY-RUN` pour différencier de `🔴 LIVE`, mais la sévérité (CRITICAL)
> est identique.

### 8.2 Section "Dry-run M8"

**Actuel (ligne ~103)** :
> Kill switch JAMAIS en dry-run (invariant M4 préservé). Alerte
> `dry_run_virtual_drawdown` INFO only à 50 % du seuil

**Nouveau (post-M15)** :
> Kill switch déclenché à `KILL_SWITCH_DRAWDOWN_PCT` identique à live.
> Alerte `kill_switch_triggered` CRITICAL identique live, avec badge
> `🟢 DRY-RUN` dans le header. Ségrégation data préservée
> (MyOrder.realistic_fill, MyPosition.simulated, contrainte unique triple).

### 8.3 Section "Conventions de code"

Ajouter :
> **Modes d'exécution (M15+)** : `EXECUTION_MODE: "simulation" | "dry_run"
> | "live"` remplace `DRY_RUN: bool`. Ancien flag lu en fallback avec
> warning de deprecation.

### 8.4 Section "APIs Polymarket utilisées"

Ajouter sous-section :
> **CLOB WebSocket (M14+)** : `wss://ws-subscriptions-clob.polymarket.com`
> est désormais utilisé en production pour SlippageChecker (channel
> `market`) en remplacement du polling `/midpoint`. Fallback HTTP en cas
> de déconnexion WS.

Et une autre :
> **Fee rate (M16+)** : `GET /fee-rate?tokenID=<id>` consommé par
> `FeeRateClient` avec cache TTL 60 s. Sizer soustrait les fees de l'EV
> avant validation.

---

## 9. Open questions — à trancher avant spec M13

Questions dont la réponse influence le design final du scoring v2. À
résoudre en session dédiée (ou explicitement marqués TODO dans la spec).

1. **Pondération finale de la formule v2** : 0.25/0.20/0.20/0.15/0.10/0.10
   proposée en §1.2 est une intuition. Doit être validée par un backtest
   sur les 90 derniers jours de données Polymarket (wallets labelés
   manuellement comme "smart money" vs "random" dans un set de validation).
   Décision : lancer le backtest dès le début de M13 avec plusieurs
   pondérations candidates et retenir celle qui maximise un proxy de
   qualité (précision sur le top-20 du pool).

2. **Horizon temporel** : fenêtre 90 jours glissants par défaut. Certains
   papers (Reichenbach-Walther) suggèrent 6-12 mois pour stabilité
   statistique. Trade-off : court horizon = plus réactif aux wallets
   récemment devenus skilled, long horizon = plus robuste. **Décision
   attendue** : 90 j par défaut, `SCORING_WINDOW_DAYS` configurable.

3. **Weighting temporel** : pondération exponentielle (plus de poids au
   récent, half-life 30 j) vs uniforme sur la fenêtre. Gemini et KreoPoly
   suggèrent exponentiel. **Décision attendue** : exponentiel, half-life
   configurable `SCORING_HALFLIFE_DAYS=30`.

4. **Paired vs solo signals** : le `timing_alpha` pair-level de
   Mitts-Ofir → wallet-level via mean weighted. Mais 2 wallets avec 1 seul
   trade timing_alpha=1.0 chacun ont le même score qu'un wallet avec 50
   trades timing_alpha=1.0. Décision : weighting par `sqrt(n_trades)` pour
   balancer entre quantity et quality.

5. **Cold start** : un wallet nouveau (30j < age < 90j) n'a pas
   suffisamment de data pour tous les facteurs. Strategy : gate dur
   `trade_count_90d >= 50` résout ça par exclusion. Mais les wallets
   "jeunes prometteurs" sont exclus. Alternative : catégorie `shadow_new`
   observée pendant 30j supplémentaires avec trade_count assoupli.
   **Décision attendue** : keep gate dur strict v2.0, assouplir en v2.1 si
   on rate trop de candidats.

6. **Backtest budget** : backtest honnête requiert ~50-100 wallets labelés
   manuellement + 6 mois historique + reconstruction equity curves.
   Temps estimé : 2-3 semaines en parallèle de M14. **Décision attendue** :
   budget approuvé.

7. **Apify Leaderboard Scraper** : l'acheter ou rester en
   `/holders` + `/trades` reverse-engineering ? Décision après revue
   coût/complexité en début de M13.

---

## 10. Journal des sources consultées

- `docs/development/gemini_deep_search_v2_and_more.md` — 59 sources citées,
  focus architecture institutionnelle HFT-grade
- `docs/development/perplexity_deep_search_v2_and_more.md` — 37 sources
  citées, focus patterns smart money + approche pragmatique
- Session brainstorming 2026-04-18 — audit code interne (middleware,
  pnl_writer, pipeline latency), 6 agents Explore/general-purpose

Principales divergences arbitrées :
- Formule scoring (§1.2) : ni l'une ni l'autre, hybride documenté
- Cible latence (§4.1) : Perplexity (2-3 s), pas Gemini (347 ms)
- Mode taxonomy (§3.1) : Perplexity (3 modes), pas Gemini (binaire)

Principales convergences :
- WebSocket > polling (consensus)
- Kill switch parité dry/live (consensus)
- Filtrage logs middleware + lecteur (consensus)
- Sortino > Sharpe pour binaire (consensus)
- Brier score indispensable (consensus)
- Specialization + timing alpha dominants (consensus)
