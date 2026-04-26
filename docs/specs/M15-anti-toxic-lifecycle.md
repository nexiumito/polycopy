# M15 — Anti-toxic lifecycle + internal PnL feedback

**Status** : Draft — 2026-04-25
**Depends on** : M5 (discovery lifecycle), M5_bis (eviction opt-in + audit H-007), M5_ter (watcher live-reload), M8 (dry-run realistic fill — produit `realized_pnl` source), M13 (dry-run observability — `PositionSizer` side-aware débloque cristallisation copy PnL), M14 (scoring v2.1-ROBUST shippé en MA — formule pilote stable post-shadow)
**Bloque** : MF (v2.2-DISCRIMINATING capstone — exige 30j d'`internal_pnl_score` collectée), MH (UX dashboard sur les nouveaux facteurs)
**Workflow git** : commits directement sur `main` (pas de branche, pas de PR — règle projet)
**Charge estimée** : M (4-5 jours dev + 30j calendaire pour collecte cold-start `internal_pnl_score` post-merge)

---

## 0. TL;DR

M15 ferme la **boucle anti-toxic** ouverte par l'observation runtime 2026-04-24
(wallet `0x21ffd2b7…0d71` ACTIVE depuis 5 jours, 19 % WR, −$0.55 PnL, score
v2.1 = 0.66 sans aucun mécanisme automatique pour le démote — l'utilisateur a
dû le blacklister manuellement via `.env` + restart). Le module ajoute simul-
tanément (1) un **signal de performance interne** (`internal_pnl_score`) fitté
sur la PnL réalisée par polycopy depuis qu'on copie le wallet, (2) un
**lifecycle ranking-based** qui remplace le threshold absolu cassé, (3) trois
**gates anti-arbitrage-bot + probation + auto-blacklist** qui ferment les gros
trous structurels.

**8 items couplés** mappés MB.1 → MB.8 du brief
[docs/next/MB.md](../next/MB.md) :

- **MB.1** — Collecteur `_compute_internal_pnl_score(wallet)` dans
  `MetricsCollectorV2` : `sigmoid(signed_pnl_30d / 10)` avec cold-start
  `None` si `< 10` positions copiées closed.
- **MB.2** — Nouveau facteur `internal_pnl` dans l'aggregator scoring v2.1.1
  (poids `0.25`, redistribution proportionnelle des 5 autres). Cold-start
  renormalize localement à 5 facteurs si `internal_pnl_score is None`.
- **MB.3** — `_decide_active` ranking-based (top-N par score v2.1) +
  garde-fou threshold absolu `< 0.30` préservé. Élimine la pathologie
  "wallet 0.66 ACTIVE indéfiniment" observée 2026-04-24.
- **MB.4** — Fix audit **[H-007]** : `classify_sell_only_transitions`
  consomme maintenant les **fresh scores** pour le calcul `_delta_vs_worst`
  (active_non_pinned snapshots refreshed avant l'évaluation T6/T8).
- **MB.5** — Helper `_log_empirical_margin_recommendation` au boot :
  observe la `std(active_scores)` sur 7j post-v2.1, log la
  recommandation 1σ. Pas d'auto-ajustement (décision humaine).
- **MB.6** — **Probation fractional-Kelly 0.25×** pour wallets `10 ≤
  trade_count_90d < 50` qui passent les autres gates. `is_probation` flag DB
  + `PositionSizer` multiplie `my_size` par `0.25`. Auto-release au seuil
  `≥ 50`.
- **MB.7** — Nouveau **gate dur arbitrage_bot_pattern** : rejette les
  wallets dont `|YES_net − NO_net| / gross < 0.10` sur fenêtre 90j (capture
  les $40M/an d'extracteurs Bregman documentés Dev Genius / Claude §9 item
  5).
- **MB.8** — **Auto-blacklist** sur seuils `cumulative_observed_pnl <
  -$5 OR (observed_win_rate < 0.25 AND observed_position_count ≥ 30)`.
  Nouveau template Telegram `trader_auto_blacklisted.md.j2`. Discipline
  réversible identique M5_bis (retrait via `.env` → `reconcile_blacklist`).

Résultat attendu post-merge :

- **Aucun wallet ne reste ACTIVE indéfiniment avec PnL négatif** : auto-
  blacklist (MB.8) ou demote ranking (MB.3) tirent en moins de 24h.
- **Rotation pool effective** : ≥ 5 promotions et ≥ 5 demotions sur 14j
  d'observation (vs 0/0 mesuré 2026-04-24).
- **Scoring v2.1.1 prédictif post-30j** : `internal_pnl_score` collecté
  sur ≥ 50 % des wallets ACTIVE après 30j (cold-start cleared).
- **Arbitrage bots filtrés** : pas de gain `cash_pnl_90d > 0` admis quand
  le wallet est juste un extracteur YES+NO neutre directionellement.
- **H-007 corrigé** : eviction state machine consomme les fresh scores —
  cohérent avec la recalibration MA.7.
- **Probation actif** : wallets `[10, 50)` trades sized `0.25×` jusqu'à
  passage du gate full ou demote ranking.

Tests cumulés estimés : **~26 tests unit** + **3 tests intégration**.
Charge cumulée : 5 jours dev + 30 jours calendaires data collection.
Prérequis : MA shippé (v2.1 stable). Bloque : MF (v2.2-DISCRIMINATING
capstone).

---

## 1. Motivation & use case concret

### 1.1 Le symptôme observé — 2026-04-24

Dump dashboard `/performance` user 2026-04-24 sur le test 14 jours
`uni-debian` :

> - Wallet `0x21ffd2b7…0d71` : status **ACTIVE depuis 5 jours**.
>   - Win rate observé : **19 %** (10 W / 42 L sur les positions copiées
>     fermées).
>   - PnL cumulatif observé : **−$0.55** (cristallisé via M13 Bug 5 SELL +
>     M13 v2 neg_risk resolution).
>   - Score v2.1 du cycle courant : **0.66** (au-dessus de
>     `SCORING_DEMOTION_THRESHOLD=0.30` par 2σ).
>   - Pas une seule transition `active → shadow` automatique sur 5 jours.
> - User a manuellement blacklisté via `.env` + restart bot.
> - Wallet `0x63d4…a2f1` : score v2.1 = **0.83**, mais **dernier trade
>   détecté il y a 15 h**. Pompe un slot `MAX_ACTIVE_TRADERS=10` sans
>   produire de signal copy-tradeable.
> - Discovery panel `/home` : **0 promotions / 0 demotions sur 24 h** avec
>   pool 43 shadow + 7 active. Zéro rotation alors que les scores doivent
>   bouger sur ce volume de candidats.

Diagnostic croisé via 3 deep-searches + audit code 2026-04-24 + session A
brouillon (`docs/bug/session_A_anti_toxic_trader_lifecycle.md`) :

| Symptôme | Root cause | Référence |
|---|---|---|
| Wallet 0x21ffd…0d71 ACTIVE 5j WR 19 % PnL −$0.55 | Aucun signal de performance **interne** dans v2.1 — formule fit historique du wallet source, pas la PnL réalisée par polycopy depuis qu'on copie | F02 (3/3), Claude §3.2 + item 8, Convexly E2 |
| Aucun demote sur 24h pool 50/10 | `_decide_active` utilise `SCORING_DEMOTION_THRESHOLD=0.30` absolu — quand le pool drift, un wallet rank-worst peut rester >0.30 indéfiniment | F06 (3/3), Claude C11 + §9 item 4 |
| 0x63d4…a2f1 score 0.83 dernier trade 15h | `risk_adjusted` dominé par sentinel sur curve plate → wallets dormants haut-rankés. M14 MA.3 fixe partiellement (médiane), mais pas de signal "is the wallet still actively producing copy-tradeable signals?" | Claude C10 + complément MA.3 |
| H-007 audit | `classify_sell_only_transitions` calcule `_delta_vs_worst` sur snapshots stale tandis que `self_score`/`triggering_score` sont fresh — incohérence | [H-007 audit] |
| Arbitrage bots passent tous nos gates | Wallets YES+NO net-zero ont `cash_pnl_90d > 0`, `trade_count ≥ 50`, `days_active ≥ 30`, non-zombie, non-wash. Mais leur PnL n'est pas **transférable** à un copy-trader | F14 (Claude §9 item 5 + A10 + $40M/an Dev Genius) |
| Wallets `[10, 50)` trades exclus par gate | M14 préserve `trade_count ≥ 50` (cold-start mode `≥ 20`) — blinds Mitts-Ofir-type wallets concentrés black-swan | F11 (Gemini § Cold-Start + Claude §9 item 11) |

**8 défauts simultanés**, indépendants par la cause mais couplés dans
leurs effets. Un patch unitaire (auto-blacklist seul, ou ranking seul, ou
internal_pnl seul) ne suffit pas — il faut le bundle.

### 1.2 Pourquoi un seul livrable bundle

1. **MB.1 collecteur sans MB.2 facteur** = donnée orpheline non utilisée.
2. **MB.2 facteur sans MB.1 collecteur** = facteur retournant `None` partout.
3. **MB.3 ranking sans MB.7 arbitrage_bot gate** = ranking promu sur arb bots
   au top du pool.
4. **MB.6 probation sans MB.1 internal_pnl** = on copie 0.25× mais on n'a
   aucun signal pour valider/invalider la probation après 30j.
5. **MB.4 fix H-007 sans MA.7 margin recalibration** = swap mais delta calculé
   sur scores stale (regression MA.7).
6. **MB.8 auto-blacklist sans MB.3 ranking** = on garde un mécanisme purement
   threshold quand on devrait avoir ranking + safeguard combiné.

Donc M15 = **un seul bundle, 8 commits atomiques sur `main`** suivant
pattern M12 / M14. Chaque commit isolé est testable mais le résultat
business émerge du couplage.

### 1.3 Ce qui ne change PAS dans M15

Diff M15 strictement additif sur les invariants suivants — aucune ligne
modifiée :

- **Lifecycle M5** : `shadow → active → sell_only → shadow → ... | pinned
  | blacklisted`, cap `MAX_ACTIVE_TRADERS`, `BLACKLISTED_WALLETS` discipline
  réversible, `pinned` jamais demote-able.
- **M5_bis eviction core** : `EvictionScheduler.run_cycle` orchestration,
  `CascadePlanner` pure logic (1 swap/cycle EC-2), `HysteresisTracker`
  in-memory. M15 fixe **uniquement** la propagation des fresh scores
  (MB.4) — la state machine garde sa structure.
- **M5_ter watcher live-reload** : `WalletPoller`, `DataApiClient`,
  `list_wallets_to_poll` strict `status IN ('active', 'pinned',
  'sell_only')` + double-check `BLACKLISTED_WALLETS`. M15 ne touche pas.
- **M11 latency / WS market** : `ClobMarketWSClient`, 6 stages
  `trade_latency_samples`, `/latency`. Intacts.
- **M12 squelette + M14 v2.1** : sous-package `discovery/scoring/v2/` +
  pure functions par facteur + `gates.py` + `normalization.py` (rank
  transform MA.2) + `aggregator.py` (pondérations renormalisées MA.1)
  préservés. M15 ajoute **1 facteur** + **1 gate** + bump version
  `"v2.1.1"`.
- **M13 dry-run** : `PositionSizer` side-aware (Bug 5),
  `DryRunResolutionWatcher` neg_risk (M8 v2), `/home` cards. M15
  réutilise `my_positions.realized_pnl` (cristallisé par M13 Bug 5
  SELL + M8 v2 resolution) en lecture pour le collecteur internal_pnl
  — n'écrit rien.
- **M16 fees dynamiques** : `FeeRateClient` + `STRATEGY_FEES_AWARE_ENABLED`
  + EV after-fee dans `_check_buy`. M15 réutilise la même path
  `_check_buy` pour appliquer le multiplicateur probation 0.25× **après**
  le rejet `ev_negative_after_fees` (la probation est un sizing layer, pas
  un EV gate).
- **Triple garde-fou M3 + 4ᵉ M8** : M15 reste 100 % read-only côté
  scoring/lifecycle (Data API + Gamma + DB locale). Aucune creds CLOB
  consommée. Le seul write nouveau est l'`auto_blacklist` transition
  M5_bis-style (status DB), équivalent à un append `reconcile_blacklist`.

### 1.4 Ce que change explicitement M15 (vue de haut)

| Module | Diff | Référence MB |
|---|---|---|
| [src/polycopy/storage/models.py](../../src/polycopy/storage/models.py) | +`MyPosition.source_wallet_address: str | None` (nullable, FK logique) + `TargetTrader.is_probation: bool = False` | MB.1 + MB.6 |
| [migrations/versions/0009_*.py](../../migrations/versions/) | Migration Alembic 0009 — nouveaux champs + index secondaire | MB.1 + MB.6 |
| [src/polycopy/discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py) | `_compute_internal_pnl_score(wallet)` + `_compute_net_exposure_ratio(wallet)` | MB.1 + MB.7 |
| [src/polycopy/discovery/scoring/v2/factors/internal_pnl.py](../../src/polycopy/discovery/scoring/v2/factors/) | Nouveau factor module — `compute_internal_pnl(metrics) -> float | None` | MB.2 |
| [src/polycopy/discovery/scoring/v2/aggregator.py](../../src/polycopy/discovery/scoring/v2/aggregator.py) | Pondérations v2.1.1 (drop 0.20 sur 5 autres au profit `internal_pnl=0.25`). Cold-start renormalisation locale si `None`. | MB.2 |
| [src/polycopy/discovery/scoring/v2/gates.py](../../src/polycopy/discovery/scoring/v2/gates.py) | Nouveau gate `not_arbitrage_bot` ajouté en queue de `check_all_gates` | MB.7 |
| [src/polycopy/discovery/decision_engine.py:290-339](../../src/polycopy/discovery/decision_engine.py#L290) | `_decide_active` ranking-based + safeguard `< 0.30` + auto-blacklist branch | MB.3 + MB.8 |
| [src/polycopy/discovery/eviction/state_machine.py:75-102](../../src/polycopy/discovery/eviction/state_machine.py#L75) | `_delta_vs_worst` consomme `inputs.scores` pour les active_non_pinned (fix H-007) | MB.4 |
| [src/polycopy/discovery/eviction/scheduler.py](../../src/polycopy/discovery/eviction/scheduler.py) | Boot helper `_log_empirical_margin_recommendation(settings, sf)` co-lancé | MB.5 |
| [src/polycopy/strategy/pipeline.py:155-258](../../src/polycopy/strategy/pipeline.py#L155) | `PositionSizer._check_buy` multiplie `my_size *= 0.25` si `is_source_probation=True` | MB.6 |
| [src/polycopy/watcher/poller.py](../../src/polycopy/watcher/poller.py) | `DetectedTradeDTO` enrichi avec `is_source_probation` (1 query batch par cycle, pas N+1) | MB.6 |
| [src/polycopy/storage/dtos.py](../../src/polycopy/storage/dtos.py) | `DetectedTradeDTO` +`is_source_probation: bool = False` | MB.6 |
| [src/polycopy/monitoring/templates/trader_auto_blacklisted.md.j2](../../src/polycopy/monitoring/templates/) | Nouveau template Telegram MarkdownV2 | MB.8 |
| [src/polycopy/monitoring/dtos.py](../../src/polycopy/monitoring/dtos.py) | `Alert.event` Literal +`"trader_auto_blacklisted"` | MB.8 |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +5 settings (`scoring_internal_min_positions`, `scoring_internal_pnl_scale_usd`, `auto_blacklist_pnl_threshold_usd`, `auto_blacklist_min_positions_for_wr`, bump `scoring_version` literal `"v2.1.1"`) | tous |
| [src/polycopy/storage/repositories.py](../../src/polycopy/storage/repositories.py) | `MyPositionRepository.sum_realized_pnl_by_source_wallet(...)` + `TargetTraderRepository.set_probation(wallet, on)` | MB.1 + MB.6 |
| [scripts/validate_mb_hypotheses.py](../../scripts/) | Nouveau — H-EMP-3 + H-EMP-11 + H-EMP-13 | hypothèses |
| Tests | +26 unit + 3 intégration | tous |

### 1.5 Pourquoi pas v2.2-DISCRIMINATING tout de suite

[Claude §4.2
v2.2-DISCRIMINATING](../deepsearch/claude_Architectural_Review_of_Scoring_Discovery_and_Latency_for_a_Single-Process_asyncio_Polymarket_Copy-Trader_(April_2026).md)
propose `0.30·internal_pnl + 0.25·informed_score (Mitts-Ofir composite) +
0.15·sortino_robust + 0.15·calibration_proper + 0.15·wash_penalty ×
not_arb_bot`. **3 raisons pour ne pas la livrer maintenant** :

1. **Sirolly wash cluster continuous score** (poids 0.15) = ~3-4 jours de
   port Python du graph clustering algorithm Sirolly et al. SSRN 5714122 →
   migre en **MF**.
2. **Mitts-Ofir composite informed_score** complet (timing_alpha vrai +
   market_hhi + size anomaly + liquidity-adjusted ROI) = features
   engineering spécifique → migre en **MF**.
3. **Internal PnL feedback (poids 0.30)** exige **30j de copy data réelle**.
   Sans M15 MB.1, le facteur n'a **pas de donnée**. M15 démarre la
   collecte ; MF la consomme.

M15 est la **fondation** qui débloque MF — sans MB.1 + MB.2, MF est juste
de la sophistication architecturale sur du vide.

---

## 2. Scope / non-goals

### 2.1 Dans le scope

**MB.1 — Collecteur `internal_pnl_score(w)`** :

- Nouvelle méthode `MetricsCollectorV2._compute_internal_pnl_score(wallet)
  → float | None`.
- Query SQL : `SELECT realized_pnl FROM my_positions WHERE
  source_wallet_address = :wallet AND closed_at IS NOT NULL AND
  simulated = :mode_filter AND closed_at > :since_30d` (filtre selon
  `execution_mode` — cf. décision **D8** infra).
- Calcul : `signed_pnl_30d = SUM(realized_pnl)`, `count = COUNT(*)`. Si
  `count < SCORING_INTERNAL_MIN_POSITIONS=10` → retourne `None`.
- Sinon : `internal_pnl_score = sigmoid(signed_pnl_30d /
  SCORING_INTERNAL_PNL_SCALE_USD)` avec `sigmoid(x) = 1 / (1 + exp(-x))`,
  scale default `10.0` (≈ +$10 / 30j ↔ score 0.73).
- Pure et async (1 query SQL, 0 réseau).

**MB.2 — Facteur `internal_pnl` dans v2.1.1** :

- Nouveau module `src/polycopy/discovery/scoring/v2/factors/internal_pnl.py`
  exposant `compute_internal_pnl(metrics: TraderMetricsV2) -> float |
  None`.
- `TraderMetricsV2` enrichi avec `internal_pnl_score: float | None = None`
  (alimenté par `MetricsCollectorV2.collect`).
- `aggregator.py` :
  - Bump `_WEIGHT_RISK_ADJUSTED 0.3125 → 0.25`,
    `_WEIGHT_CALIBRATION 0.2500 → 0.20`,
    `_WEIGHT_SPECIALIZATION 0.1875 → 0.15`,
    `_WEIGHT_CONSISTENCY 0.1250 → 0.075`,
    `_WEIGHT_DISCIPLINE 0.1250 → 0.075`,
    `_WEIGHT_INTERNAL_PNL = 0.25` (nouveau).
  - Cold-start branch : si `internal_pnl_score is None` → calcul score
    sur les **5 facteurs hérités** avec renormalisation locale (chaque
    poids divisé par `0.75` = somme post-drop) → somme à 1.0 sur 5
    facteurs.
  - Bump `SCORING_VERSION` literal `Literal["v1", "v2", "v2.1", "v2.1.1"]`.
    Default reste `"v1"`. v2.1 conserve sa fonction registry intacte
    (audit trail sacré M14).

**MB.3 — Ranking-based activation** :

- `_decide_active` (decision_engine.py:290-339) refactor :
  - Fetch les scores de tous les wallets `status='active'` du cycle.
  - Si rank du wallet courant `> MAX_ACTIVE_TRADERS` → incrément
    `consecutive_low_score_cycles` (réutilise hystérésis M5).
  - Si `new_count >= SCORING_DEMOTION_HYSTERESIS_CYCLES=3` → demote
    shadow + `previously_demoted_at = now`.
- Garde-fou absolu `< SCORING_ABSOLUTE_HARD_FLOOR=0.30` toujours actif :
  un wallet sous 0.30 force-demote même s'il est dans le top-N (cas
  pathologique : tous les autres scores < 0.30).
- Préservation `pinned` jamais demote-able (safeguard M5).
- `previously_demoted_at` posé via `set_previously_demoted_at` (M5_bis
  signature inchangée).

**MB.4 — Fix EvictionScheduler scores stale (audit H-007)** :

- `state_machine.classify_sell_only_transitions(inputs, tracker, blacklist)`
  inchangée signature, mais utilise désormais `inputs.scores` aussi
  pour les active_non_pinned dans `_delta_vs_worst`.
- Helper `_delta_vs_worst(self_score, active_non_pinned, scores)` :
  remplace `t.score` (DB stale) par `scores.get(t.wallet_address.lower(),
  t.score or 0.0)` (fresh).
- Pas de migration DB ni de breaking signature.

**MB.5 — `_log_empirical_margin_recommendation`** :

- Helper module `src/polycopy/discovery/eviction/scheduler.py` (privé) :
  `async def _log_empirical_margin_recommendation(settings, session_factory)`.
- Logique : `SELECT score FROM trader_scores WHERE scoring_version IN
  ('v2.1', 'v2.1.1') AND cycle_at > now - 7d AND target_trader_id IN
  (active wallets)`. Calcule `pstdev(scores)`, log structlog
  `eviction_margin_empirical_recommendation` avec `current=settings.
  eviction_score_margin`, `empirical_1_sigma`, `recommended_range=
  [σ × 0.8, σ × 1.2]`.
- Appelé une fois au boot du `EvictionScheduler` (post `__init__`).
  No-op si `EVICTION_ENABLED=false`.
- **Aucun auto-ajustement** — décision humaine via `.env` tweak.

**MB.6 — Probation fractional-Kelly** :

- Migration 0009 : `ALTER TABLE target_traders ADD COLUMN is_probation
  BOOLEAN NOT NULL DEFAULT FALSE`.
- `DecisionEngine` :
  - Wallet absent + score `>= scoring_promotion_threshold` + `10 ≤
    trade_count_90d < 50` (gates relaxés en cold-start mode probation,
    `days_active ≥ 7` au lieu de `≥ 30`) → `discovered_shadow` mais
    avec flag `is_probation=True`. Sinon flow standard.
  - Promotion shadow → active : `is_probation` reste True jusqu'au gate
    full (`trade_count_90d >= 50 AND days_active >= 30`). Ce check
    arrive dans `_decide_active` ou cycle dédié — voir §5.6.
  - Auto-release : `if metrics.trade_count_90d >= 50 and
    metrics.days_active >= 30 and current.is_probation:` →
    `set_probation(wallet, on=False)` + `trader_events`
    `probation_released`.
- `WalletPoller` (M5_ter) : enrichit chaque `DetectedTrade` extrait avec
  `is_source_probation` lu sur `TargetTrader.is_probation` (1 query batch
  par cycle, jamais N+1 dans le pipeline).
- `DetectedTradeDTO` (`storage/dtos.py`) : +`is_source_probation: bool =
  False` (default safe pour les anciens DTOs).
- `PositionSizer._check_buy` (post fee-check M16) :
  ```python
  if ctx.trade.is_source_probation:
      raw_my_size *= 0.25  # quarter-Kelly probation
  ```
  Application stricte avant `ctx.my_size = raw_my_size`. SELL passthrough
  (un SELL ne dimensionne pas une nouvelle position).
- Probation **ne bypasse PAS** : `cash_pnl_90d > 0`, `not_blacklisted`,
  `not_wash_cluster`, `not_arbitrage_bot` (MB.7). Ces 4 gates restent
  durs même en probation.

**MB.7 — Arbitrage bot filter** :

- Nouvelle méthode `MetricsCollectorV2._compute_net_exposure_ratio(wallet)
  → float`.
  - Pour chaque `condition_id` du wallet : `yes_net = sum(size pour
    positions outcome_index=0)`, `no_net = sum(size pour positions
    outcome_index=1)`. `gross = abs(yes_net) + abs(no_net)`. `ratio =
    abs(yes_net - no_net) / gross if gross > 0 else 1.0`.
  - Aggregé sur tous les `condition_ids` 90j : `avg_net_exposure =
    mean(ratio_per_cond)`.
- `TraderMetricsV2` enrichi avec `net_exposure_ratio: float = 1.0`
  (default safe = "directional, ne déclenche pas").
- Nouveau gate `check_not_arbitrage_bot(metrics)` dans `gates.py` :
  ```python
  passed = metrics.net_exposure_ratio >= 0.10
  ```
  Ajouté en **queue** de `check_all_gates` (ordre fail-fast : env
  lookups d'abord, DTO lookups simples ensuite, computation lookup en
  dernier).
- Wallet rejeté écrit `trader_events.event_type="gate_rejected"` avec
  `event_metadata={"reason_code": "arbitrage_bot_pattern",
  "net_exposure_ratio": 0.07, "condition_ids_count": 23}`.

**MB.8 — Auto-blacklist seuil PnL cumulé + alertes Telegram** :

- Nouveau setting `auto_blacklist_pnl_threshold_usd: Decimal =
  Decimal("-5.00")` (Pydantic, range `[-1000.0, 0.0]`).
- Nouveau setting `auto_blacklist_min_positions_for_wr: int = 30`
  (Pydantic, range `[10, 200]`).
- `_decide_active` (M5 + M5_bis path) post-decision : si le wallet vient
  de transitionner ou keep ACTIVE, calcule :
  ```python
  cumulative_observed_pnl = ...  # via MyPositionRepository.sum_realized_pnl_by_source_wallet
  observed_position_count = ...
  observed_win_rate = ...
  if cumulative_observed_pnl < settings.auto_blacklist_pnl_threshold_usd:
      auto_blacklist(wallet, reason="pnl_threshold")
  elif observed_position_count >= settings.auto_blacklist_min_positions_for_wr \
       and observed_win_rate < 0.25:
      auto_blacklist(wallet, reason="win_rate_floor")
  ```
- `auto_blacklist(wallet, reason)` :
  - `transition_status_unsafe(wallet, "blacklisted")` + écrit
    `trader_events` event `auto_blacklisted` avec `event_metadata={
    reason, observed_pnl, observed_wr, observed_position_count}`.
  - Push alert `trader_auto_blacklisted` (level WARNING, cooldown_key =
    `f"auto_blacklist_{wallet}"` — idempotence cf. §14.4).
- Nouveau template
  `src/polycopy/monitoring/templates/trader_auto_blacklisted.md.j2`
  (MarkdownV2 escape strict cohérent M7) avec : wallet (short hash) +
  reason humaine + stats observées + lien dashboard.
- Réversibilité : retrait via `BLACKLISTED_WALLETS` env var modif (le
  wallet auto-blacklist ne s'ajoute pas à `BLACKLISTED_WALLETS` env —
  la transition vit en DB seulement). `reconcile_blacklist` n'écrase
  pas un blacklist DB-only sans `BLACKLISTED_WALLETS` env entry —
  documenté §14.4 piège.

### 2.2 Hors scope explicites (liste exhaustive)

- ❌ **Sirolly wash cluster continuous score** — algo graph clustering
  `wash_cluster_score(w) ∈ [0, 1]` continu (~3-4j Python port). Migre en
  **MF**.
- ❌ **Mitts-Ofir composite `informed_score`** complet (timing_alpha
  vrai + market_hhi + conviction_sigma + liquidity_adjusted_roi). MA.5
  M14 inverse seulement le **signe** HHI ; M15 ne touche pas. Migre en
  **MF**.
- ❌ **CLV (Closing Line Value)** — exige snapshots time-series prix
  pré-résolution. Migre en **MG**.
- ❌ **Kelly proxy `conviction_sigma`** comme **factor scoring** — M15
  utilise `0.25` Kelly **uniquement** pour le **sizing probation**, pas
  comme signal de scoring. Migre en **MG**.
- ❌ **Liquidity-adjusted ROI (Kyle's λ)** — exige depth historique →
  **MG**.
- ❌ **Thompson Sampling Trend-Aware ranking** — l'approximation
  rank-based MB.3 + threshold safeguard est suffisante (Claude §4.1 +
  §7.3). Migre en spec dédiée si besoin de dynamic rebalancing prouvé.
- ❌ **Dashboard `/performance` colonne `internal_pnl_score`** (UX sur
  la donnée que MB.1+MB.2 produisent). Migre en **MH**.
- ❌ **Dashboard `/strategy` panel "fee-drag par wallet probation"**.
  Migre en **MH**.
- ❌ **Convergence signal cross-wallet agreement** (Bullpen-style). Migre
  en **MF** (fait sens avec `informed_score`).
- ❌ **Anti-copy bait detection** (Claude §9 item 9 — wallet qui post
  bait fills pour piéger les copy bots). Hors scope v2.1.1, spec future
  qui consomme l'`internal_pnl_score` que M15 produit.
- ❌ **Latency tolerance factor** (`avg_holding_time` comme facteur
  scoring). Migre en **MG**.
- ❌ **Fenêtre rolling 180j + exp decay half-life 30j**. M15 garde
  fenêtre 30j strict pour `internal_pnl_score` (cohérent budget compute).
  Hors scope, v3 future.
- ❌ **Auto-detection wash cluster** continue. M15 garde
  `WASH_CLUSTER_WALLETS` manuel via env (contrat M14). Migre en **M17+**.
- ❌ **Migration des rows historiques v2.1 en v2.1.1**. Versioning
  sacré : aucune row réécrite.
- ❌ **Bumper `SCORING_VERSION` default à `"v2.1.1"` au merge**. Default
  reste `"v1"`. La cutover v1 → v2.1 → v2.1.1 reste 100 % manuelle, par
  étapes (cf. §11.4).
- ❌ **Nouveau onglet dashboard `/probation`**. Le filtrage par
  `is_probation` se fait via `/performance?status=probation` (URL
  query) ou `/traders?probation=true` — UX additif minimal géré dans MH.
- ❌ **Backfill rétroactif `source_wallet_address` sur les anciens
  `my_positions`**. Les positions M3..M14 ouvertes avant migration 0009
  resteront `source_wallet_address=NULL`. Le collecteur internal_pnl
  ignore ces rows (filtre `source_wallet_address = :wallet`). Acceptable
  cold-start.
- ❌ **Goldsky free Starter pour booster `_compute_net_exposure_ratio`
  scale 90j sur 50 wallets**. Garde Data API `/positions` actuel + 1
  query par cycle. Migre en **MJ** ou plus tard si quota Data API
  insuffisant.
- ❌ **Auto-blacklist via Telegram bot incoming command** — viole
  l'invariant M7 §13 "Telegram emitter-only". Le user doit toujours
  modifier `.env` + redémarrer pour retirer un wallet.

---

## 3. User stories

### 3.1 Story A — Wallet 0x21ffd…0d71 auto-démontée 24h après ship M15

**Avant M15** (observation 2026-04-24) :

- Wallet `0x21ffd2b7…0d71` ACTIVE depuis 5 jours.
- Win rate observé : 19 % (10 W / 42 L), PnL observé : −$0.55.
- Score v2.1 = 0.66 (au-dessus du `SCORING_DEMOTION_THRESHOLD=0.30`).
- Aucun mécanisme automatique ne le démote. User blacklist manuel `.env`.

**Après M15** (post-ship + 24h cycle discovery) :

- T+0 : ship M15. Wallet déjà ACTIVE, status inchangé en T+0.
- T+0 : `MetricsCollectorV2._compute_internal_pnl_score(0x21ffd…)` calcule
  `signed_pnl_30d = -$0.55`, `count = 52` ≥ 10 → `score =
  sigmoid(-0.55 / 10) = sigmoid(-0.055) = 0.486`. Très bas mais pas
  catastrophique en isolation.
- T+0 : aggregator v2.1.1 calcule score complet :
  - `risk_adjusted_rank = 0.7` (via M14 v2.1).
  - `calibration_rank = 0.6`.
  - `specialization_rank = 0.4`.
  - `consistency_rank = 0.3`.
  - `discipline_rank = 0.5`.
  - `internal_pnl_rank = 0.486` rank-normalized contre pool ≈ 0.10
    (bottom 10% du pool pour internal_pnl).
  - Score final = `0.25·0.10 + 0.25·0.7 + 0.20·0.6 + 0.15·0.4 +
    0.075·0.3 + 0.075·0.5 = 0.025 + 0.175 + 0.12 + 0.06 + 0.022 +
    0.038 = 0.44`. Drop substantiel vs 0.66 v2.1.
- T+0 : `_decide_active` ranking-based : pool active 7 wallets, scores
  `[0.44, 0.61, 0.65, 0.71, 0.78, 0.81, 0.83]`. `0x21ffd…` rank 7/7
  > `MAX_ACTIVE_TRADERS=10` (le wallet est encore dans top-10 du pool
  active actuel — pool < cap, pas de pression). Mais
  `cumulative_observed_pnl = -$0.55 > settings.
  auto_blacklist_pnl_threshold_usd=-$5` → pas de fire. Et
  `observed_win_rate = 0.19 < 0.25` MAIS `observed_position_count = 52
  ≥ 30` → **auto-blacklist fire** !
- T+0 (3s plus tard) : alert Telegram `trader_auto_blacklisted` reçue
  par user :
  > 🚫 *Wallet auto-blacklisté*
  > Wallet : `0x21ffd2b7…0d71`
  > Raison : win-rate observé 19 % sur 52 positions copiées
  > PnL observé : −$0.55
  > Status : `active → blacklisted` (M15 MB.8)
  > [📊 Dashboard](http://uni-debian.tail-xxxxx.ts.net:8787/)
- T+1 cycle : `target_traders.status='blacklisted'`. `WalletPoller`
  M5_ter exclut le wallet du polling. Plus aucun trade copié de
  `0x21ffd…0d71`.
- T+0 + 7j : si l'utilisateur veut le re-tester, **PAS de retrait
  automatique** — il doit ajouter le wallet à `BLACKLISTED_WALLETS=...`
  `.env` puis le retirer (cohérent discipline M5_bis réversible) — OU
  `transition_status_unsafe(wallet, "shadow")` manuel via SQL. Cf. §11.5.

**Bénéfice quantifié** : pas besoin d'intervention manuelle, le wallet
est sorti en < 6h après détection — 5 jours de gain sur l'observation
réelle.

### 3.2 Story B — Wallet shadow 25 trades à forte timing_alpha promu en probation

**Avant M15** : un wallet candidat `0xMITTSOFIR-LIKE…` apparaît dans le
pool shadow avec :
- 25 trades sur 14j (`trade_count_90d=25 < 50` gate hard).
- HHI catégories = 0.92 (concentration extrême, signal Mitts-Ofir-like
  post-M14 MA.5).
- Win rate 64 % (16 W / 9 L), PnL +$210 sur ses 25 trades.
- `cash_pnl_90d` > 0, non-blacklisted, non-wash.

Le gate `trade_count ≥ 50` (cold-start mode `≥ 20`) **rejette** le
wallet. Il n'est jamais scoré. Le pool active rate des opportunités
black-swan que Mitts-Ofir documente.

**Après M15** (avec MB.6 probation) :

- Cycle discovery : `MetricsCollectorV2.collect(wallet)`. Le wallet
  passe le check probation `(10 ≤ trade_count_90d < 50) AND
  (days_active ≥ 7) AND cash_pnl > 0 AND not_blacklisted AND
  not_wash AND not_arbitrage_bot` → **promotable en probation**.
- `DecisionEngine.decide` : insert shadow + `is_probation=True` +
  `trader_events` event `discovered_shadow` avec `event_metadata=
  {is_probation: True}`.
- T+0 + `TRADER_SHADOW_DAYS=7` : `_decide_shadow` promote actif
  (score ≥ promotion threshold + days_observed ≥ 7). Le wallet est
  ACTIVE avec `is_probation=True`.
- Cycle suivant : `WalletPoller` détecte un trade BUY YES $0.40 size 100.
  Pipeline strategy `PositionSizer._check_buy` : `raw_my_size = 100 ×
  copy_ratio=0.01 = 1.0` → fee check OK → **probation multiplier**
  `my_size = 1.0 × 0.25 = 0.25`. L'order final est `BUY size=0.25
  @ $0.40 = $0.10`.
- 30j plus tard : le wallet a accumulé `trade_count_90d=58`,
  `days_active=42`. `MetricsCollectorV2.collect` détecte gate full
  satisfied. `_decide_active` (post-discover hook) appelle
  `set_probation(wallet, on=False)` + `trader_events` event
  `probation_released`. Sizing redevient normal `1.0`.

**Bénéfice quantifié** : on capture un Mitts-Ofir-like wallet avec un
sizing 0.25× défensif pendant 30 jours, sans le rater complètement
comme M14 le fait actuellement.

### 3.3 Story C — Arbitrage bot rejeté au scoring (MB.7)

**Avant M15** : un wallet `0xARB…` est un arbitrageur YES+NO sur
neg_risk markets. Pattern :
- Sur `cond_id=0xC1` : BUY YES size 1000 @ 0.42, BUY NO size 1000 @
  0.59. `yes_net=1000, no_net=1000, gross=2000, ratio=0/2000=0`.
- Sur 23 `condition_ids` distincts en 90j : tous `ratio≈0` (parfait
  YES+NO neutre).
- `cash_pnl_90d` = +$340 (1.7 % du gross volume — typique arbitrage).
- `trade_count_90d=460`, `days_active=85`. Tous gates M14 passés.

Score v2.1 du wallet : 0.71 → **promu ACTIVE**. Polycopy copie ses
trades :
- BUY YES size 10 @ 0.42 sur cond C1 → côté polycopy : OK.
- BUY NO size 10 @ 0.59 sur cond C1 → `PositionSizer._check_buy`
  rejette `position_already_open` (M13 Bug 5 garde-fou). Pas de
  duplication.
- Mais les YES seuls ne sont **pas le signal directionnel** du wallet
  — ils sont la moitié d'une arb. Polycopy paie 0.42 pour YES, le
  marché résout NO → perte $0.42 × 10 = $4.20.
- Sur 90j de copie : polycopy accumule des pertes structurelles.

**Après M15 MB.7** :

- `MetricsCollectorV2._compute_net_exposure_ratio(0xARB)` calcule
  `avg_net_exposure = 0.07` (sous le seuil 0.10).
- `check_not_arbitrage_bot(metrics)` retourne `passed=False, reason=
  "arbitrage_bot_pattern"`.
- `gates.check_all_gates` short-circuit : wallet **jamais scoré**,
  écrit `trader_events` event `gate_rejected` avec `event_metadata=
  {gate: "not_arbitrage_bot", net_exposure_ratio: 0.07,
  condition_ids_count: 23}`.
- Wallet reste shadow (pas de promotion) ou si déjà active → pas de
  re-scoring → `_decide_active` reçoit `score=0` (pas calculé) → ranking
  pousse en bas → demote shadow après hystérésis.

**Bénéfice quantifié** : économie estimée 2-5 % du capital sur 90j (le
$40M/an documentés Dev Genius / Claude §A10 sont la preuve macro de
l'impact).

### 3.4 Story D — H-007 fixé : eviction cohérente entre cycles (MB.4)

**Avant M15** : pool active 8 wallets, 1 sell_only `0xSO…` (entered_at=
T-2j, triggering_wallet=`0xC1`). Cycles :

- T-1 cycle : pool active scores `[0.42, 0.51, ..., 0.78]` worst=0.42.
  `0xSO` score=0.55, `0xC1` score=0.65. Delta = 0.10 (= margin). Pas
  d'abort.
- T+0 cycle : `_classify_cascade` refresh les scores → `0xSO=0.58,
  0xC1=0.62`. Delta = 0.04 < 0.10 → abort armé.
  - **Bug H-007** : `classify_sell_only_transitions` consomme
    `inputs.scores[0xSO]=0.58` (fresh), `inputs.scores[0xC1]=0.62`
    (fresh) → delta correct 0.04. **Mais** `_delta_vs_worst(0xSO,
    active_non_pinned)` consomme `t.score` de la DB stale — worst
    active retourné depuis le snapshot DB est encore `0xWA` à 0.42
    (pas refresh dans le snapshot pré-state-machine). Delta vs worst =
    0.58 - 0.42 = 0.16 (rappelé dans `EvictionDecision.delta_vs_worst_active`).
  - Résultat : la décision abort est correcte mais le `delta_vs_worst`
    rapporté dans `trader_events` est stale → audit trail confusion.

**Après M15 MB.4** :

- `state_machine` consomme `inputs.scores` aussi pour les
  active_non_pinned :
  ```python
  def _delta_vs_worst(self_score, active_non_pinned, scores):
      if not active_non_pinned:
          return None
      worst = min(active_non_pinned,
                  key=lambda t: (scores.get(t.wallet_address.lower(), t.score or 0.0),
                                 t.wallet_address))
      worst_fresh = scores.get(worst.wallet_address.lower(), worst.score or 0.0)
      return self_score - worst_fresh
  ```
- T+0 cycle post-fix : worst active fresh = 0.45 (`0xWA` a remonté 0.03
  ce cycle aussi). Delta vs worst = 0.58 - 0.45 = 0.13. Audit trail
  cohérent.

**Bénéfice quantifié** : pas d'impact PnL direct (la décision abort
était déjà correcte), mais audit trail enfin cohérent → debugging
eviction décisions devient possible.

### 3.5 Story E — Pool dormant + 1 candidat brillant : ranking force la rotation (MB.3)

**Avant M15** : pool active 5 wallets, scores `[0.51, 0.55, 0.58, 0.61,
0.66]` (2026-04-24 réel + projeté MA.7 margin 0.10). Pool shadow 38
wallets, top score 0.78.

- M5/M14 logic : `_decide_active(0xACT5_score=0.66)` : score ≥ 0.30 →
  `keep`. Aucun demote.
- M5_bis eviction : delta candidat-worst = 0.78 - 0.51 = 0.27 ≥ 0.10
  → eviction triggers... mais `EVICTION_ENABLED=false` par défaut. Si
  off → 0 rotation.

**Après M15 MB.3** (avec `EVICTION_ENABLED=false`, défault user) :

- `_decide_active(0xACT5)` : ranking calcul. Pool active 5 wallets,
  rank de `0xACT5_score=0.51` = 5/5 > `MAX_ACTIVE_TRADERS=5` ? Non,
  cap n'est pas atteint. Mais **shadow** top score 0.78 est out-of-pool.
  Si le pool a slot disponible (cap=10, current=5) → pas de pression.
- Wait, MB.3 ne touche que le **demote** path (active rank > MAX_ACTIVE).
  Le pool est sub-cap → personne n'est out-of-top-N → personne demote.
- **Mais avec internal_pnl** (MB.2) : le score `0xACT5` post-v2.1.1
  inclut maintenant son `internal_pnl_score`. Si polycopy a copié son
  trades 30 jours et observe `signed_pnl=-$2.10` → `internal_pnl_score=
  sigmoid(-0.21) = 0.448`. Score v2.1.1 drop de 0.51 → ~0.36.
- `auto_blacklist_pnl_threshold_usd=-$5.00` pas atteint (−$2.10 > −$5).
  Pas d'auto-blacklist.
- Pool size still 5/10 → pas de demote. Mais sur le **shadow cycle** :
  `0xSHADOW_TOP_score=0.78` → `_decide_shadow` promote → pool 6/10.
  Rotation effective.

**Verdict** : la pathologie 2026-04-24 (0 promotion / 0 demotion sur
24h) vient principalement du fait que **les scores ne bougent pas**
(M14 corrige). MB.3 ranking-based est utile **quand le pool atteint
le cap** ; sinon le pool sub-cap accepte tout candidat ≥ promotion
threshold.

**Cas démonstratif MB.3** : pool active 10/10, scores `[0.31, 0.35,
0.40, 0.55, 0.58, 0.61, 0.65, 0.71, 0.75, 0.80]`. Shadow top 0.45.
`_decide_active(0x_at_0.31)` : rank 10/10 = `MAX_ACTIVE_TRADERS=10`.
Out-of-top-N (rank index ≥ 10 - 1 = 9, so `wallet_rank=9` is the
worst still in cap). On demote uniquement si `rank >= MAX_ACTIVE_TRADERS`
(strict, donc rank 10+) OU si `rank == MAX_ACTIVE_TRADERS - 1` (worst,
inclusivement) et un shadow ≥ score+margin attend. Cf. décision **D9**
infra.

### 3.6 Story F — Recap ops user

User regarde dashboard `/home` 30j post-merge M15 :

- `Promotions / Demotions sur 14j` : **8 / 6** (vs 0 / 0 pré-M15).
- `Auto-blacklist sur 14j` : **2** (un PnL-threshold, un win-rate-floor).
- `Probation actif` : **3 wallets** (sized 0.25×).
- `Coverage internal_pnl_score` : **52 / 65** wallets ACTIVE+SHADOW
  (cold-start cleared après 30j).
- `Arbitrage bot rejected (last 30d)` : **4** (rare comme attendu pour
  notre pool — 4/65 = 6%).

Aucune intervention manuelle nécessaire. User confirme le bot tourne
sain.

---

## 4. Architecture

### 4.1 Diagramme bundle MB.1 → MB.8

```
                          ┌─────────────────────────────────────────────────┐
                          │  DiscoveryOrchestrator._run_one_cycle (M5+M12+M14)│
                          └────┬────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  MetricsCollectorV2.collect(wallet)                       (MB.1+MB.7)│
│  ──────────────────────                                              │
│  - existing M14 metrics (Sortino, Brier P(YES), HHI, …)              │
│  - NEW : _compute_internal_pnl_score(wallet)             (MB.1)      │
│      → query my_positions WHERE source_wallet = :w + simulated      │
│      → sigmoid(SUM(realized_pnl) / scale_usd)                        │
│      → None if count < SCORING_INTERNAL_MIN_POSITIONS                │
│  - NEW : _compute_net_exposure_ratio(wallet)             (MB.7)      │
│      → fetch /positions (already cached M14)                         │
│      → mean(|YES_net - NO_net| / gross over cond_ids)                │
│  → TraderMetricsV2 enriched (internal_pnl_score, net_exposure_ratio) │
└──────┬────────────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  gates.check_all_gates(metrics, wallet, settings)        (MB.7)      │
│  ──────────────────────                                              │
│  Sequence (fail-fast) :                                              │
│  1. not_blacklisted                                                  │
│  2. not_wash_cluster                                                 │
│  3. days_active_min       (cold_start mode 7 if probation)           │
│  4. trade_count_min       (cold_start mode 20 OR probation 10)       │
│  5. cash_pnl_positive                                                │
│  6. zombie_ratio_max                                                 │
│  7. NEW : not_arbitrage_bot   (net_exposure_ratio >= 0.10)           │
│                                                                      │
│  Wallet rejected → trader_events.event_type="gate_rejected"          │
│                    event_metadata.reason_code = ...                   │
└──────┬────────────────────────────────────────────────────────────────┘
       │ pass all gates
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  compute_score_v2_1_1(metrics, pool_context)              (MB.2)     │
│  ──────────────────────                                              │
│  raw = (                                                             │
│    risk_adjusted, calibration, timing_alpha, specialization,         │
│    consistency, discipline                                           │
│    + NEW internal_pnl = compute_internal_pnl(metrics)                │
│  )                                                                   │
│  IF metrics.internal_pnl_score is None:                              │
│      # Cold-start branch — renormalize to 5 factors locally          │
│      final = 0.3125·rank(risk_adjusted) + 0.25·rank(calibration)     │
│            + 0.1875·rank(specialization) + 0.125·rank(consistency)   │
│            + 0.125·rank(discipline)     # M14 v2.1 weights restored  │
│  ELSE:                                                               │
│      final = 0.25·rank(risk_adjusted) + 0.20·rank(calibration)       │
│            + 0.15·rank(specialization) + 0.075·rank(consistency)     │
│            + 0.075·rank(discipline) + 0.25·rank(internal_pnl)        │
│  scoring_version = "v2.1.1"                                          │
└──────┬────────────────────────────────────────────────────────────────┘
       │ score
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  DecisionEngine.decide(scoring, current, active_count)   (MB.3+MB.6+MB.8)│
│  ──────────────────────                                              │
│  - shadow / blacklist / pinned : unchanged paths                     │
│  - absent + score≥promo + 10≤trade_count<50 + days≥7 + gates light:  │
│       insert shadow, is_probation=True              (MB.6)           │
│  - absent + score≥promo + trade_count≥50 + days≥30 + gates full:     │
│       insert shadow, is_probation=False             (M5)             │
│  - shadow → active : promote standard (M5). is_probation flag carried│
│  - active wallet:                                                    │
│       NEW _decide_active ranking-based                (MB.3)         │
│       1. fetch active scores → rank wallet           (FRESH, MB.4)   │
│       2. if rank >= MAX_ACTIVE_TRADERS:              (out-of-top-N)  │
│            increment_low_score; if cycles>=3: demote shadow          │
│       3. ELSE check absolute hard floor:                             │
│            if score < SCORING_ABSOLUTE_HARD_FLOOR=0.30: demote       │
│                                                                      │
│       4. Auto-blacklist branch (MB.8) :                              │
│            cumulative_pnl, observed_wr, observed_count               │
│            ← MyPositionRepository.sum_realized_pnl_by_source_wallet  │
│            if pnl < threshold OR (count≥30 AND wr<0.25):             │
│                transition_status_unsafe(wallet, "blacklisted")       │
│                push Alert("trader_auto_blacklisted")                 │
│                                                                      │
│       5. Probation auto-release (MB.6) :                             │
│            if is_probation AND trade_count≥50 AND days_active≥30:    │
│                set_probation(wallet, on=False)                       │
│                event probation_released                              │
└──────┬────────────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  EvictionScheduler.run_cycle(scores_by_wallet)            (MB.4+MB.5)│
│  ──────────────────────                                              │
│  - existing M5_bis core (cascade, hysteresis, T6/T8)                 │
│  - NEW : classify_sell_only_transitions consumes inputs.scores       │
│    for active_non_pinned in _delta_vs_worst (FRESH, MB.4)            │
│  - boot only : _log_empirical_margin_recommendation       (MB.5)     │
└───────────────────────────────────────────────────────────────────────┘

         ┌───────────────────────────────────────────────────────────┐
         │  Parallel: Strategy pipeline (M2+M13+M16)     (MB.6 hook) │
         │                                                            │
         │  WalletPoller → DetectedTradeDTO with is_source_probation  │
         │                                                            │
         │  PositionSizer._check_buy:                                 │
         │     existing M13 + M16 logic                               │
         │     IF ctx.trade.is_source_probation:                      │
         │         raw_my_size *= 0.25                                │
         └────────────────────────────────────────────────────────────┘
```

### 4.2 Fichiers touchés (récapitulatif)

| Fichier | Type changement | Lignes estimées |
|---|---|---|
| [src/polycopy/storage/models.py](../../src/polycopy/storage/models.py) | `MyPosition.source_wallet_address`, `TargetTrader.is_probation` | +6 / -0 |
| [migrations/versions/0009_*.py](../../migrations/versions/) | Nouvelle migration Alembic | +60 / -0 |
| [src/polycopy/discovery/dtos.py](../../src/polycopy/discovery/dtos.py) (`TraderMetricsV2`) | +`internal_pnl_score: float | None`, +`net_exposure_ratio: float = 1.0` | +3 / -0 |
| [src/polycopy/discovery/scoring/v2/dtos.py](../../src/polycopy/discovery/scoring/v2/dtos.py) | `RawSubscores` + `ScoringNormalizedSubscores` +`internal_pnl: float` | +3 / -0 |
| [src/polycopy/discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py) | `_compute_internal_pnl_score`, `_compute_net_exposure_ratio` + intégration `collect` | +85 / -3 |
| [src/polycopy/discovery/scoring/v2/factors/internal_pnl.py](../../src/polycopy/discovery/scoring/v2/factors/) | NEW factor module pure | +35 / -0 |
| [src/polycopy/discovery/scoring/v2/factors/__init__.py](../../src/polycopy/discovery/scoring/v2/factors/__init__.py) | `from .internal_pnl import compute_internal_pnl` | +1 / -0 |
| [src/polycopy/discovery/scoring/v2/aggregator.py](../../src/polycopy/discovery/scoring/v2/aggregator.py) | Nouvelles pondérations + cold-start branch + bump version | +60 / -10 |
| [src/polycopy/discovery/scoring/v2/gates.py](../../src/polycopy/discovery/scoring/v2/gates.py) | +`check_not_arbitrage_bot` ajouté à `check_all_gates` | +30 / -2 |
| [src/polycopy/discovery/decision_engine.py](../../src/polycopy/discovery/decision_engine.py) | `_decide_active` ranking + auto-blacklist + probation auto-release | +120 / -25 |
| [src/polycopy/discovery/eviction/state_machine.py](../../src/polycopy/discovery/eviction/state_machine.py) | `_delta_vs_worst` consume fresh scores (H-007) | +12 / -8 |
| [src/polycopy/discovery/eviction/scheduler.py](../../src/polycopy/discovery/eviction/scheduler.py) | `_log_empirical_margin_recommendation` boot helper | +35 / -0 |
| [src/polycopy/strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py) | `PositionSizer._check_buy` probation multiplier | +10 / -0 |
| [src/polycopy/storage/dtos.py](../../src/polycopy/storage/dtos.py) | `DetectedTradeDTO` +`is_source_probation: bool = False` | +2 / -0 |
| [src/polycopy/watcher/poller.py](../../src/polycopy/watcher/poller.py) | Enrich DTO with probation flag (1 batch query) | +25 / -3 |
| [src/polycopy/storage/repositories.py](../../src/polycopy/storage/repositories.py) | `MyPositionRepository.sum_realized_pnl_by_source_wallet` + `TargetTraderRepository.set_probation`, `list_active_with_probation` | +60 / -0 |
| [src/polycopy/monitoring/dtos.py](../../src/polycopy/monitoring/dtos.py) | `Alert.event` Literal +`"trader_auto_blacklisted"` | +2 / -0 |
| [src/polycopy/monitoring/templates/trader_auto_blacklisted.md.j2](../../src/polycopy/monitoring/templates/) | NEW template MarkdownV2 | +25 / -0 |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +5 settings + bump literal + cross-field validators | +50 / -5 |
| [.env.example](../../.env.example) | Bloc M15 documenté | +20 / -0 |
| Tests unit | +26 tests ciblés | +700 / -50 |
| Tests intégration | +3 (cold-start E2E, probation E2E, auto-blacklist E2E) | +250 / -0 |
| `scripts/validate_mb_hypotheses.py` | NEW — H-EMP-3 + H-EMP-11 + H-EMP-13 | +180 / -0 |

### 4.3 Dépendances avec autres milestones

- **M5 / M5_bis / M5_ter** : invariants lifecycle préservés. M15 ajoute :
  - `is_probation` flag (M5_bis-style add column).
  - Ranking dans `_decide_active` (remplace threshold absolu mais
    préserve hystérésis 3 cycles + safeguard pinned).
  - Fresh scores propagation H-007 fix (renforce M5_bis cohérence
    audit trail).
- **M11 (latency)** : aucun. M15 ne touche pas la couche WS.
- **M12 (gates v2) + M14 (v2.1)** : registry étendu avec `"v2.1.1"`.
  Append-only strict (audit trail v2 + v2.1 préservés).
- **M13 (dry-run observability)** : M15 réutilise `my_positions.realized_pnl`
  cristallisé par M13 Bug 5 + M8 v2 neg_risk. Pas de nouvelle écriture.
- **M16 (fees)** : `PositionSizer._check_buy` modifié — la probation
  multiplier `*=0.25` arrive **après** le fee check (`ev_negative_after_fees`
  reject prime sur probation). Ordre :
  ```
  1. position_already_open check (M13)
  2. raw_size + cap_size (M2)
  3. M16 fees + EV check
  4. M15 probation multiplier
  5. ctx.my_size = raw_my_size
  ```
- **M16_bis (à venir)** : aucun lien direct. M15 ferme la boucle
  scoring → DecisionEngine; M16 ferme la boucle EV → PositionSizer.

---

## 5. Algorithmes

### 5.1 MB.1 — `_compute_internal_pnl_score(wallet)`

**Fichier** :
[src/polycopy/discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py).

**Contrat** : `async def _compute_internal_pnl_score(wallet: str) → float
| None`. Pure stateful (1 query SQL, pas réseau).

```python
# src/polycopy/discovery/metrics_collector_v2.py — MB.1

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import exp

_INTERNAL_PNL_WINDOW_DAYS: int = 30


async def _compute_internal_pnl_score(
    self,
    wallet_address: str,
) -> float | None:
    """Score sigmoid sur la PnL réalisée par polycopy depuis qu'il copie
    `wallet_address`.

    Source de vérité : `my_positions.realized_pnl` cristallisé par
    M13 Bug 5 (SELL copié) ou M8 v2 neg_risk resolution. Le scope du
    sigmoid est paramétré par `SCORING_INTERNAL_PNL_SCALE_USD` (default
    10.0 — soit +$10/30j ↔ score 0.73).

    Filtre `simulated` selon `execution_mode` (cf. décision D8) :

    - `live` : `simulated=False` strict (prod copy data).
    - `dry_run` : `simulated=True` strict (virtual copy data).
    - `simulation` : `simulated=True` (offline backtest, équivalent
      dry-run sémantiquement).

    Cold-start : si `count < SCORING_INTERNAL_MIN_POSITIONS=10` →
    retourne `None`. L'aggregator (MB.2) traite ce cas via la branche
    cold-start (renormalize à 5 facteurs).

    Pure async — 1 query SQL, pas de réseau, pas de side-effect DB.
    """
    cutoff = datetime.now(tz=UTC) - timedelta(days=_INTERNAL_PNL_WINDOW_DAYS)
    simulated_flag = self._settings.execution_mode != "live"

    pnl_sum, count = await self._my_positions_repo.sum_realized_pnl_by_source_wallet(
        wallet_address=wallet_address.lower(),
        since=cutoff,
        simulated=simulated_flag,
    )
    if count < self._settings.scoring_internal_min_positions:
        return None

    scale_usd = float(self._settings.scoring_internal_pnl_scale_usd)
    if scale_usd <= 0.0:  # défense en profondeur
        scale_usd = 10.0
    x = float(pnl_sum) / scale_usd
    # sigmoid(x) = 1 / (1 + exp(-x)). x positif borné ~ [-50, 50] avant
    # saturation flottante. Notre scale 10.0 + pnl ±$500 max attendu
    # → x ∈ [-50, 50] sûr.
    return 1.0 / (1.0 + exp(-max(-50.0, min(50.0, x))))
```

**Repository helper** :

```python
# src/polycopy/storage/repositories.py — MB.1 (MyPositionRepository)

async def sum_realized_pnl_by_source_wallet(
    self,
    *,
    wallet_address: str,
    since: datetime,
    simulated: bool,
) -> tuple[float, int]:
    """Retourne (signed_pnl_sum, count) sur fenêtre `since`.

    Filtre :
    - `MyPosition.source_wallet_address == wallet_address.lower()`
    - `MyPosition.closed_at IS NOT NULL`
    - `MyPosition.closed_at > since`
    - `MyPosition.realized_pnl IS NOT NULL`
    - `MyPosition.simulated == simulated`

    Index composite recommandé migration 0009 :
    `(source_wallet_address, closed_at, simulated)`.
    """
    async with self._sf() as session:
        stmt = (
            select(
                func.coalesce(func.sum(MyPosition.realized_pnl), 0.0),
                func.count(MyPosition.id),
            )
            .where(
                MyPosition.source_wallet_address == wallet_address.lower(),
                MyPosition.closed_at.is_not(None),
                MyPosition.closed_at > since,
                MyPosition.realized_pnl.is_not(None),
                MyPosition.simulated == simulated,
            )
        )
        row = (await session.execute(stmt)).first()
    if row is None:
        return 0.0, 0
    return float(row[0] or 0.0), int(row[1] or 0)
```

### 5.2 MB.2 — Facteur `internal_pnl` dans v2.1.1

**Fichier nouveau** :
[src/polycopy/discovery/scoring/v2/factors/internal_pnl.py](../../src/polycopy/discovery/scoring/v2/factors/).

```python
# src/polycopy/discovery/scoring/v2/factors/internal_pnl.py — MB.2

"""Factor `internal_pnl` (M15 MB.2).

Lit `metrics.internal_pnl_score` calculé par
:meth:`MetricsCollectorV2._compute_internal_pnl_score`. Score sigmoid
sur PnL réalisé par polycopy depuis qu'il copie le wallet.

Cold-start : si `internal_pnl_score is None` (count < 10 positions
copiées closed) → retourne **None**. L'aggregator traite ce cas via
la branche cold-start (renormalisation locale à 5 facteurs).

Pure function. Pas de pool context — score déjà clipped [0, 1] par le
sigmoid en amont.
"""

from __future__ import annotations

from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


def compute_internal_pnl(metrics: TraderMetricsV2) -> float | None:
    """Lit `metrics.internal_pnl_score`. Pas de calcul ici (déjà fait
    par le collector pour éviter le coupling au repository).

    Returns:
      `None` si cold-start (collecteur a retourné None).
      `float ∈ [0, 1]` sinon (sigmoid déjà clippé, défensive clip).
    """
    score = metrics.internal_pnl_score
    if score is None:
        return None
    return max(0.0, min(1.0, float(score)))
```

**Aggregator** :

```python
# src/polycopy/discovery/scoring/v2/aggregator.py — MB.2

# Pondérations v2.1.1 (M15 MB.2 — drop 0.20 sur les 5 v2.1, intronisation
# `internal_pnl=0.25`). Justification §4.1 + §14.1.
#
#   risk_adjusted   : 0.3125 → 0.25      (-0.0625)
#   calibration     : 0.2500 → 0.20      (-0.05)
#   specialization  : 0.1875 → 0.15      (-0.0375)
#   consistency     : 0.1250 → 0.075     (-0.05)
#   discipline      : 0.1250 → 0.075     (-0.05)
#   timing_alpha    : 0.0   (inchangé)
#   internal_pnl    : 0.0   → 0.25       (NEW)
#   ────────────────────────────────────────
#   sum             : 1.0    1.0
#
# Cold-start (internal_pnl=None) : on revient aux poids v2.1 strict
# (somme=1.0 sur 5 facteurs hérités). Pas de renormalisation magique.
_WEIGHT_RISK_ADJUSTED_V2_1_1: float = 0.25
_WEIGHT_CALIBRATION_V2_1_1: float = 0.20
_WEIGHT_TIMING_ALPHA_V2_1_1: float = 0.0
_WEIGHT_SPECIALIZATION_V2_1_1: float = 0.15
_WEIGHT_CONSISTENCY_V2_1_1: float = 0.075
_WEIGHT_DISCIPLINE_V2_1_1: float = 0.075
_WEIGHT_INTERNAL_PNL_V2_1_1: float = 0.25

# Cold-start : poids v2.1 (M14) restaurés sur 5 facteurs (somme=1.0).
_WEIGHT_RISK_ADJUSTED_COLD: float = 0.3125
_WEIGHT_CALIBRATION_COLD: float = 0.2500
_WEIGHT_TIMING_ALPHA_COLD: float = 0.0
_WEIGHT_SPECIALIZATION_COLD: float = 0.1875
_WEIGHT_CONSISTENCY_COLD: float = 0.1250
_WEIGHT_DISCIPLINE_COLD: float = 0.1250

# Validation au load (raise ImportError si modif casse l'invariant).
_v2_1_1_sum = (
    _WEIGHT_RISK_ADJUSTED_V2_1_1 + _WEIGHT_CALIBRATION_V2_1_1
    + _WEIGHT_TIMING_ALPHA_V2_1_1 + _WEIGHT_SPECIALIZATION_V2_1_1
    + _WEIGHT_CONSISTENCY_V2_1_1 + _WEIGHT_DISCIPLINE_V2_1_1
    + _WEIGHT_INTERNAL_PNL_V2_1_1
)
_cold_sum = (
    _WEIGHT_RISK_ADJUSTED_COLD + _WEIGHT_CALIBRATION_COLD
    + _WEIGHT_TIMING_ALPHA_COLD + _WEIGHT_SPECIALIZATION_COLD
    + _WEIGHT_CONSISTENCY_COLD + _WEIGHT_DISCIPLINE_COLD
)
if abs(_v2_1_1_sum - 1.0) > 1e-6 or abs(_cold_sum - 1.0) > 1e-6:
    raise ImportError(
        f"Pondérations scoring v2.1.1 ne somment pas à 1.0 : "
        f"v2.1.1={_v2_1_1_sum}, cold-start={_cold_sum}"
    )


def compute_score_v2_1_1(
    metrics: TraderMetricsV2,
    pool_context: PoolContext,
) -> ScoreV2Breakdown:
    """Aggregator v2.1.1 (M15 MB.2). Diff vs v2.1 :

    - +1 facteur `internal_pnl` poids 0.25.
    - 5 autres facteurs renormalisés (-0.0625 / -0.05 / -0.0375 / -0.05 /
      -0.05).
    - Cold-start branch : si `metrics.internal_pnl_score is None` →
      score calculé sur 5 facteurs avec poids v2.1 restaurés (somme=1.0).
    - `scoring_version="v2.1.1"`.
    """
    raw_internal_pnl = compute_internal_pnl(metrics)
    cold_start = raw_internal_pnl is None

    raw = RawSubscores(
        risk_adjusted=compute_risk_adjusted(metrics),
        calibration=compute_calibration(metrics, pool_context.brier_baseline_pool),
        timing_alpha=compute_timing_alpha(metrics),
        specialization=compute_specialization(metrics),
        consistency=compute_consistency(metrics),
        discipline=compute_discipline(metrics),
        internal_pnl=raw_internal_pnl if not cold_start else 0.0,
    )

    normalized = ScoringNormalizedSubscores(
        risk_adjusted=rank_normalize_one(raw.risk_adjusted, pool_context.risk_adjusted_pool),
        calibration=rank_normalize_one(raw.calibration, pool_context.calibration_pool),
        timing_alpha=rank_normalize_one(raw.timing_alpha, pool_context.timing_alpha_pool),
        specialization=rank_normalize_one(raw.specialization, pool_context.specialization_pool),
        consistency=rank_normalize_one(raw.consistency, pool_context.consistency_pool),
        discipline=rank_normalize_one(raw.discipline, pool_context.discipline_pool),
        internal_pnl=(
            rank_normalize_one(raw_internal_pnl, pool_context.internal_pnl_pool)
            if not cold_start else 0.0
        ),
    )

    if cold_start:
        final = (
            _WEIGHT_RISK_ADJUSTED_COLD * normalized.risk_adjusted
            + _WEIGHT_CALIBRATION_COLD * normalized.calibration
            + _WEIGHT_TIMING_ALPHA_COLD * normalized.timing_alpha
            + _WEIGHT_SPECIALIZATION_COLD * normalized.specialization
            + _WEIGHT_CONSISTENCY_COLD * normalized.consistency
            + _WEIGHT_DISCIPLINE_COLD * normalized.discipline
        )
    else:
        final = (
            _WEIGHT_RISK_ADJUSTED_V2_1_1 * normalized.risk_adjusted
            + _WEIGHT_CALIBRATION_V2_1_1 * normalized.calibration
            + _WEIGHT_TIMING_ALPHA_V2_1_1 * normalized.timing_alpha
            + _WEIGHT_SPECIALIZATION_V2_1_1 * normalized.specialization
            + _WEIGHT_CONSISTENCY_V2_1_1 * normalized.consistency
            + _WEIGHT_DISCIPLINE_V2_1_1 * normalized.discipline
            + _WEIGHT_INTERNAL_PNL_V2_1_1 * normalized.internal_pnl
        )

    return ScoreV2Breakdown(
        wallet_address=metrics.wallet_address,
        score=max(0.0, min(1.0, final)),
        raw=raw,
        normalized=normalized,
        brier_baseline_pool=pool_context.brier_baseline_pool,
        scoring_version="v2.1.1",
        cold_start_internal_pnl=cold_start,
    )
```

**Pool context extension** : `PoolContext` enrichi avec
`internal_pnl_pool: list[float]` (collecté par
`_build_pool_context` qui itère `metrics.internal_pnl_score` non-None).

### 5.3 MB.3 — Ranking-based activation

**Fichier** :
[src/polycopy/discovery/decision_engine.py:290-339](../../src/polycopy/discovery/decision_engine.py#L290).

```python
# src/polycopy/discovery/decision_engine.py — MB.3 + MB.6 + MB.8

async def _decide_active(
    self,
    current: TargetTrader,
    score: float,
    version: str,
) -> DiscoveryDecision:
    """M15 — ranking-based activation + auto-blacklist + probation release.

    Diff vs M5 :
    1. Remplace `score < SCORING_DEMOTION_THRESHOLD` par `wallet_rank >=
       MAX_ACTIVE_TRADERS` (out-of-top-N parmi les actives).
    2. Garde-fou absolu `score < SCORING_ABSOLUTE_HARD_FLOOR=0.30` toujours
       force-demote.
    3. Auto-blacklist si `cumulative_observed_pnl <
       AUTO_BLACKLIST_PNL_THRESHOLD_USD` OR `(observed_position_count >=
       AUTO_BLACKLIST_MIN_POSITIONS_FOR_WR AND observed_win_rate < 0.25)`.
    4. Probation auto-release si `is_probation AND trade_count_90d >= 50
       AND days_active >= 30`.

    Hystérésis 3 cycles préservée (réutilise `consecutive_low_score_cycles`).
    """
    cfg = self._settings
    wallet = current.wallet_address.lower()

    # Étape 4 — Probation auto-release (avant ranking).
    if current.is_probation:
        await self._maybe_release_probation(current)

    # Étape 3 — Auto-blacklist (MB.8). Court-circuit si tire.
    auto_bl_decision = await self._maybe_auto_blacklist(current, score, version)
    if auto_bl_decision is not None:
        return auto_bl_decision

    # Étape 1 + 2 — Ranking-based + safeguard.
    active_scores = await self._target_repo.list_active_scores()
    # active_scores = list[(wallet_address_lower, score)] — fresh scores
    # snapshot from this cycle (passed via target_repo, M14+ aware).
    sorted_scores = sorted(active_scores, key=lambda r: -r[1])
    wallet_rank = next(
        (i for i, (w, _) in enumerate(sorted_scores) if w == wallet),
        len(sorted_scores),  # not in active set — fall through
    )

    # Safeguard absolute hard floor (cas pathologique pool entièrement < 0.30).
    if score < cfg.scoring_absolute_hard_floor:
        new_count = await self._target_repo.increment_low_score(wallet)
        if new_count >= cfg.scoring_demotion_hysteresis_cycles:
            return await self._do_demote(
                current, score, version, new_count,
                reason=f"score {score:.2f} < absolute_hard_floor "
                       f"{cfg.scoring_absolute_hard_floor}",
            )
        return DiscoveryDecision(
            wallet_address=wallet,
            decision="keep",
            from_status="active",
            to_status="active",
            score_at_event=score,
            scoring_version=version,
            reason=(
                f"under absolute_hard_floor {new_count}/"
                f"{cfg.scoring_demotion_hysteresis_cycles} (score {score:.2f})"
            ),
            event_metadata={"cycles_under_threshold": new_count,
                            "ranking_basis": "absolute_floor"},
        )

    # Out-of-top-N : rank >= MAX_ACTIVE_TRADERS = wallet rank-worst au cap.
    out_of_top_n = wallet_rank >= cfg.max_active_traders
    if not out_of_top_n:
        # Score acceptable + dans top-N : reset hystérésis.
        if current.consecutive_low_score_cycles > 0:
            await self._target_repo.reset_low_score(wallet)
        return DiscoveryDecision(
            wallet_address=wallet,
            decision="keep",
            from_status="active",
            to_status="active",
            score_at_event=score,
            scoring_version=version,
            reason=(
                f"rank {wallet_rank + 1}/{len(sorted_scores)} within "
                f"top-{cfg.max_active_traders} (score {score:.2f})"
            ),
            event_metadata={"wallet_rank": wallet_rank + 1,
                            "ranking_basis": "top_n"},
        )

    # Out-of-top-N → incrément hystérésis.
    new_count = await self._target_repo.increment_low_score(wallet)
    if new_count >= cfg.scoring_demotion_hysteresis_cycles:
        return await self._do_demote(
            current, score, version, new_count,
            reason=(
                f"rank {wallet_rank + 1} > MAX_ACTIVE_TRADERS="
                f"{cfg.max_active_traders} for {new_count} cycles"
            ),
        )
    return DiscoveryDecision(
        wallet_address=wallet,
        decision="keep",
        from_status="active",
        to_status="active",
        score_at_event=score,
        scoring_version=version,
        reason=(
            f"out-of-top-N {new_count}/"
            f"{cfg.scoring_demotion_hysteresis_cycles} (rank "
            f"{wallet_rank + 1}, score {score:.2f})"
        ),
        event_metadata={"cycles_out_of_top_n": new_count,
                        "wallet_rank": wallet_rank + 1,
                        "ranking_basis": "top_n"},
    )
```

**Helpers privés** (`_do_demote`, `_maybe_release_probation`,
`_maybe_auto_blacklist`) — voir §5.6, §5.8.

### 5.4 MB.4 — Fix EvictionScheduler scores stale (audit H-007)

**Fichier** :
[src/polycopy/discovery/eviction/state_machine.py:75-180](../../src/polycopy/discovery/eviction/state_machine.py#L75).

```python
# src/polycopy/discovery/eviction/state_machine.py — MB.4

def classify_sell_only_transitions(
    inputs: StateMachineInputs,
    tracker: HysteresisTracker,
    *,
    blacklist: set[str],
) -> list[EvictionDecision]:
    """[Doc identique M5_bis §4.5 + ajout MB.4]

    M15 MB.4 : `_delta_vs_worst` consomme désormais `inputs.scores`
    pour les active_non_pinned (H-007 audit fix). Avant : `t.score` DB
    stale. Après : `inputs.scores.get(t.wallet_address.lower(), t.score
    or 0.0)` fresh.

    Pas de breaking signature ; le scheduler appelait déjà
    `inputs.scores` avec les scores frais via `run_cycle`.
    """
    decisions: list[EvictionDecision] = []
    traders_by_wallet = {t.wallet_address.lower(): t for t in inputs.traders}
    active_non_pinned = [
        t for t in inputs.traders
        if t.status == "active" and not t.pinned and t.score is not None
    ]

    for sell_only in [t for t in inputs.traders if t.status == "sell_only"]:
        wallet = sell_only.wallet_address.lower()
        if wallet in blacklist:
            continue

        triggering = (
            sell_only.eviction_triggering_wallet.lower()
            if sell_only.eviction_triggering_wallet
            else None
        )
        self_score = inputs.scores.get(wallet, sell_only.score or 0.0)
        triggering_score: float | None = None
        if triggering is not None:
            t_snap = traders_by_wallet.get(triggering)
            triggering_score = (
                inputs.scores.get(triggering, t_snap.score or 0.0)
                if t_snap is not None
                else inputs.scores.get(triggering)
            )

        abort_triggered = False
        abort_in_progress = False
        if triggering is not None and triggering_score is not None:
            delta = triggering_score - self_score
            if delta < inputs.score_margin:
                cycles = tracker.tick(
                    wallet, direction="abort",
                    target_wallet=triggering, current_delta=delta,
                )
                if cycles >= inputs.hysteresis_cycles:
                    decisions.append(
                        EvictionDecision(
                            wallet_address=wallet,
                            transition="abort_to_active",
                            from_status="sell_only",
                            to_status="active",
                            score_at_event=self_score,
                            # MB.4 — fresh scores for delta_vs_worst computation
                            delta_vs_worst_active=_delta_vs_worst_fresh(
                                self_score, active_non_pinned, inputs.scores,
                            ),
                            triggering_wallet=triggering,
                            cycles_observed=cycles,
                            reason_code="abort_delta_below_margin",
                        ),
                    )
                    tracker.reset(wallet)
                    abort_triggered = True
                else:
                    abort_in_progress = True
            else:
                existing = tracker.get(wallet)
                if existing is not None and existing.direction == "abort":
                    tracker.reset(wallet)

        if abort_triggered:
            continue

        if sell_only.open_positions_count == 0 and not abort_in_progress:
            decisions.append(
                EvictionDecision(
                    wallet_address=wallet,
                    transition="complete_to_shadow",
                    from_status="sell_only",
                    to_status="shadow",
                    score_at_event=self_score,
                    reason_code="positions_all_closed",
                ),
            )
            tracker.reset(wallet)

    return decisions


def _delta_vs_worst_fresh(
    self_score: float,
    active_non_pinned: list[TraderSnapshot],
    scores: dict[str, float],
) -> float | None:
    """M15 MB.4 — H-007 fix : utilise `scores` fresh pour identifier le
    worst active_non_pinned, pas `t.score` DB stale.

    Si `scores` ne contient pas un wallet (cas dégradé), fallback sur
    `t.score` snapshot DB (comportement M5_bis pré-MB.4).
    """
    if not active_non_pinned:
        return None
    worst = min(
        active_non_pinned,
        key=lambda t: (
            scores.get(t.wallet_address.lower(), t.score or 0.0),
            t.wallet_address,
        ),
    )
    worst_fresh = scores.get(worst.wallet_address.lower(), worst.score or 0.0)
    return self_score - worst_fresh
```

**Note** : `_delta_vs_worst` (sans `_fresh`) M5_bis original conservé
pour backward-compat tests qui ne passent pas `scores` — déprécié au
docstring.

### 5.5 MB.5 — `_log_empirical_margin_recommendation`

**Fichier** :
[src/polycopy/discovery/eviction/scheduler.py](../../src/polycopy/discovery/eviction/scheduler.py).

```python
# src/polycopy/discovery/eviction/scheduler.py — MB.5

from datetime import UTC, datetime, timedelta
from statistics import pstdev


async def _log_empirical_margin_recommendation(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mesure l'écart-type empirique des scores ACTIVE sur les 7 derniers
    jours et log la recommandation `eviction_score_margin`.

    Ne modifie **rien** — décision humaine (user tweak `.env`). Appelé
    une fois au boot du `EvictionScheduler` (post `__init__`). No-op si
    aucune row `trader_scores` v2.1+ trouvée (boot frais).

    Hypothèse H-EMP-2 (M14) : variance réduite post-rank-transform → std
    attendue ~0.06. Margin recommandé = 0.06-0.08.
    """
    if not settings.eviction_enabled:
        return  # no-op si M5_bis off
    cutoff = datetime.now(tz=UTC) - timedelta(days=7)
    async with session_factory() as session:
        # Scores ACTIVE des 7 derniers jours, versions v2.1 et v2.1.1.
        stmt = (
            select(TraderScore.score)
            .join(TargetTrader, TraderScore.target_trader_id == TargetTrader.id)
            .where(
                TraderScore.cycle_at > cutoff,
                TraderScore.scoring_version.in_(("v2.1", "v2.1.1")),
                TargetTrader.status == "active",
            )
        )
        scores = list((await session.execute(stmt)).scalars().all())

    if len(scores) < 10:
        log.info(
            "eviction_margin_empirical_recommendation_insufficient_data",
            samples=len(scores),
            current_margin=settings.eviction_score_margin,
        )
        return

    sigma = float(pstdev(scores))
    log.info(
        "eviction_margin_empirical_recommendation",
        samples=len(scores),
        current_margin=settings.eviction_score_margin,
        empirical_1_sigma=round(sigma, 4),
        recommended_min=round(sigma * 0.8, 4),
        recommended_max=round(sigma * 1.2, 4),
        delta_vs_current=round(sigma - settings.eviction_score_margin, 4),
    )
```

Branché dans `EvictionScheduler.__init__` ou via `run_cycle` premier
cycle.

### 5.6 MB.6 — Probation fractional-Kelly

**Étape 1 — DB column** : `target_traders.is_probation: bool` (migration
0009).

**Étape 2 — `DecisionEngine.decide` extension** :

```python
# src/polycopy/discovery/decision_engine.py — MB.6 (extension absent path)

if current is None:
    # ... existing M5/M14 path : promotion threshold check, cap check ...

    # NEW MB.6 : path probation pour wallets 10-50 trades.
    in_probation_window = (
        cfg.probation_min_trades <= scoring.metrics.trade_count_90d < cfg.probation_full_trades
        and scoring.metrics.days_active >= cfg.probation_min_days
    )
    if in_probation_window and not bypass_full_gates:
        # Probation candidate : insert shadow with is_probation=True.
        # Tous les autres gates (cash_pnl_positive, not_blacklisted,
        # not_wash_cluster, not_arbitrage_bot) déjà passés via
        # `gates.check_all_gates` côté orchestrator avant scoring.
        await self._target_repo.insert_shadow(wallet, is_probation=True)
        return DiscoveryDecision(
            wallet_address=wallet,
            decision="discovered_shadow",
            from_status="absent",
            to_status="shadow",
            score_at_event=score,
            scoring_version=version,
            reason=(
                f"probation: {scoring.metrics.trade_count_90d} trades, "
                f"{scoring.metrics.days_active}d active. Sized 0.25× "
                f"until trade_count >= {cfg.probation_full_trades}"
            ),
            event_metadata={"is_probation": True,
                            "trade_count_90d": scoring.metrics.trade_count_90d,
                            "days_active": scoring.metrics.days_active},
        )

    # Else : standard path (insert shadow, promote direct si bypass...).
    # ... rest unchanged ...
```

**Étape 3 — Auto-release** :

```python
async def _maybe_release_probation(self, current: TargetTrader) -> None:
    """Auto-release : `is_probation=False` si gate full satisfait."""
    cfg = self._settings
    metrics = await self._metrics_collector.collect(current.wallet_address)
    if (
        metrics.trade_count_90d >= cfg.probation_full_trades
        and metrics.days_active >= cfg.probation_full_days
    ):
        await self._target_repo.set_probation(current.wallet_address, on=False)
        await self._target_repo.write_event(
            wallet=current.wallet_address,
            event_type="probation_released",
            event_metadata={
                "trade_count_90d": metrics.trade_count_90d,
                "days_active": metrics.days_active,
            },
        )
        log.info(
            "trader_probation_released",
            wallet=current.wallet_address,
            trade_count_90d=metrics.trade_count_90d,
            days_active=metrics.days_active,
        )
```

**Étape 4 — `WalletPoller` enrichit `DetectedTradeDTO`** :

```python
# src/polycopy/watcher/poller.py — MB.6

async def _enrich_with_probation_flags(
    self,
    trades: list[DetectedTradeDTO],
) -> list[DetectedTradeDTO]:
    """1 query batch sur les wallets sources de ces trades — pas N+1."""
    if not trades:
        return trades
    wallets_lc = {t.target_wallet.lower() for t in trades}
    probation_map = await self._target_repo.list_probation_flags(wallets_lc)
    return [
        t.model_copy(update={"is_source_probation": probation_map.get(t.target_wallet.lower(), False)})
        for t in trades
    ]
```

**Étape 5 — `PositionSizer._check_buy` multiplier** :

```python
# src/polycopy/strategy/pipeline.py — MB.6 (extension _check_buy)

# ... après EV check M16 :
ctx.fee_rate = float(effective_fee_rate)
ctx.fee_cost_usd = float(fee_cost)
ctx.ev_after_fee_usd = float(ev_after_fee)

if ev_after_fee < self._settings.strategy_min_ev_usd_after_fee:
    return FilterResult(passed=False, reason="ev_negative_after_fees")

# M15 MB.6 : probation multiplier — appliqué APRÈS le fee/EV check.
# Probation wallet → my_size *= 0.25 (quarter-Kelly).
if ctx.trade.is_source_probation:
    raw_my_size *= self._settings.probation_size_multiplier  # default 0.25

ctx.my_size = raw_my_size
return FilterResult(passed=True)
```

### 5.7 MB.7 — Arbitrage bot filter gate

**Fichier** :
[src/polycopy/discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py).

```python
# src/polycopy/discovery/metrics_collector_v2.py — MB.7

from collections import defaultdict


def _compute_net_exposure_ratio(positions: list[RawPosition]) -> float:
    """Pour chaque condition_id, calcule `|YES_net - NO_net| / gross`.
    Aggrège en moyenne sur les condition_ids 90j.

    - `outcome_index == 0` (BUY YES) : contribue à `yes_net`.
    - `outcome_index == 1` (BUY NO)  : contribue à `no_net`.
    - `outcome_index is None` : skip (DTO legacy ou marché atypique).

    Pour un wallet directional pur :
    - 1 cond, BUY YES size 100 → ratio = 100 / 100 = 1.0
    Pour un arbitrageur YES+NO neutre :
    - 1 cond, BUY YES 100 + BUY NO 100 → ratio = 0 / 200 = 0.0

    Default safe : si aucune position éligible → 1.0 (directional, ne
    déclenche pas le gate).

    Pure function. Testable isolément.
    """
    by_cond: dict[str, dict[str, float]] = defaultdict(lambda: {"yes": 0.0, "no": 0.0})
    for p in positions:
        if p.outcome_index is None:
            continue
        side = "yes" if p.outcome_index == 0 else "no"
        by_cond[p.condition_id][side] += float(p.size)

    if not by_cond:
        return 1.0

    ratios: list[float] = []
    for amounts in by_cond.values():
        gross = abs(amounts["yes"]) + abs(amounts["no"])
        if gross <= 0.0:
            continue
        ratio = abs(amounts["yes"] - amounts["no"]) / gross
        ratios.append(ratio)

    if not ratios:
        return 1.0
    return sum(ratios) / len(ratios)
```

**Gate** :

```python
# src/polycopy/discovery/scoring/v2/gates.py — MB.7

_NET_EXPOSURE_MIN: float = 0.10  # arbitrage bot threshold


def check_not_arbitrage_bot(metrics: TraderMetricsV2) -> GateResult:
    """Gate 7 : `net_exposure_ratio >= 0.10`. Anti-arbitrage-bot YES+NO.

    Détecte les wallets qui passent les autres gates (positive PnL,
    high trade count, active, non-zombie, non-wash) **mais** sont des
    arbitrageurs neutres directionellement — leur PnL n'est pas
    transférable à un copy-trader (Claude §9 item 5 + A10, $40M/an
    Dev Genius).
    """
    observed = float(metrics.net_exposure_ratio)
    passed = observed >= _NET_EXPOSURE_MIN
    return GateResult(
        gate_name="not_arbitrage_bot",
        passed=passed,
        observed_value=observed,
        threshold=_NET_EXPOSURE_MIN,
        reason=(
            f"net_exposure_ratio:{observed:.3f} >= {_NET_EXPOSURE_MIN}"
            if passed
            else f"net_exposure_ratio:{observed:.3f} < {_NET_EXPOSURE_MIN} "
                 f"(arbitrage bot pattern)"
        ),
    )


def check_all_gates(
    metrics: TraderMetricsV2,
    wallet: str,
    settings: Settings,
) -> AggregateGateResult:
    """Vérifie les 7 gates (M15 MB.7 +1) en séquence fail-fast.

    Ordre optimisé : env lookups → DTO simple lookups → DTO computed
    lookup (`net_exposure_ratio`) en dernier. Coût moyen d'un fail
    minimal.
    """
    cold_start_mode: bool = getattr(settings, "scoring_v2_cold_start_mode", False)
    checks: list[Callable[[], GateResult]] = [
        lambda: check_not_blacklisted(wallet, settings),
        lambda: check_not_wash_cluster(wallet, settings),
        lambda: check_days_active(metrics, cold_start_mode=cold_start_mode),
        lambda: check_trade_count(metrics, cold_start_mode=cold_start_mode),
        lambda: check_cash_pnl(metrics),
        lambda: check_zombie_ratio(metrics),
        lambda: check_not_arbitrage_bot(metrics),  # MB.7 NEW (last — DTO computed)
    ]
    for check in checks:
        result = check()
        if not result.passed:
            return AggregateGateResult(passed=False, failed_gate=result)
    return AggregateGateResult(passed=True, failed_gate=None)
```

### 5.8 MB.8 — Auto-blacklist + alertes Telegram

**Fichier** :
[src/polycopy/discovery/decision_engine.py](../../src/polycopy/discovery/decision_engine.py).

```python
# src/polycopy/discovery/decision_engine.py — MB.8

async def _maybe_auto_blacklist(
    self,
    current: TargetTrader,
    score: float,
    version: str,
) -> DiscoveryDecision | None:
    """M15 MB.8 — auto-blacklist si critères PnL ou WR violation.

    Critères (OU logique) :
    - `cumulative_observed_pnl < AUTO_BLACKLIST_PNL_THRESHOLD_USD` (default −$5)
    - `(observed_position_count >= AUTO_BLACKLIST_MIN_POSITIONS_FOR_WR) AND
       (observed_win_rate < 0.25)` (default ≥30 positions + WR<25%)

    Idempotence : un wallet déjà `blacklisted` (status DB) ne re-déclenche
    pas. Le cooldown_key Telegram = `f"auto_blacklist_{wallet}"`.

    Returns : DiscoveryDecision si fire, sinon None.
    """
    cfg = self._settings
    wallet = current.wallet_address.lower()

    # Filtre simulated selon execution_mode (cohérent MB.1).
    simulated_flag = cfg.execution_mode != "live"

    pnl_sum, count = await self._my_positions_repo.sum_realized_pnl_by_source_wallet(
        wallet_address=wallet,
        since=datetime.now(tz=UTC) - timedelta(days=30),
        simulated=simulated_flag,
    )
    if count == 0:
        return None  # pas de copy data → pas de signal observable

    # Win rate calc : count(realized_pnl > 0) / (count(>0) + count(<0)).
    # Break-even (=0) exclus du dénominateur (cf. §14.3 piège).
    wins, losses = await self._my_positions_repo.count_wins_losses_by_source_wallet(
        wallet_address=wallet,
        since=datetime.now(tz=UTC) - timedelta(days=30),
        simulated=simulated_flag,
    )
    decided = wins + losses
    observed_wr = (wins / decided) if decided > 0 else None

    fires_pnl = pnl_sum < float(cfg.auto_blacklist_pnl_threshold_usd)
    fires_wr = (
        observed_wr is not None
        and decided >= cfg.auto_blacklist_min_positions_for_wr
        and observed_wr < 0.25
    )

    if not fires_pnl and not fires_wr:
        return None

    reason_code = "pnl_threshold" if fires_pnl else "win_rate_floor"

    # Transition status (unsafe — override pinned safeguard absent ici car
    # un pinned ne peut pas atteindre _decide_active path après _maybe_*
    # hooks ; mais defensive : on use `transition_status_unsafe` cohérent
    # M5_bis blacklist path).
    await self._target_repo.transition_status_unsafe(
        wallet=wallet,
        new_status="blacklisted",
    )
    await self._target_repo.write_event(
        wallet=wallet,
        event_type="auto_blacklisted",
        from_status="active",
        to_status="blacklisted",
        score_at_event=score,
        scoring_version=version,
        reason=f"auto_blacklist:{reason_code}",
        event_metadata={
            "reason_code": reason_code,
            "observed_pnl_30d": round(pnl_sum, 4),
            "observed_position_count_30d": count,
            "observed_decided_count_30d": decided,
            "observed_win_rate_30d": (round(observed_wr, 4) if observed_wr is not None else None),
            "auto_blacklist_pnl_threshold_usd": float(cfg.auto_blacklist_pnl_threshold_usd),
        },
    )

    # Push alert (idempotent via cooldown_key).
    if self._alerts is not None:
        try:
            self._alerts.put_nowait(
                Alert(
                    level="WARNING",
                    event="trader_auto_blacklisted",
                    body={
                        "wallet": wallet,
                        "reason_code": reason_code,
                        "observed_pnl": pnl_sum,
                        "observed_wr": observed_wr,
                        "observed_position_count": count,
                        "score_at_event": score,
                    },
                    cooldown_key=f"auto_blacklist_{wallet}",
                ),
            )
        except asyncio.QueueFull:
            log.warning("alerts_queue_full_dropped",
                        event="trader_auto_blacklisted")

    log.warning(
        "trader_auto_blacklisted",
        wallet=wallet,
        reason_code=reason_code,
        observed_pnl=pnl_sum,
        observed_wr=observed_wr,
        observed_position_count=count,
    )
    return DiscoveryDecision(
        wallet_address=wallet,
        decision="auto_blacklist",
        from_status="active",
        to_status="blacklisted",
        score_at_event=score,
        scoring_version=version,
        reason=f"auto_blacklist:{reason_code}",
        event_metadata={
            "reason_code": reason_code,
            "observed_pnl_30d": pnl_sum,
            "observed_position_count_30d": count,
            "observed_win_rate_30d": observed_wr,
        },
    )
```

**Template Telegram** :

```jinja
{# src/polycopy/monitoring/templates/trader_auto_blacklisted.md.j2 #}
🚫 *Wallet auto-blacklisté*

*Wallet* : `{{ wallet[:10] ~ '…' ~ wallet[-4:] }}`
*Raison* : {% if reason_code == "pnl_threshold" -%}
PnL observé sur 30j *< {{ pnl_threshold | format_usd_tg }}*
{%- else -%}
Win-rate observé *{{ (observed_wr * 100) | round(1) }}%* sur *{{ observed_position_count }}* positions copiées
{%- endif %}

*PnL observé* : *{{ observed_pnl | format_usd_tg }}*
*Score au moment* : {{ score_at_event | round(2) }}
*Status* : `active → blacklisted`

Réversibilité : `BLACKLISTED_WALLETS` env (cf\\. spec M15 §11\\.5)\\.

{% if dashboard_url -%}
[📊 Dashboard]({{ dashboard_url }})
{%- endif %}
```

Escape MarkdownV2 cohérent M7 (caractères `_*[]()~>#+-=|{}.!\` échappés
via `telegram_md_escape`). Le template évite les variables
user-controlled à l'intérieur de blocks Markdown sans escape.

---

## 6. DTOs / signatures

### 6.1 `MyPosition` étendu (MB.1)

```python
# src/polycopy/storage/models.py — MB.1

class MyPosition(Base):
    """[Doc M3+M8 inchangé]

    M15 MB.1 : ajout `source_wallet_address` (wallet polymarket source qui
    a déclenché la copie). Default `None` pour les rows historiques M3..M14.
    Le collecteur internal_pnl filtre `source_wallet_address = :wallet` —
    les rows None sont ignorés (acceptable cold-start, pas de backfill v1).
    """

    __tablename__ = "my_positions"
    # ... champs existants ...
    source_wallet_address: Mapped[str | None] = mapped_column(
        String(42),
        nullable=True,
        index=True,  # NEW M15 — query collecteur 1 wallet par cycle
    )
```

### 6.2 `TargetTrader.is_probation` (MB.6)

```python
# src/polycopy/storage/models.py — MB.6

class TargetTrader(Base):
    # ... champs M5+M5_bis existants ...
    is_probation: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    # M15 MB.6 : True ⟺ wallet copié à 0.25× sizing jusqu'à passage du
    # gate full (`trade_count_90d >= 50 AND days_active >= 30`). Auto-
    # release dans `_maybe_release_probation`. Pas mutuellement exclusif
    # avec pinned (un wallet pinned est jamais en probation par design,
    # mais le code ne contraint pas).
```

### 6.3 `TraderMetricsV2` étendu (MB.1 + MB.7)

```python
# src/polycopy/discovery/scoring/v2/dtos.py — MB.1 + MB.7

class TraderMetricsV2(BaseModel):
    """[Doc M14 inchangé + extension MB.1 / MB.7]"""
    model_config = ConfigDict(frozen=True)
    # ... champs M14 existants ...
    internal_pnl_score: float | None = None  # MB.1 — None = cold-start
    net_exposure_ratio: float = 1.0          # MB.7 — default safe directional
```

### 6.4 `DetectedTradeDTO` enrichi (MB.6)

```python
# src/polycopy/storage/dtos.py — MB.6

class DetectedTradeDTO(BaseModel):
    # ... champs existants M1..M14 ...
    is_source_probation: bool = False  # MB.6 — alimenté par WalletPoller
```

### 6.5 `RawSubscores` + `ScoringNormalizedSubscores` étendus (MB.2)

```python
# src/polycopy/discovery/scoring/v2/dtos.py — MB.2

class RawSubscores(BaseModel):
    model_config = ConfigDict(frozen=True)
    risk_adjusted: float
    calibration: float
    timing_alpha: float
    specialization: float
    consistency: float
    discipline: float
    internal_pnl: float = 0.0  # MB.2 — 0.0 placeholder si cold-start


class ScoringNormalizedSubscores(BaseModel):
    model_config = ConfigDict(frozen=True)
    # ... 6 fields existing ...
    internal_pnl: float = 0.0  # MB.2


class ScoreV2Breakdown(BaseModel):
    model_config = ConfigDict(frozen=True)
    # ... existing fields ...
    cold_start_internal_pnl: bool = False  # MB.2 — True si formule cold-start
```

### 6.6 `Alert.event` Literal étendu (MB.8)

```python
# src/polycopy/monitoring/dtos.py — MB.8

class Alert(BaseModel):
    event: Literal[
        # ... existing values ...
        "trader_auto_blacklisted",  # MB.8 NEW
    ]
```

### 6.7 Repository signatures (MB.1, MB.6, MB.8)

```python
# src/polycopy/storage/repositories.py — M15

class MyPositionRepository:
    async def sum_realized_pnl_by_source_wallet(
        self, *, wallet_address: str, since: datetime, simulated: bool,
    ) -> tuple[float, int]: ...   # MB.1

    async def count_wins_losses_by_source_wallet(
        self, *, wallet_address: str, since: datetime, simulated: bool,
    ) -> tuple[int, int]: ...     # MB.8


class TargetTraderRepository:
    async def insert_shadow(
        self, wallet: str, *, is_probation: bool = False,
    ) -> TargetTrader: ...  # MB.6 — added optional kwarg, default safe

    async def set_probation(self, wallet: str, *, on: bool) -> None: ...  # MB.6

    async def list_probation_flags(
        self, wallets: set[str],
    ) -> dict[str, bool]: ...  # MB.6 — batch lookup for WalletPoller

    async def list_active_scores(self) -> list[tuple[str, float]]: ...  # MB.3
    # NEW : returns (wallet_lc, score) for status='active', score from
    # latest trader_scores row (cycle_at DESC LIMIT 1 per wallet).
```

---

## 7. Settings

Nouvelles env vars + modification de defaults (toutes backward-compat) :

| Variable env | Champ Settings | Default M15 | Default M14 (avant) | Description |
|---|---|---|---|---|
| `SCORING_VERSION` | `Literal["v1","v2","v2.1","v2.1.1"]` | `"v1"` | `"v1"` | Inchangé default. Literal étendu `"v2.1.1"`. |
| `SCORING_INTERNAL_MIN_POSITIONS` | `int [1, 200]` | `10` | (n'existait pas) | Cold-start threshold MB.1. |
| `SCORING_INTERNAL_PNL_SCALE_USD` | `Decimal [0.10, 1000.0]` | `Decimal("10.0")` | (n'existait pas) | Scale sigmoid MB.1 (≈ score 0.73 à +$10/30j). |
| `SCORING_ABSOLUTE_HARD_FLOOR` | `float [0.0, 0.50]` | `0.30` | `0.30` (réutilisé `SCORING_DEMOTION_THRESHOLD`) | Garde-fou MB.3. Renommé pour clarté. |
| `AUTO_BLACKLIST_PNL_THRESHOLD_USD` | `Decimal [-1000, 0]` | `Decimal("-5.0")` | (n'existait pas) | MB.8 critère 1. |
| `AUTO_BLACKLIST_MIN_POSITIONS_FOR_WR` | `int [10, 200]` | `30` | (n'existait pas) | MB.8 critère 2 plancher. |
| `PROBATION_MIN_TRADES` | `int [1, 100]` | `10` | (n'existait pas) | MB.6 entry threshold. |
| `PROBATION_FULL_TRADES` | `int [10, 500]` | `50` | (réutilisé `_TRADE_COUNT_MIN`) | MB.6 release threshold (= gate full M14 strict). |
| `PROBATION_MIN_DAYS` | `int [1, 90]` | `7` | (n'existait pas) | MB.6 entry day-active threshold. |
| `PROBATION_FULL_DAYS` | `int [1, 365]` | `30` | (réutilisé `_DAYS_ACTIVE_MIN`) | MB.6 release day-active threshold. |
| `PROBATION_SIZE_MULTIPLIER` | `Decimal [0.05, 1.0]` | `Decimal("0.25")` | (n'existait pas) | MB.6 quarter-Kelly. |

**`.env.example` mise à jour** :

```dotenv
# --- M15 : Anti-toxic lifecycle + internal PnL feedback ---
# v2.1.1 ajoute le facteur internal_pnl (poids 0.25) qui mesure la PnL
# réalisée par polycopy depuis qu'il copie chaque wallet. Cold-start
# (count<10 positions copiées closed) → fallback sur v2.1 5-factors.
# SCORING_VERSION=v1                            # "v1" / "v2" / "v2.1" / "v2.1.1"
# SCORING_INTERNAL_MIN_POSITIONS=10
# SCORING_INTERNAL_PNL_SCALE_USD=10.0           # +$10/30j ↔ score 0.73

# Garde-fou absolu MB.3 — un wallet sous ce seuil force-demote même
# s'il est dans le top-N (cas pathologique).
# SCORING_ABSOLUTE_HARD_FLOOR=0.30

# Auto-blacklist MB.8 — réversible via BLACKLISTED_WALLETS env modif.
# AUTO_BLACKLIST_PNL_THRESHOLD_USD=-5.0
# AUTO_BLACKLIST_MIN_POSITIONS_FOR_WR=30

# Probation fractional-Kelly MB.6 — wallets 10≤trades<50 sized 0.25×.
# PROBATION_MIN_TRADES=10
# PROBATION_FULL_TRADES=50
# PROBATION_MIN_DAYS=7
# PROBATION_FULL_DAYS=30
# PROBATION_SIZE_MULTIPLIER=0.25
```

**Cross-field validators** :

- Si `SCORING_VERSION=v2.1.1` ET `SCORING_INTERNAL_MIN_POSITIONS > 50` →
  log WARNING au boot ("threshold trop strict — cold-start cleared rare").
- Si `PROBATION_MIN_TRADES >= PROBATION_FULL_TRADES` → raise
  `ValidationError` au boot (config incohérente).
- Si `AUTO_BLACKLIST_PNL_THRESHOLD_USD > 0` → raise (doit être négatif).
- Si `PROBATION_SIZE_MULTIPLIER >= 1.0` → log WARNING ("probation
  multiplier ≥ 1 = pas de protection").

---

## 8. Invariants sécurité

### 8.1 Versioning sacré (append-only) préservé

- Chaque row `trader_scores` porte sa `scoring_version` ∈ `{"v1", "v2",
  "v2.1", "v2.1.1"}`. Aucun `UPDATE` rétroactif M15.
- v2.1 reste accessible via le registry (audit trail intact). Les rows
  historiques `scoring_version="v2.1"` ne sont **jamais** réécrites en
  v2.1.1.
- M15 ajoute une nouvelle entrée registry `_compute_score_v2_1_1_wrapper`
  ; `_compute_score_v2_1_wrapper` (M14) inchangé.
- Test de non-régression : `test_scoring_versions_registry_extends_not_replaces`.

### 8.2 Triple garde-fou M3 + 4ᵉ M8 intacts

Confirmer : aucun fichier executor / direct credentials path touché
par M15.

- ❌ Pas de `ClobWriteClient`, `_persist_realistic_simulated`,
  `_persist_sent_order`, `WalletStateReader`. Le multiplier probation
  vit dans `PositionSizer` côté **strategy** (M2 read-only path).
- ❌ Pas de `RiskManager` modifié — la probation **ne bypasse pas** les
  gates de risque (capital exceeded, drawdown).
- ✅ `MyPosition.source_wallet_address` est en **lecture** côté MB.1 ;
  l'écriture du champ se fait côté Executor M3+M8 dans
  `_persist_sent_order` / `_persist_realistic_simulated` — modification
  uniquement additive (le DTO `OrderApproved` doit transporter
  `source_wallet_address` depuis le pipeline strategy → §14.5 piège).
- ✅ Aucune nouvelle surface POST. Aucun nouveau cred consommé. Data API
  (`/positions` MB.7), Gamma (M14 inchangé), DB locale.

**Spécifique MB.6 sizing probation** : la multiplication `*0.25` arrive
**après** le fee/EV check M16 et **avant** le `RiskManager.check`
exposure check. Donc le RiskManager voit le `my_size` final probation —
pas de bypass capital_exceeded.

### 8.3 Pas de fuite de secret (grep automatisé)

Tous les nouveaux events structlog M15 (`trader_auto_blacklisted`,
`trader_probation_released`, `eviction_margin_empirical_recommendation`,
`gate_rejected` avec `reason_code="arbitrage_bot_pattern"`) ne
contiennent que :

- Wallet adresses (publiques on-chain).
- Scoring values (publiques par construction).
- DB-derived counts / aggregates (publics).

Test à ajouter : `test_no_secret_leak_in_m15_alerts_and_logs` — grep
defensive `POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`,
`CLOB_API_SECRET`, `REMOTE_CONTROL_TOTP_SECRET`, `GOLDSKY_API_KEY` dans :

1. Tous les events loggés sur 1 cycle simulé MB.1+MB.2+MB.3+MB.7+MB.8.
2. Le rendu Telegram MarkdownV2 du template `trader_auto_blacklisted.md.j2`
   (rendu sur fixture wallet/pnl/wr).
3. Le `event_metadata` des `trader_events` rows écrits par M15.

### 8.4 Auto-blacklist discipline réversible

L'auto-blacklist M15 (MB.8) écrit `target_traders.status='blacklisted'`
mais **n'ajoute pas** le wallet à `BLACKLISTED_WALLETS` env var (qui est
read-only côté M5_bis `reconcile_blacklist`). Donc :

- **Au prochain cycle** : `reconcile_blacklist` voit le wallet en DB
  status `blacklisted` mais **absent** de `BLACKLISTED_WALLETS` env →
  applique la transition **T11 (unblacklist)** → restore status
  `shadow` (wallet non-pinned car pinned exclu de l'auto-blacklist par
  la safeguard M5_bis).
- **Pour rendre la blacklist persistante**, l'utilisateur doit ajouter
  manuellement le wallet à `BLACKLISTED_WALLETS` env var puis restart.

Cette **fenêtre de réversibilité** est délibérée — on ne veut **pas**
que l'auto-blacklist soit irréversible sans intervention humaine.
Documenté §11.5 + dans le template Telegram (footer "Réversibilité :
BLACKLISTED_WALLETS env").

**Conséquence sécuritaire** : si l'auto-blacklist tire à tort sur un
wallet (faux-positif), l'utilisateur a **24h max** (cycle reconcile)
pour l'observer et décider — pas de "blacklist permanent" silencieux.

**Mais aussi** : pendant le 24h max, le wallet est exclu du polling
(WalletPoller M5_ter filtre `status IN ('active', 'pinned',
'sell_only')`). Aucun trade copié. L'effet protecteur est immédiat.

**Test** : `test_auto_blacklist_reversed_by_reconcile_when_not_in_env`
— fire auto-blacklist, lancer reconcile_blacklist sans env update,
assert status repassé `shadow` (avec event `unblacklist`).

### 8.5 Gates durs M12 + M14 préservés

MB.7 ajoute le gate `not_arbitrage_bot` mais **ne supprime aucun gate
existant** :

- `cash_pnl_90d > 0`
- `trade_count_90d ≥ 50` (cold-start mode 20)
- `days_active ≥ 30` (cold-start mode 7)
- `zombie_ratio < 0.40`
- `not in BLACKLISTED_WALLETS`
- `not in WASH_CLUSTER_WALLETS`
- **NEW** : `net_exposure_ratio ≥ 0.10` (anti-arbitrage)

**Probation MB.6 relax les 2 gates trade_count/days** mais conserve
strict les 4 autres :

- En probation : `trade_count_90d ≥ 10` (au lieu 50), `days_active ≥ 7`
  (au lieu 30).
- **Ne relax pas** : `cash_pnl_positive`, `not_blacklisted`,
  `not_wash_cluster`, `not_arbitrage_bot`, `zombie_ratio_max`.

Test : `test_probation_gate_relaxes_only_trade_count_and_days_active`.

### 8.6 Blacklist double-check M5_ter préservé

`DecisionEngine.decide` : la branche `if wallet in
cfg.blacklisted_wallets` reste **première** (avant probation, avant
auto-blacklist). Skip strict.

`list_wallets_to_poll` (M5_ter watcher) : double-check Python-side sur
`BLACKLISTED_WALLETS` env conserve. M15 n'introduit pas de nouvelle
fenêtre où un wallet env-blacklist serait polled.

### 8.7 Aucune nouvelle surface de credentials

- M15 reste 100% read-only côté API publique : Data API `/positions`
  (MB.7 net_exposure), Data API `/activity` (M14 inchangé), Gamma
  (M14 inchangé). DB locale en lecture/écriture (writes : status
  transition, events, probation flag).
- Aucune creds CLOB L1/L2 touchée. Aucun nouvel endpoint POST.
- Le seul write nouveau côté `my_positions` est `source_wallet_address`
  — fait par l'**Executor** (M3+M8) via paths existants `_persist_*`.
  M15 ajoute juste le champ DB et la propagation `OrderApproved.source_wallet_address`
  depuis le pipeline (lecture du DTO trade).

---

## 9. Test plan

Total : **26 tests unit** + **3 tests intégration**.

### 9.1 MB.1 — Collecteur `internal_pnl_score` (4 tests)

Dans `tests/unit/test_metrics_collector_v2.py` (étendre).

1. **`test_internal_pnl_score_sigmoid_bounds`**
   - Preconditions : seed 12 `MyPosition` avec
     `source_wallet_address="0xWA"`, `realized_pnl` summed `+$50`.
   - Action : `_compute_internal_pnl_score("0xWA")` avec scale=10.0.
   - Assertion : `score == sigmoid(5.0) ≈ 0.9933` (±1e-6).

2. **`test_internal_pnl_score_returns_none_under_min_positions`**
   - Preconditions : 9 closed positions (sous le seuil 10).
   - Action : idem.
   - Assertion : `score is None`.

3. **`test_internal_pnl_score_dry_run_vs_live_mode_isolation`**
   - Preconditions : 12 simulated (dry_run) + 12 real (live), même
     wallet source.
   - Action : `execution_mode=dry_run` vs `live` → 2 calls.
   - Assertion : pnl scores diffèrent (filtre `simulated` correct).

4. **`test_internal_pnl_score_30d_window_correct`**
   - Preconditions : 5 closed avec `closed_at = now-15d`, 8 closed
     `closed_at = now-45d`. `count = 13` total mais filtre 30j ne garde
     que les 5 récents → `count=5 < 10` → None.
   - Action : `_compute_internal_pnl_score`.
   - Assertion : `score is None` (sous le seuil après filtrage).

### 9.2 MB.2 — Facteur + aggregator v2.1.1 (4 tests)

Dans `tests/unit/test_scoring_v2_aggregator.py` (étendre).

5. **`test_aggregator_v2_1_1_weights_sum_to_one`**
   - Action : assert `_WEIGHT_RISK_ADJUSTED_V2_1_1 + ... +
     _WEIGHT_INTERNAL_PNL_V2_1_1 == 1.0` (±1e-6) ET cold-start sum ==
     1.0.
   - Assertion : passe (validé au load par `raise ImportError`).

6. **`test_aggregator_cold_start_renormalizes_without_internal_pnl`**
   - Preconditions : `metrics.internal_pnl_score = None`. 5 autres
     factors set à `0.5` rank-normalized.
   - Action : `compute_score_v2_1_1(metrics, pool_ctx)`.
   - Assertion : `breakdown.cold_start_internal_pnl is True`,
     `breakdown.score == sum(0.3125+0.25+0.1875+0.125+0.125) × 0.5 ==
     0.5` (= 5 facteurs renormalisés à v2.1 weights).

7. **`test_aggregator_v2_1_1_responds_to_internal_pnl_change`**
   - Preconditions : 2 metrics identiques sauf `internal_pnl_score`
     (0.2 vs 0.8). Pool fixed.
   - Action : compute scores.
   - Assertion : `score(internal=0.8) > score(internal=0.2)` (delta ≈
     `0.25 × Δrank` réaliste).

8. **`test_compute_internal_pnl_clips_to_unit_interval`**
   - Preconditions : `metrics.internal_pnl_score = 1.5` (anormal).
   - Action : `compute_internal_pnl(metrics)`.
   - Assertion : `1.0` (defensive clip).

### 9.3 MB.3 — Ranking-based activation (5 tests)

Dans `tests/unit/test_decision_engine.py` (étendre).

9. **`test_decide_active_ranking_based_demotes_out_of_top_n`**
   - Preconditions : pool active 10 wallets, scores 0.31..0.80, le
     wallet courant `0x_at_0.31` rank 10/10. `MAX_ACTIVE_TRADERS=10`.
     hystérésis 3 cycles.
   - Action : `_decide_active(0x_at_0.31, 0.31, "v2.1.1")` × 3 cycles.
   - Assertion : T+0 keep + cycles=1, T+1 keep + cycles=2, T+2 demote
     to shadow + `ranking_basis="top_n"`.

10. **`test_decide_active_hysteresis_preserved`**
    - Preconditions : T+0 wallet rank 11/10 (out). T+1 cycle suivant
      le wallet remonte rank 5/10 (in).
    - Action : `_decide_active` × 2.
    - Assertion : T+1 reset hystérésis (`consecutive_low_score_cycles=0`).

11. **`test_decide_active_absolute_threshold_safeguard_still_fires`**
    - Preconditions : pool 10 wallets, scores `[0.05, 0.10, ..., 0.30]`
      tous bas (cas pathologique). `0x_at_0.20` rank 5/10 (in top-N).
      Mais `score=0.20 < SCORING_ABSOLUTE_HARD_FLOOR=0.30`.
    - Action : `_decide_active` × 3 cycles.
    - Assertion : demote shadow avec `ranking_basis="absolute_floor"`.

12. **`test_decide_active_pinned_never_demoted`**
    - Preconditions : pinned wallet `0xPIN`, score 0.05 (très bas).
    - Action : `_decide_active(pinned, 0.05, "v2.1.1")` × 10 cycles.
    - Assertion : toutes les itérations retournent `keep` avec
      `from_status="pinned"`. Pas d'incrément hystérésis.

13. **`test_decide_active_ranking_stable_with_pool_change`** —
    régression test H-007 partielle.
    - Preconditions : pool active 5 wallets `[0.40, 0.50, 0.55, 0.60,
      0.65]`. Cap 10. Personne out-of-top-N. Cycle suivant 1 nouveau
      wallet ajouté (pool 6/10), tous les autres scores stables.
    - Action : `_decide_active(0x_at_0.40)` cycle T0 puis T1.
    - Assertion : aucun demote car le pool est sub-cap. (Test négatif
      protection contre ranking hyperactif.)

### 9.4 MB.4 — Fix EvictionScheduler scores stale (2 tests)

Dans `tests/unit/test_eviction_state_machine.py` (étendre).

14. **`test_classify_sell_only_uses_fresh_scores_for_worst_active`**
    - Preconditions : pool 1 sell_only `0xSO`, 3 active non-pinned
      `[0xA1, 0xA2, 0xA3]` avec snapshot DB scores `[0.30, 0.45, 0.50]`.
      `inputs.scores` (fresh) `{0xA1: 0.55, 0xA2: 0.45, 0xA3: 0.50,
      0xSO: 0.40, triggering: 0.42}`.
    - Action : `classify_sell_only_transitions`.
    - Assertion : `delta_vs_worst_active = 0.40 - 0.45 = -0.05` (worst
      fresh = 0xA2 à 0.45, pas 0xA1 à 0.30 stale).

15. **`test_eviction_no_stale_score_dependency_regression`**
    - Preconditions : pool stable 2 cycles consécutifs ; entre cycles
      les scores DB ne sont **pas** mis à jour (cas dégradé) — seul
      `inputs.scores` porte les fresh.
    - Action : run cycle 1 → cycle 2.
    - Assertion : audit trail `EvictionDecision.delta_vs_worst_active`
      cohérent entre les 2 cycles (pas de drift dû à stale).

### 9.5 MB.5 — Empirical margin recommendation (1 test)

Dans `tests/unit/test_eviction_scheduler.py` (étendre).

16. **`test_log_empirical_margin_recommendation_with_fixture_pool`**
    - Preconditions : seed 50 `trader_scores` rows
      `scoring_version="v2.1"` cycle_at récent + 8 wallets ACTIVE.
      Scores variant `[0.40 .. 0.70]` (σ ≈ 0.09).
    - Action : `_log_empirical_margin_recommendation(settings, sf)`
      avec capture `caplog`.
    - Assertion : event `eviction_margin_empirical_recommendation`
      logged avec `samples=50`, `empirical_1_sigma ≈ 0.09 ± 0.02`,
      `recommended_min ≈ 0.072`, `recommended_max ≈ 0.108`.

### 9.6 MB.6 — Probation fractional-Kelly (4 tests)

Dans `tests/unit/test_strategy_pipeline.py` + nouveau
`tests/unit/test_probation_lifecycle.py`.

17. **`test_probation_wallet_sized_quarter_kelly`**
    - Preconditions : `DetectedTradeDTO(is_source_probation=True,
      side="BUY", size=100, price=0.50)`. `copy_ratio=0.01`,
      `max_position_usd=200`. `STRATEGY_FEES_AWARE_ENABLED=False`
      (skip M16).
    - Action : `PositionSizer._check_buy(ctx)`.
    - Assertion : `ctx.my_size == 100 × 0.01 × 0.25 == 0.25` (cap_size
      OK 200/0.50=400 > 0.25).

18. **`test_probation_does_not_bypass_blacklist_or_arbitrage_gates`**
    - Preconditions : wallet probation candidat avec `cash_pnl_90d=
      +$10`, `trade_count_90d=15`, `days_active=8` mais
      `net_exposure_ratio=0.05` (arbitrage bot).
    - Action : `gates.check_all_gates(metrics, wallet, settings)`.
    - Assertion : `passed=False, failed_gate.gate_name=
      "not_arbitrage_bot"`. Probation candidat **rejeté** au gate.

19. **`test_probation_released_at_50_trades`**
    - Preconditions : wallet ACTIVE `is_probation=True`,
      `trade_count_90d=51`, `days_active=32`.
    - Action : `DecisionEngine._maybe_release_probation(current)`.
    - Assertion : `set_probation(wallet, on=False)` appelé,
      `trader_events` event `probation_released` écrit.

20. **`test_walletpoller_enriches_dto_with_probation_flag`**
    - Preconditions : 2 wallets en DB, 1 avec `is_probation=True`. 5
      trades détectés (3 du probation, 2 du standard).
    - Action : `WalletPoller._enrich_with_probation_flags(trades)`.
    - Assertion : 3 trades ont `is_source_probation=True`, 2 False.

### 9.7 MB.7 — Arbitrage bot filter (4 tests)

Dans `tests/unit/test_metrics_collector_v2.py` + `test_scoring_v2_gates.py`.

21. **`test_compute_net_exposure_ratio_arbitrage_bot_under_threshold`**
    - Preconditions : 1 cond_id avec `outcome_index=0 size=100` +
      `outcome_index=1 size=100`.
    - Action : `_compute_net_exposure_ratio(positions)`.
    - Assertion : `ratio == 0.0` (parfait neutre).

22. **`test_compute_net_exposure_ratio_directional_trader_above_threshold`**
    - Preconditions : 3 cond_ids, BUY YES uniquement (`outcome_index=0`
      size 50/100/200).
    - Action : idem.
    - Assertion : `ratio == 1.0` (purement directional).

23. **`test_compute_net_exposure_ratio_mixed_partial`**
    - Preconditions : cond1 YES 100 + NO 30 (ratio=70/130=0.538), cond2
      YES 100 + NO 100 (ratio=0).
    - Action : idem.
    - Assertion : `avg_ratio = (0.538 + 0.0) / 2 == 0.269`.

24. **`test_arbitrage_gate_writes_trader_event`**
    - Preconditions : metrics avec `net_exposure_ratio=0.05`.
      `gates.check_all_gates` rejette.
    - Action : orchestrator pipeline (mock) écrit l'event.
    - Assertion : `trader_events` row event_type="gate_rejected" avec
      event_metadata `{reason_code: "arbitrage_bot_pattern",
      net_exposure_ratio: 0.05}`.

### 9.8 MB.8 — Auto-blacklist + Telegram template (3 tests)

Dans `tests/unit/test_decision_engine.py` + `test_telegram_templates.py`.

25. **`test_auto_blacklist_fires_on_pnl_threshold`**
    - Preconditions : seed `MyPosition` rows pour `0xTOX` avec
      cumulative `realized_pnl = -$8.50` sur 30j (count=15).
      `auto_blacklist_pnl_threshold_usd=-5.00`.
    - Action : `DecisionEngine._maybe_auto_blacklist(current,
      score=0.55, version="v2.1.1")`.
    - Assertion : `decision="auto_blacklist"`, transition `active →
      blacklisted` appliquée, alert `trader_auto_blacklisted` poussée
      avec `body.reason_code="pnl_threshold"`.

26. **`test_auto_blacklist_fires_on_win_rate_floor_with_min_positions`**
    - Preconditions : `0xTOX` avec 35 positions closed
      (`wins=6, losses=29` → wr=0.171). PnL cumulative=-$1 (sous
      threshold pnl=−$5 NON atteint, mais wr critère fire).
    - Action : idem.
    - Assertion : fire avec `reason_code="win_rate_floor"`.

27. **`test_auto_blacklist_idempotent_no_duplicate_alert`**
    - Preconditions : wallet déjà `status="blacklisted"`. `_maybe_*`
      called.
    - Action : idem 2× consécutives.
    - Assertion : `transition_status_unsafe` non rappelé (déjà
      blacklisted), alert non re-poussée (cooldown_key match).

28. **`test_telegram_template_auto_blacklisted_renders_safely`**
    - Preconditions : render `trader_auto_blacklisted.md.j2` avec
      `wallet="0xABC...XYZ"`, `reason_code="pnl_threshold"`,
      `observed_pnl=-8.50`, `pnl_threshold=-5.00`,
      `score_at_event=0.55`, `dashboard_url="http://...".
    - Action : Jinja2 render via `TelegramRenderer`.
    - Assertion : output ne contient aucun caractère MarkdownV2
      non-échappé non escapé. Output ne contient aucun secret marker
      (POLYMARKET_PRIVATE_KEY etc.).

### 9.9 Tests d'intégration (3 tests, opt-in `pytest -m integration`)

Dans `tests/integration/test_m15_e2e.py` (nouveau).

29. **`test_internal_pnl_cold_start_to_active_e2e`** (offline avec DB
    fixture).
    - Preconditions : DB seed pour wallet `0xCOLD` avec 0 closed
      positions M3..M14. Lancer 3 cycles discovery + écrire 12
      `MyPosition` simulated closed entre cycles 1-2 (via M13 helpers).
    - Action : run discovery cycles via `DiscoveryOrchestrator`.
    - Assertion : cycle 1 score v2.1.1 cold-start branch,
      `internal_pnl_score=None`. Cycle 2 (12 positions) score full
      v2.1.1, `internal_pnl_score is not None`. Verify
      `trader_scores.metrics_snapshot["internal_pnl_score"]`.

30. **`test_probation_full_lifecycle_e2e`** (offline).
    - Preconditions : DB seed candidat avec `trade_count_90d=20`,
      `days_active=10`, gates passants.
    - Action : run discovery cycle (insert shadow probation), simuler
      `TRADER_SHADOW_DAYS=0` + `DISCOVERY_SHADOW_BYPASS=true` boot
      pour promotion immédiate, lancer `WalletPoller` + 3 trades
      détectés sur ce wallet, vérifier `my_orders.size`. Plus tard,
      simuler `trade_count_90d=55, days_active=35` → release.
    - Assertion :
      - `target_traders.is_probation=True` post-promotion.
      - `my_orders.size == raw_size × 0.25` sur les 3 ordres.
      - Post-release : `is_probation=False`, `trader_events`
        `probation_released` row.

31. **`test_auto_blacklist_e2e_alert_telegram_renders`** (offline).
    - Preconditions : DB seed `0xTOX` ACTIVE + 15 MyPosition closed
      cumulative −$10. Mock Telegram client.
    - Action : run discovery cycle.
    - Assertion :
      - `target_traders.status="blacklisted"`.
      - Mock Telegram reçu 1 message MarkdownV2 escape strict
        contenant les stats observées.
      - `trader_events` row `auto_blacklisted`.
      - `WalletPoller` au cycle suivant exclut le wallet du polling
        (via `list_wallets_to_poll` filtre).

**Total : 28 unit + 3 intégration = 31 tests**.

---

## 10. Impact sur l'existant

### 10.1 Modules touchés

| Module | Changement | Backwards compat |
|---|---|---|
| [storage/models.py](../../src/polycopy/storage/models.py) | `MyPosition.source_wallet_address` (nullable), `TargetTrader.is_probation` (default False) | Defaults safe — rows existantes None / False. |
| [storage/repositories.py](../../src/polycopy/storage/repositories.py) | +5 méthodes (`sum_realized_pnl_by_source_wallet`, `count_wins_losses_*`, `set_probation`, `list_probation_flags`, `list_active_scores`) | Additif. Aucune signature changée. |
| [discovery/dtos.py](../../src/polycopy/discovery/dtos.py) | `TraderMetricsV2` +2 champs (`internal_pnl_score`, `net_exposure_ratio`) | Defaults `None` / `1.0` — non-cassants. |
| [discovery/scoring/v2/dtos.py](../../src/polycopy/discovery/scoring/v2/dtos.py) | `RawSubscores` + `Normalized` +`internal_pnl`, `ScoreV2Breakdown` +`cold_start_internal_pnl: bool=False` | Defaults safe. |
| [discovery/scoring/v2/factors/](../../src/polycopy/discovery/scoring/v2/factors/) | New `internal_pnl.py` module + `__init__.py` export | Additif. |
| [discovery/scoring/v2/aggregator.py](../../src/polycopy/discovery/scoring/v2/aggregator.py) | Nouvelles pondérations + `compute_score_v2_1_1` + wrapper registry | `compute_score_v2_1` (M14) intact. v2.1 chemin shadow préservé si l'utilisateur ne flip pas vers v2.1.1. |
| [discovery/scoring/v2/gates.py](../../src/polycopy/discovery/scoring/v2/gates.py) | +`check_not_arbitrage_bot` ajouté à `check_all_gates` | Additif fail-fast — wallets qui passaient déjà les 6 gates passeront aussi le 7ᵉ si `net_exposure_ratio ≥ 0.10` (probable cas commun). Tests M12 / M14 doivent être enrichis avec une fixture `net_exposure_ratio=1.0` par défaut. |
| [discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py) | +2 méthodes (`_compute_internal_pnl_score`, `_compute_net_exposure_ratio`) + integration `collect()` | Additif — tests M14 collect() doivent être étendus (1 mock `MyPositionRepository` + reuse fixtures `RawPosition`). |
| [discovery/decision_engine.py](../../src/polycopy/discovery/decision_engine.py) | `_decide_active` refactor ranking + auto-blacklist + probation release. Helpers privés. | **Tests M5+M5_bis existants** (`test_decide_active_*`) doivent être **adaptés** — la sémantique `score < demotion_threshold = demote` change. Documenter dans le commit message. |
| [discovery/eviction/state_machine.py](../../src/polycopy/discovery/eviction/state_machine.py) | `_delta_vs_worst_fresh` consume scores. `_delta_vs_worst` deprecated mais conservé. | Tests M5_bis existants passent (signature inchangée). Nouveaux tests MB.4 ciblés. |
| [discovery/eviction/scheduler.py](../../src/polycopy/discovery/eviction/scheduler.py) | `_log_empirical_margin_recommendation` boot-only. | Aucune dépendance test break. |
| [strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py) | `_check_buy` +`*= probation_size_multiplier` post fee/EV. | Tests M2/M13/M16 PositionSizer doivent être enrichis avec fixture `is_source_probation=False` (default). Comportement préservé. |
| [storage/dtos.py](../../src/polycopy/storage/dtos.py) (`DetectedTradeDTO`) | +`is_source_probation: bool=False` | Default safe. |
| [watcher/poller.py](../../src/polycopy/watcher/poller.py) | `_enrich_with_probation_flags` 1 query batch | Tests M5_ter inchangés (le batch query n'impacte pas la signature poller). |
| [monitoring/dtos.py](../../src/polycopy/monitoring/dtos.py) | `Alert.event` Literal +`"trader_auto_blacklisted"` | Pydantic Literal — additif, aucun test break. |
| [monitoring/templates/](../../src/polycopy/monitoring/templates/) | +1 template Jinja2 | Additif. |
| [config.py](../../src/polycopy/config.py) | +9 settings + literal extension + cross-field validators | Tous defaults non-cassants. |

### 10.2 Changements de valeurs par défaut

- `SCORING_VERSION` reste `"v1"` default. v2.1.1 piloté uniquement si
  l'utilisateur flip explicitement (post-shadow + H-EMP validation).
- `EVICTION_SCORE_MARGIN` inchangé (reste 0.10 M14). MB.5 log empirical
  recommandation, pas auto-tweak.
- Nouveaux defaults M15 (toutes optionnelles, défauts safe — `0` /
  `False` / sentinel cohérents).

### 10.3 Tests M5/M5_bis/M14 existants à adapter (estimation)

À vérifier lors de l'implémentation :

- `tests/unit/test_decision_engine.py::test_decide_active_*` (~4 tests)
  → adapter pour le ranking-based path (la sémantique de `score <
  threshold = demote` n'existe plus telle quelle).
- `tests/unit/test_scoring_v2_aggregator.py::test_compute_score_v2_*`
  (~3 tests) → ajouter fixture `internal_pnl_score=None` ou valeur
  pour vérifier les 2 branches.
- `tests/unit/test_scoring_v2_gates.py::test_check_all_gates_*` (~2-3
  tests) → ajouter fixture `net_exposure_ratio=1.0` (default safe) ou
  valeurs spécifiques.
- `tests/unit/test_metrics_collector_v2.py` (~3-5 tests) → mock
  `MyPositionRepository.sum_realized_pnl_by_source_wallet` + extend
  fixture `RawPosition` avec `outcome_index` populé.
- `tests/unit/test_strategy_pipeline.py::test_position_sizer_*`
  (~3 tests) → ajouter fixture `DetectedTradeDTO(is_source_probation=
  False)` pour rétrocompat ; nouveau test ciblé MB.6 #17 ci-dessus.

Charge totale adaptation : ~12-15 tests existants modifiés.

### 10.4 Impact CLI / boot

- Nouveau log boot via `_log_empirical_margin_recommendation` (info
  level) si `EVICTION_ENABLED=true` ET ≥10 samples.
- Nouveau log boot warnings si cross-field validators trippent (cf.
  §7).
- Pas de modification du runner CLI ni du dashboard structurel.

### 10.5 Impact dashboard

- `/performance` reste fonctionnel — la nouvelle colonne `internal_pnl_score`
  pour visualiser cold-start vs full migre en **MH** (out of scope M15).
- `/scoring` continue à afficher les versions disponibles (v1 vs v2.1
  vs v2.1.1 si l'utilisateur active le shadow). Si MH pas livré : v2.1.1
  affiché à la place de v2.1 quand `SCORING_VERSION=v2.1.1`. Acceptable
  transitoire.
- Pas de nouveau panel UX dans M15.

---

## 11. Migration / Backwards compat

### 11.1 Migration Alembic 0009 (NOUVELLE)

M15 ajoute **2 colonnes DB** + 1 index → migration `0009_anti_toxic_lifecycle.py`.

```python
# migrations/versions/0009_anti_toxic_lifecycle.py — M15

"""M15 anti-toxic lifecycle — source_wallet_address + is_probation

Revision ID: 0009_anti_toxic_lifecycle
Revises: 0008_<previous>
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa


revision = "0009_anti_toxic_lifecycle"
down_revision = "0008_<previous>"  # à confirmer post-MC ship
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MB.1 — my_positions.source_wallet_address (nullable, indexed).
    with op.batch_alter_table("my_positions") as batch:
        batch.add_column(
            sa.Column("source_wallet_address", sa.String(42), nullable=True)
        )
    op.create_index(
        "ix_my_positions_source_wallet_closed",
        "my_positions",
        ["source_wallet_address", "closed_at", "simulated"],
        unique=False,
    )

    # MB.6 — target_traders.is_probation (NOT NULL, default False).
    with op.batch_alter_table("target_traders") as batch:
        batch.add_column(
            sa.Column(
                "is_probation",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    op.drop_index("ix_my_positions_source_wallet_closed", table_name="my_positions")
    with op.batch_alter_table("my_positions") as batch:
        batch.drop_column("source_wallet_address")
    with op.batch_alter_table("target_traders") as batch:
        batch.drop_column("is_probation")
```

**Idempotence** : `batch_alter_table` SQLite-friendly. Safe sur DB
M3..M14 existante. Aucun backfill `source_wallet_address` v1 (positions
historiques M3..M14 restent NULL — collecteur internal_pnl ignore).

### 11.2 Rollback path

Si une régression M15 apparaît post-merge :

- **Option A (runtime, recommandé)** : `SCORING_VERSION=v1` (default)
  → rollback v2.1.1 piloting. Restart bot suffit.
  - Probation : positions probation existantes persistent
    (`is_probation=True` en DB). Multiplier `0.25×` continue à
    s'appliquer aux trades détectés. **Pour rollback complet probation**,
    il faut SQL manuel `UPDATE target_traders SET is_probation = 0`.
  - Auto-blacklist : les wallets `target_traders.status="blacklisted"`
    auto-déclenchés restent. Pour les re-shadow → ajout/retrait
    `BLACKLISTED_WALLETS` env (cf. §11.5) ou SQL manuel
    `transition_status_unsafe`.
- **Option B (config flag)** : si rollback partiel souhaité —
  - `AUTO_BLACKLIST_PNL_THRESHOLD_USD=-1000.0` → effectivement désactive
    le critère pnl_threshold.
  - `AUTO_BLACKLIST_MIN_POSITIONS_FOR_WR=1000` → désactive le critère
    win_rate_floor.
  - `PROBATION_SIZE_MULTIPLIER=1.0` → désactive la réduction (équivaut
    full sizing sur les wallets probation existants).
- **Option C (git)** : `git revert <sha>` × 8 (les 8 commits MB.1 →
  MB.8). Migration 0009 doit être rollback via `alembic downgrade
  0008_<previous>` **avant** le revert (sinon les colonnes vivantes en
  DB causent un mismatch Pydantic au boot). Documenté §11.7.

### 11.3 Cohabitation v2.1 vs v2.1.1

Pendant la shadow period v2.1.1 (J0 à J+30) :

- `SCORING_VERSION=v1` (default) → 2 versions calculées en parallèle (v1
  pilote, v2.1.1 shadow).
- Si l'utilisateur avait `SCORING_V2_1_SHADOW_DAYS>0` actif (M14) → 3
  versions en parallèle (v1 pilote, v2.1 shadow M14, v2.1.1 shadow M15).
  **Recommandation §11.4 ship M15** : poser `SCORING_V2_1_SHADOW_DAYS=0`
  pour économiser le compute (v2.1 reste accessible via le registry
  pour audit historique).
- v2.1.1 cold-start : pendant 30j post-merge, la majorité des wallets
  ACTIVE auront `internal_pnl_score=None` → branche cold-start
  fallback v2.1 weights. **Donc v2.1.1 ≈ v2.1** durant le cold-start
  cleared.
- À J+30 post-merge : ≥50% des ACTIVE ont ≥10 positions copiées closed
  (cf. cible §0). v2.1.1 commence à diverger de v2.1 (poids 0.25 sur
  internal_pnl actif).

### 11.4 Cutover post-shadow (J+30)

**Séquence explicite** :

1. **T0** = merge M15 sur `main`. `SCORING_VERSION=v1` default.
   `SCORING_V2_1_SHADOW_DAYS=0` (recommandé).
2. **T0 + 30j** = fin shadow period internal_pnl. Lancer
   `python scripts/validate_mb_hypotheses.py --output
   /tmp/mb_validation.html` pour vérifier H-EMP-3 + H-EMP-11 + H-EMP-13
   sur 30j réels.
3. **T0 + 30j** : si H-EMP OK (cf. §14.4 seuils) ET visualisation
   manuelle dashboard `/performance` cohérente (cold-start cleared
   ≥50% ACTIVE, top-10 v2.1.1 différent de v2.1 par ≥3 ranks moyen,
   pas de wallet locked sur 10 cycles consécutifs) → user flip
   `SCORING_VERSION=v2.1.1` dans `.env` + redémarre.
4. **T0 + 60j** : double shadow inverse (v2.1 continue 30j supplémentaires
   en double-write si l'utilisateur le souhaite via `SCORING_V2_1_SHADOW_DAYS=30`).
   Si pas de régression détectée via dashboard `/pnl` → user set
   `SCORING_V2_1_SHADOW_DAYS=0`.

**Aucun auto-flip** : décision 100% humaine.

### 11.5 Discipline auto-blacklist réversible

Cf. §8.4. **Pour rendre la blacklist persistante** post-auto-blacklist :

```bash
# Sur uni-debian, après réception alert Telegram trader_auto_blacklisted :
echo "Adding 0x21ffd2b7…0d71 to BLACKLISTED_WALLETS"

# Édition .env :
sed -i 's/^BLACKLISTED_WALLETS=\(.*\)$/BLACKLISTED_WALLETS=\1,0x21ffd2b7..0d71/' .env

# Restart bot (auto-systemd ou manuel) :
pkill -f "python -m polycopy" && python -m polycopy --verbose &

# Vérifier au prochain cycle reconcile_blacklist :
# - target_traders.status reste "blacklisted" (env match → pas de unblacklist).
```

**Pour annuler une auto-blacklist (faux positif)** :

- Option simple (recommandée) : ne rien faire — au prochain
  `reconcile_blacklist` (cycle suivant), le wallet revient `shadow`
  automatiquement. Si l'utilisateur veut accélérer : `bash
  scripts/reconcile_blacklist_manual.sh` (à créer si besoin, OUT OF
  SCOPE M15).
- Option SQL : `UPDATE target_traders SET status='shadow' WHERE
  wallet_address = '0xTOX'` (manuel). **Garder en tête** : `target_traders`
  est un audit critique — modifier via SQL doit être rare et tracé.

### 11.6 Gestion des rows existantes

- `trader_scores` : `scoring_version="v1"` / `"v2"` / `"v2.1"` toutes
  intactes (audit trail préservé).
- `target_traders.is_probation=False` par défaut sur les rows existantes
  M3..M14 (server_default 0). Comportement strict M14.
- `my_positions.source_wallet_address=NULL` sur les rows historiques
  M3..M14. Le collecteur internal_pnl ignore (filtre exact match) —
  donc pour ces wallets, `count<10` jusqu'à accumulation de nouvelles
  positions post-merge. Cold-start naturel.

### 11.7 Rollback migration 0009 sécurisé

```bash
# 1. Stop bot (sinon mismatch Pydantic au write live).
pkill -f "python -m polycopy"

# 2. Backup DB (toujours).
cp ~/.polycopy/data/polycopy.db ~/.polycopy/data/polycopy.db.pre-rollback-m15

# 3. Downgrade Alembic.
source .venv/bin/activate
alembic downgrade 0008_<previous>

# 4. Git revert M15 commits (8 commits MB.1 → MB.8).
git log --oneline -20  # repère SHAs
git revert <SHA_MB.8> <SHA_MB.7> ... <SHA_MB.1>  # ordre inverse

# 5. Restart bot.
python -m polycopy --verbose
```

---

## 12. Commandes de vérification finale

Bloc copiable pour l'implémenteur M15 :

```bash
# 1. Environnement (idempotent).
cd /home/nexium/code/polycopy
source .venv/bin/activate

# 2. Lint + type-check (après chaque commit MB.x).
ruff check .
ruff format --check .
mypy src --strict

# 3. Tests ciblés par item MB (entre commits, ~30s chacun).
pytest tests/unit/test_metrics_collector_v2.py -x --tb=short -k "internal_pnl or net_exposure"  # MB.1 + MB.7
pytest tests/unit/test_scoring_v2_aggregator.py -x --tb=short -k "v2_1_1 or cold_start"          # MB.2
pytest tests/unit/test_decision_engine.py -x --tb=short -k "ranking or auto_blacklist or probation"  # MB.3 + MB.6 + MB.8
pytest tests/unit/test_eviction_state_machine.py -x --tb=short -k "fresh"                        # MB.4
pytest tests/unit/test_eviction_scheduler.py -x --tb=short -k "empirical_margin"                 # MB.5
pytest tests/unit/test_strategy_pipeline.py -x --tb=short -k "probation"                         # MB.6 (PositionSizer)
pytest tests/unit/test_scoring_v2_gates.py -x --tb=short -k "arbitrage"                          # MB.7

# 4. Tests intégration (opt-in).
pytest -m integration tests/integration/test_m15_e2e.py -x --tb=short

# 5. Full suite (à la fin uniquement, ~3 min).
pytest

# 6. Migration 0009 sanity check.
alembic upgrade head     # applique 0009
alembic downgrade -1     # rollback to 0008
alembic upgrade head     # ré-applique
sqlite3 ~/.polycopy/data/polycopy.db ".schema my_positions" | grep source_wallet_address
sqlite3 ~/.polycopy/data/polycopy.db ".schema target_traders" | grep is_probation

# 7. Validation hypothèses empiriques avant cutover (BLOQUANT user-side).
python scripts/validate_mb_hypotheses.py \
  --sql-dump tests/fixtures/h_emp_post_m15_seed.sql \
  --output /tmp/mb_validation.html
# Vérifier dans le rapport :
# - H-EMP-3 : Spearman ρ(internal_pnl_score, score v2.1.1) ∈ [0.1, 0.7]
# - H-EMP-11 : ≥90% des wallets pool passent le gate not_arbitrage_bot
# - H-EMP-13 : informatif — % wallets cumulative_pnl_90d > 0 (pas go/no-go)
# Si H-EMP-3 ou H-EMP-11 fail → STOP cutover, investiguer.

# 8. Smoke test runtime — 2 cycles avec scoring v2.1.1 actif.
SCORING_VERSION=v2.1.1 \
DISCOVERY_ENABLED=true \
DISCOVERY_INTERVAL_SECONDS=3600 \
TRADER_DAILY_PNL_ENABLED=true \
DASHBOARD_ENABLED=true \
EVICTION_ENABLED=false \
python -m polycopy --verbose &
sleep 7200   # 2 cycles
# Vérifier :
# - trader_scores rows scoring_version="v2.1.1" écrits.
# - cold_start_internal_pnl=True pour la majorité (cold-start cleared).
# - Aucun wallet locked sur 2 cycles.
# - Logs : aucun WARNING `weights_v2_1_1_renormalized_failed` ou
#   `scoring_v2_1_1_no_pool_context`.
kill %1 && wait

# 9. Smoke test probation lifecycle.
# (Manuel) : seed un wallet candidate avec trade_count=20, observer 1
# cycle discovery, vérifier `target_traders.is_probation=True`. Détecter
# 1 trade BUY, vérifier `my_orders.size = raw × 0.25`.

# 10. Smoke test auto-blacklist.
# (Manuel) : seed `MyPosition` rows pour un wallet ACTIVE avec PnL
# cumulatif < -$5. Run 1 cycle discovery. Vérifier :
# - `target_traders.status="blacklisted"`.
# - Alert Telegram (si TELEGRAM_BOT_TOKEN set).

# 11. Grep sécurité (aucun secret leak dans nouveaux logs/templates).
pytest tests/unit/test_no_secret_leak_in_m15_alerts_and_logs.py -v

# 12. Smoke rollback (flag v1 strict).
SCORING_VERSION=v1 python -m polycopy --verbose
# Vérifier : pas de calcul v2.1.1, pas de row trader_scores avec
# scoring_version="v2.1.1" sur le cycle.
```

Après `git push` sur `main`, côté production `uni-debian` :

```bash
ssh uni-debian
cd ~/Documents/GitHub/polycopy
git pull
# alembic upgrade head est lancé au boot via init_db M3.
# Bot auto-restart si systemd unit en place.
# Surveiller dans les 30j qui suivent :
# - /performance : aucun wallet ne reste ACTIVE indéfiniment avec PnL<-$5.
# - /pnl : pas de régression vs v2.1 baseline.
# - Telegram : alertes trader_auto_blacklisted attendues 0-2/14j.
# - logs : aucun WARNING `internal_pnl_repository_error` ou
#   `gate_arbitrage_bot_compute_failed`.
```

---

## 13. Hors scope M15 (à ne pas implémenter)

- ❌ **MF — Sirolly wash cluster continuous score** + **Mitts-Ofir
  composite informed_score**. M15 ajoute internal_pnl + arbitrage_bot
  gate uniquement.
- ❌ **MG — CLV (Closing Line Value) + Kelly proxy comme factor +
  liquidity-adjusted ROI**.
- ❌ **MH — Dashboard UX** : nouvelle colonne `internal_pnl_score` dans
  `/performance`, panel "fee-drag par wallet probation",
  `is_probation` badge UX, dashboard auto-blacklist history table.
  Tous reportés à MH (UX-pure).
- ❌ **Auto-detection wash cluster continue** (M17+).
- ❌ **Convergence signal cross-wallet** (Bullpen-style — MF).
- ❌ **Backfill rétroactif `source_wallet_address`** sur les rows M3..M14.
- ❌ **Backfill rétroactif `internal_pnl_score`** dans `trader_scores`
  rows v2.1 historiques (versioning sacré).
- ❌ **Auto-flip cutover v2.1 → v2.1.1**. Décision humaine post-shadow.
- ❌ **Anti-copy bait detection** (Claude §9 item 9 — wallet qui post
  bait fills pour piéger les copy bots) — feature future qui consommera
  l'`internal_pnl_score` produit par M15.
- ❌ **Goldsky free Starter pour `_compute_net_exposure_ratio`** sur
  les positions historiques 90j (scale futur si Data API quota
  insuffisant).
- ❌ **Latency tolerance factor** (`avg_holding_time` factor scoring) —
  MG.
- ❌ **Auto-blacklist via Telegram bot incoming command** — viole M7
  §13 emitter-only.
- ❌ **Probation 0.10× variant** (Gemini cold-start mention 0.1×). M15
  retient 0.25× (D5 cf. §14.1 + Frigo Kalshi AI bot quart-Kelly upper
  bound).
- ❌ **Window 180j + exp decay half-life 30j** pour internal_pnl. M15
  garde fenêtre stricte 30j (cohérent budget compute + cold-start
  clear rapide).
- ❌ **Multi-wallet aggregate `internal_pnl_score`** (ex: tous wallets
  d'un cluster Sirolly). MF.
- ❌ **Réintroduire `paused` status M5 en plus du blacklist DB-only**.
  M5_bis migration 0007 a converti paused → shadow. M15 ne ressuscite
  pas paused.

---

## 14. Notes d'implémentation & zones d'incertitude

### 14.1 Décisions clefs M15

- **D1** (MB.1) : `internal_pnl_score` utilise sigmoid (smooth cap)
  plutôt que linéaire bornée. Justification : évite saturation à ±∞ et
  préserve gradient au voisinage du seuil.
- **D2** (MB.1) : scaling factor sigmoid `/10` ($10 PnL ↔ score 0.73).
  Justification : sur capital virtuel $1000, $10 PnL/30j ≈ 1% monthly =
  signal raisonnable. Ajustable via `SCORING_INTERNAL_PNL_SCALE_USD`.
- **D3** (MB.1) : cold-start `internal_pnl_score = None` plutôt que
  0.5 neutre. Justification : 0.5 biaiserait tous les nouveaux wallets
  vers la médiane. `None` → facteur ignoré localement, renormalize 5
  autres. Plus honnête statistiquement.
- **D4** (MB.3) : ranking + safeguard threshold absolu à 0.30.
  Justification : ranking-based capture la dynamique pool, threshold
  capte les cas absolument cassés. Ceinture + bretelle.
- **D5** (MB.6) : probation `0.25×` quarter-Kelly, pas `0.10×`.
  Justification : Gemini §"Fallacy Full Kelly" cite catastrophic
  drawdowns above 0.25×, donc 0.25× est la limite supérieure sûre.
  0.10× serait trop conservateur (trades trop petits pour mesurer
  internal_pnl_score rapidement).
- **D6** (MB.7) : arbitrage bot gate est **dur** (rejet total), pas un
  penalty factor. Justification : ces wallets n'ont pas de directional
  signal **by design** — les copier est toujours négatif, pas juste
  sub-optimal.
- **D7** (MB.2) : bump version `"v2.1.1"` (mineur) plutôt que `"v2.2"`.
  Justification : on ajoute **1 facteur** (internal_pnl), on ne fait
  pas le gros upgrade Sirolly+Mitts-Ofir qui est MF. Preserve la
  sémantique de versioning sacré.
- **D8** (MB.1 + MB.8) : filtre `simulated` selon `execution_mode` :
  `live` → strict `simulated=False`, `dry_run` ET `simulation` →
  `simulated=True`. Justification : isole les modes — pas de
  pollution entre runs simulation et data réelle live.
- **D9** (MB.3) : ranking-based active déclenche démote uniquement si
  `wallet_rank >= MAX_ACTIVE_TRADERS` (rank 0-indexed, donc rank N
  signifie "out-of-top-N"). Pool sub-cap (active_count < cap) → personne
  out-of-top-N. Justification : ne pas churn artificiellement quand le
  pool est en sous-effectif (rotation est utile quand on est saturés).

### 14.2 Piège : `source_wallet_address` dans `MyPosition`

Le schéma actuel de `my_positions`
([storage/models.py:214](../../src/polycopy/storage/models.py#L214))
**n'a pas** la colonne `source_wallet_address`. Migration 0009 (MB.1)
ajoute la colonne (nullable, indexée).

**Piège** : la **propagation** depuis le pipeline → executor doit aussi
être ajoutée. Pour les nouveaux trades :

1. `WalletPoller` détecte le trade — `DetectedTrade.target_wallet`
   contient le wallet source.
2. Strategy pipeline : `OrderApproved` ou équivalent doit transporter
   `source_wallet_address = ctx.trade.target_wallet`.
3. Executor `_persist_sent_order` (M3) et `_persist_realistic_simulated`
   (M8) doivent **persister** `MyPosition.source_wallet_address` au
   moment de la création.
4. Pour les SELL copiés (M13 Bug 5) : `_check_sell` match
   `(condition_id, asset_id)` mais ne touche pas `source_wallet_address`
   (la position existe déjà avec son `source_wallet_address` initial).

**Test** : `test_my_position_source_wallet_persisted_on_create` — assert
qu'un nouveau trade insère `MyPosition.source_wallet_address` lower-cased.

**Acceptable v1** : positions historiques M3..M14 restent NULL — le
collecteur internal_pnl filtre exact match, donc ces rows sont ignorées.
Acceptable cold-start naturel post-merge (jusqu'à 30j d'accumulation).

### 14.3 Piège : `observed_win_rate < 0.25` calcul break-even (MB.8)

`observed_win_rate = wins / (wins + losses)` où :
- `wins = count(realized_pnl > 0)`
- `losses = count(realized_pnl < 0)`
- **Break-even (`realized_pnl == 0`) sont exclus du dénominateur**.

Si un wallet a 30 positions toutes break-even (`realized_pnl=0`),
`observed_win_rate = None` (decided=0). **Ne pas déclencher
auto-blacklist** (neutre pas mauvais). Test #27 couvre.

### 14.4 Piège : auto-blacklist flood protection au boot (MB.8)

Au boot, si plusieurs wallets ACTIVE sont déjà sous le seuil PnL (fix
d'un bug pré-existant), on ne veut **pas** spammer 10 alertes Telegram
en 30s.

**Mitigations** :

1. **Idempotence via `cooldown_key`** : `Alert(cooldown_key=
   f"auto_blacklist_{wallet}")`. Le cooldown_key déduplique sur la
   fenêtre `TELEGRAM_ALERT_DIGEST_WINDOW_SECONDS` (M7).
2. **Idempotence via DB status** : `_maybe_auto_blacklist` checke en
   premier `if current.status == "blacklisted": return None` avant
   d'aller plus loin. Wallet déjà blacklist → pas de re-fire.
3. **Cap par cycle** : si > 5 auto-blacklist déclenchés sur 1 cycle →
   log WARNING `auto_blacklist_storm_detected` (signal d'investigation).
   Pas de hard-cap — la sécurité prime.

Test #27 valide idempotence.

### 14.5 Piège : `OrderApproved` doit transporter `source_wallet_address`

Le DTO qui passe du pipeline strategy à l'executor doit porter
`source_wallet_address` pour que `_persist_*` puisse le persister sur
`MyPosition`.

**Vérification** : `src/polycopy/strategy/dtos.py` (`OrderApproved` ou
`PipelineContext.trade`) doit contenir le wallet source. Si déjà
présent via `ctx.trade.target_wallet` (DTO source) — ajouter dans
l'executor un mapping `MyPosition.source_wallet_address = ctx.trade.target_wallet.lower()`.

### 14.6 Piège : ordering des cycles discovery + eviction + internal_pnl collector

1. Cycle discovery écrit `trader_scores` avec scores v2.1.1 (incluant
   `internal_pnl_score` OU None).
2. `DecisionEngine._decide_active` utilise les fresh scores pour
   ranking (MB.3).
3. `EvictionScheduler.run_cycle(scores_by_wallet=fresh)` est hook
   post-decision — utilise les **mêmes** fresh scores (MB.4 fix
   propagation H-007).
4. Internal PnL collector tourne **avant** le scoring (peuple
   `internal_pnl_score` dans le breakdown), pas en parallèle.

### 14.7 Piège : probation vs sized 0.25× dans PositionSizer

Le `PositionSizer` (
[strategy/pipeline.py:155-258](../../src/polycopy/strategy/pipeline.py#L155-L258))
ne connaît pas directement le source_trader. Besoin d'enrichir le
`DetectedTradeDTO` avec `is_source_probation: bool` dans le
`WalletPoller` (MB.6) — pas de N+1 query dans le pipeline.

`WalletPoller._enrich_with_probation_flags` : 1 batch query
`SELECT wallet_address, is_probation FROM target_traders WHERE
wallet_address IN :wallets` par cycle. Coût négligeable (5-10 wallets
× 1 row).

### 14.8 H-EMP-3 + H-EMP-11 + H-EMP-13 : script de validation pré-cutover

**Script `scripts/validate_mb_hypotheses.py`** (nouveau, ~180 LOC) :

```python
"""Validation H-EMP-3 + H-EMP-11 + H-EMP-13 avant cutover SCORING_VERSION=v2.1.1.

Lit la DB locale post-30j de v2.1.1 shadow, calcule :

- H-EMP-3 : Spearman ρ(internal_pnl_score, score v2.1.1) sur tous wallets
            avec ≥10 closed positions copiées. Seuil go : 0.1 < ρ < 0.7
            (trop bas = un des deux est bruit, trop haut = redondance).
- H-EMP-11 : % wallets pool qui passent le gate not_arbitrage_bot.
             Seuil go : ≥90% (sinon gate trop strict).
- H-EMP-13 : informatif — % wallets pool avec cumulative_pnl_90d > 0.
             Pas go/no-go, juste sanity.

Outputs : rapport HTML + JSON. Exit code 0 si seuils atteints, 1 sinon.

Usage :
    python scripts/validate_mb_hypotheses.py --output /tmp/mb_validation.html
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

# imports stdlib + pandas (optionnel — calcul Spearman manuel possible)


def compute_h_emp_3(rows: list[dict]) -> tuple[float, int]:
    """Spearman correlation entre `internal_pnl_score` et `score` v2.1.1.

    Filtre sur rows avec `metrics_snapshot.internal_pnl_score IS NOT
    NULL` (cold-start cleared) ET `scoring_version="v2.1.1"`.
    """
    eligible = [
        (r["metrics_snapshot"]["internal_pnl_score"], r["score"])
        for r in rows
        if r.get("scoring_version") == "v2.1.1"
        and r.get("metrics_snapshot", {}).get("internal_pnl_score") is not None
    ]
    if len(eligible) < 10:
        return 0.0, 0
    # Manual Spearman (rank-based correlation) via scipy or hand-rolled.
    # ... implementation ...
    return rho, len(eligible)


def compute_h_emp_11(active_wallets_metrics: list[dict]) -> float:
    """% wallets dont `net_exposure_ratio >= 0.10`."""
    if not active_wallets_metrics:
        return 1.0
    pass_count = sum(1 for m in active_wallets_metrics if m["net_exposure_ratio"] >= 0.10)
    return pass_count / len(active_wallets_metrics)


def compute_h_emp_13(active_wallets_metrics: list[dict]) -> float:
    """% wallets dont `cash_pnl_90d > 0`. Informatif uniquement."""
    if not active_wallets_metrics:
        return 0.0
    pass_count = sum(1 for m in active_wallets_metrics if m["cash_pnl_90d"] > 0)
    return pass_count / len(active_wallets_metrics)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path,
                        default=Path.home() / ".polycopy" / "data" / "polycopy.db")
    parser.add_argument("--output", type=Path, default=Path("/tmp/mb_validation.html"))
    args = parser.parse_args()

    rows = _load_trader_scores(args.db_path)
    metrics = _load_active_metrics(args.db_path)

    rho, h3_n = compute_h_emp_3(rows)
    h11 = compute_h_emp_11(metrics)
    h13 = compute_h_emp_13(metrics)

    h3_pass = (0.1 < rho < 0.7) and h3_n >= 10
    h11_pass = h11 >= 0.90

    _write_report(args.output, rho, h3_n, h11, h13, h3_pass, h11_pass)

    if h3_pass and h11_pass:
        print(f"✅ MB validation PASSED")
        print(f"H-EMP-3: ρ={rho:.3f} on {h3_n} wallets (target [0.1, 0.7]) → OK")
        print(f"H-EMP-11: {h11:.1%} pass arb gate (target ≥90%) → OK")
        print(f"H-EMP-13 (info): {h13:.1%} cumulative_pnl_90d > 0")
        return 0
    print(f"❌ MB validation FAILED — cutover blocked")
    print(f"H-EMP-3: ρ={rho:.3f} on {h3_n} wallets → {'OK' if h3_pass else 'FAIL'}")
    print(f"H-EMP-11: {h11:.1%} pass arb gate → {'OK' if h11_pass else 'FAIL'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

### 14.9 Coût compute estimation

- MB.1 : 1 query SQL par wallet par cycle scoring. Sur 50 wallets
  candidats × 1 cycle/6h = 200 queries/jour. **Négligeable** (index
  composite `(source_wallet_address, closed_at, simulated)`).
- MB.7 : `_compute_net_exposure_ratio` réutilise les `RawPosition`
  déjà fetchées par `MetricsCollectorV2._fetch_raw_positions` (M14
  baseline). **Aucun call API supplémentaire**, juste de
  l'arithmetic local en mémoire.
- MB.3 : ranking nécessite 1 query `list_active_scores()` par cycle de
  decision (scoring fresh). **Négligeable** vs M14 baseline.
- MB.5 : 1 query `pstdev(scores)` au boot uniquement. Pas par cycle.

**Budget cycle scoring v2.1.1** : reste sous 5 min/cycle (cohérent
M14 §14.5 + M12 §11.4).

### 14.10 Open questions M15

À résoudre **post-ship** si les seuils ne sont pas atteints :

1. **Q1 (post-ship)** : si H-EMP-3 ρ > 0.7 (redondance internal_pnl ↔
   score v2.1.1) → revoir le scaling factor `SCORING_INTERNAL_PNL_SCALE_USD`
   (passer de 10.0 à 50.0 par exemple) ou revoir la pondération 0.25
   (réduire à 0.15).
2. **Q2 (post-ship)** : si H-EMP-11 < 90% (gate `not_arbitrage_bot`
   trop strict) → considérer baisser le threshold `_NET_EXPOSURE_MIN`
   de 0.10 à 0.05 (plus permissif). Mais d'abord investiguer : peut-être
   la collection `RawPosition.outcome_index` est manquante sur > 10%
   des positions → fallback `inférable via detected_trades` à
   considérer (cf. §14.2 piège).
3. **Q3 (post-ship)** : si auto-blacklist tire 0 fois sur 30j
   d'observation post-cutover → seuils `AUTO_BLACKLIST_PNL_THRESHOLD_USD`
   trop laxiste (passer de −$5 à −$2) ou `AUTO_BLACKLIST_MIN_POSITIONS_FOR_WR`
   trop élevé (passer de 30 à 20).
4. **Q4 (post-ship)** : si probation auto-release tire trop tôt (avant
   collecte internal_pnl ≥10 positions) → augmenter
   `PROBATION_FULL_TRADES` à 75 par exemple. Trade-off vs bypass
   Mitts-Ofir black-swan capture.

### 14.11 Versioning de la formule v2.1.1

- **Si une pondération est modifiée post-ship** (suite Q1/Q2) → bumper
  `SCORING_VERSION` à `"v2.1.2"` (literal étendu, registry +1 entrée).
  **Jamais** réécrire les rows v2.1.1 historiques.
- Cf. CLAUDE.md §Conventions : "changer une pondération = bumper
  version".

### 14.12 Pendant la phase cold-start (J0 → J+30)

**Comportement attendu** :

- Tous les wallets ACTIVE en cold-start (count<10) → score v2.1.1
  identique à v2.1 (branche cold-start).
- Au fur à mesure que les positions se ferment :
  - J+7 : ~15-20% des ACTIVE ont count≥10.
  - J+14 : ~40% des ACTIVE.
  - J+30 : ~60-80% des ACTIVE (cible §0).

Pour accélérer la transition (test 14j sur uni-debian) :
- Possibilité de **réduire `SCORING_INTERNAL_MIN_POSITIONS=5`** au lieu
  de 10. Trade-off : moins de bruit statistique vs cold-start clear
  rapide. À discuter en cas de test serré.

---

## 15. Prompt d'implémentation

Bloc à coller tel quel dans une nouvelle conversation Claude Code à
l'implémentation M15.

````markdown
# Contexte

polycopy lifecycle anti-toxic souffre de 8 défauts structurels documentés
dans [docs/audit/2026-04-24-polycopy-code-audit.md](docs/audit/2026-04-24-polycopy-code-audit.md)
+ session A brouillon
[docs/bug/session_A_anti_toxic_trader_lifecycle.md](docs/bug/session_A_anti_toxic_trader_lifecycle.md)
+ deep-search synthèse §2.2 F02 + §2.3 F11 + §2.4 F14 + Convexly Edge
Score V3b methodology. M15 livre **anti-toxic lifecycle + internal PnL
feedback** = bundle 8 items (MB.1 → MB.8) qui ferme la boucle.

# Prérequis

- Lire `docs/specs/M15-anti-toxic-lifecycle.md` **en entier** (spécifiquement
  §5 Algorithmes + §9 Test plan + §14 Notes implémentation).
- Lire [CLAUDE.md](CLAUDE.md) §Conventions + §Sécurité (versioning sacré
  scoring, discipline blacklist réversible, append-only trader_scores).
- Lire [docs/next/MB.md](docs/next/MB.md) pour le brief actionnable +
  tableau origines.
- Vérifier que MA (M14 v2.1) est shippé : `grep "scoring_version=v2.1"
  src/polycopy/discovery/scoring/v2/aggregator.py`.

# Ordre de commits recommandé

1. `feat(storage): MB.1 add my_positions.source_wallet_address + target_traders.is_probation (migration 0009)` (4 tests §9.1 + DTO + repository helpers)
2. `feat(discovery): MB.7 not_arbitrage_bot gate via net_exposure_ratio` (4 tests §9.7)
3. `feat(scoring): MB.2 internal_pnl factor in v2.1.1 with cold-start branch` (4 tests §9.2)
4. `feat(discovery): MB.3 ranking-based _decide_active + absolute floor safeguard` (5 tests §9.3 + adapter ~4 tests M5/M14 existants)
5. `fix(eviction): MB.4 propagate fresh scores to _delta_vs_worst (audit H-007)` (2 tests §9.4)
6. `feat(eviction): MB.5 boot helper _log_empirical_margin_recommendation` (1 test §9.5)
7. `feat(strategy): MB.6 probation 0.25× sizing + WalletPoller enrichment` (4 tests §9.6 + integration §9.9 #30)
8. `feat(decision): MB.8 auto-blacklist + Telegram trader_auto_blacklisted template` (3 tests §9.8 + integration §9.9 #31)

**Push sur main après chaque commit.** Tests verts avant chaque push.

# Validation pré-cutover (post-ship + 30j)

Après merge M15, attendre 30j calendaires de collecte data
(internal_pnl_score cold-start cleared) avant de flip
`SCORING_VERSION=v2.1.1`. Avant le flip :

```bash
python scripts/validate_mb_hypotheses.py --output /tmp/mb_validation.html
echo "Exit code: $?"
```

Vérifier :
- H-EMP-3 : Spearman ρ(internal_pnl, score v2.1.1) ∈ [0.1, 0.7]
- H-EMP-11 : ≥90% des wallets passent gate not_arbitrage_bot

Si l'un des deux échoue → **STOP**. Investiguer (cf. §14.10).

# Tests + quality gates

- Tests ciblés entre commits (cf. memory `feedback_test_scope`).
- Full `pytest` + `ruff check .` + `ruff format --check .` + `mypy src --strict` à la fin.
- Si tests M5/M5_bis/M14 cassent en cascade (changement comportemental
  MB.3 sur `_decide_active`) → **adapter** les tests existants. Documenter
  dans le commit message la nouvelle expected behavior.
  Estimation : ~12-15 tests existants à adapter (cf. §10.3).

# Git workflow

- **Tout commit directement sur `main`** — pas de branche, pas de PR
  (règle projet, workflow trunk-based).
- 8 commits atomiques (1 par item MB) poussés en série sur `main` après
  validation tests verts entre chaque push.
- Update CLAUDE.md §Conventions avec la nouvelle version `"v2.1.1"`
  dans le même run (commit additionnel ou agrégé à MB.8).

# Plan à confirmer

Commence par me confirmer ton plan en 1 message bref (1 phrase par
commit MB.x), puis enchaîne les 8 commits dans l'ordre ci-dessus. Tests
verts entre chaque push. Avant MB.2 (gros bloc aggregator) **montre-moi
le diff** pour valider le placement cold-start branch.

# Contraintes non négociables

- `SCORING_VERSION="v1"` reste default. v2.1.1 ne pilote pas tant que
  l'utilisateur n'a pas explicitement flip après shadow + H-EMP
  validation.
- **Versioning sacré** : aucune row `trader_scores` v1 / v2 / v2.1
  n'est réécrite. v2.1.1 ajoute des rows en parallèle.
- **Migration Alembic 0009** obligatoire (deux nouvelles colonnes). Tests
  upgrade/downgrade SQLite-friendly via `batch_alter_table`.
- **Triple garde-fou M3 + 4ᵉ M8 préservés** : aucune nouvelle creds
  consommée, aucune nouvelle surface POST.
- **Aucune creds CLOB consommée** — M15 100% read-only côté API publique
  + writes DB locale uniquement (status transitions, events, probation
  flag, source_wallet propagation).
- **Conventions CLAUDE.md** : async, Pydantic v2 frozen, SQLAlchemy 2.0,
  structlog, docstrings FR / code EN, pas de print.
- **mypy strict propre, ruff propre, coverage ≥ 80%** sur fichiers
  modifiés.

# Demande-moi confirmation AVANT

- Modifier `MyPosition` schema (MB.1 ajoute `source_wallet_address`).
- Modifier `TargetTrader` schema (MB.6 ajoute `is_probation`).
- Modifier `OrderApproved` ou `_persist_*` côté Executor pour propager
  `source_wallet_address` (cf. §14.5 piège).
- Refactor `_decide_active` (MB.3 — change la sémantique demote).
- Update CLAUDE.md (§10).

# STOP et signale si

- Tests M5/M5_bis/M14 cassent en cascade > 20 (signal de scope creep).
- Le DTO `OrderApproved` ne supporte pas la propagation
  `source_wallet_address` sans refactor majeur (cf. §14.5).
- `RawPosition.outcome_index` rarely présent dans Data API
  `/positions` (< 70%) → MB.7 net_exposure_ratio peu fiable, à
  reporter à MF.
- Cycle scoring > 10 min sur 50 wallets après MB.1+MB.7 (régression
  compute, cf. §14.9).

# Smoke test final obligatoire avant merge

```bash
SCORING_VERSION=v2.1.1 \
SCORING_V2_1_SHADOW_DAYS=0 \
DISCOVERY_ENABLED=true \
DISCOVERY_INTERVAL_SECONDS=3600 \
TRADER_DAILY_PNL_ENABLED=true \
DASHBOARD_ENABLED=true \
EVICTION_ENABLED=false \
python -m polycopy --verbose
```

Sur 2 cycles minimum, vérifier :
- `trader_scores` rows `scoring_version="v2.1.1"` écrits.
- Majorité des rows ont `cold_start_internal_pnl=True` (attendu J0).
- Aucune erreur logs (pas de `WARNING gate_arbitrage_bot_compute_failed`,
  `internal_pnl_repository_error`, `weights_v2_1_1_renormalized_failed`).
- `target_traders.is_probation` cohérent (faux par défaut).
- Smoke pipeline strategy avec un trade synthétique → vérifier que
  `is_source_probation=False` n'altère pas `my_size`.

Pas de commit récap final — la séquence des 8 commits MB.x sur `main`
constitue le bundle. Si tu veux un repère grossissable dans `git log`,
ajoute juste un commit doc `docs: M15 anti-toxic lifecycle shipped`
après MB.8 (optionnel).
````

---

## 16. Commit message proposé

```
feat(discovery): M15 anti-toxic lifecycle + internal PnL feedback (MB.1→MB.8)

Bundle 8 items qui ferme la boucle anti-toxic ouverte par
l'observation 2026-04-24 (wallet 0x21ffd…0d71 ACTIVE 5j WR 19%
PnL −$0.55 sans aucun mécanisme automatique pour le démote) :

- MB.1 my_positions.source_wallet_address (migration 0009) +
  _compute_internal_pnl_score(wallet) sigmoid sur 30j de PnL réalisée
  par polycopy. Cold-start None si <10 positions copiées closed.
  Filtre simulated selon execution_mode.
- MB.2 nouveau facteur internal_pnl dans v2.1.1 (poids 0.25,
  redistribution proportionnelle des 5 v2.1). Branche cold-start
  fallback v2.1 weights si internal_pnl_score is None. Bump
  SCORING_VERSION literal "v2.1.1".
- MB.3 _decide_active ranking-based (top-N MAX_ACTIVE_TRADERS) +
  safeguard absolute hard floor 0.30. Élimine la pathologie "wallet
  0.66 ACTIVE indéfiniment".
- MB.4 fix audit H-007 — classify_sell_only_transitions consume
  fresh inputs.scores pour _delta_vs_worst (active_non_pinned).
  Audit trail enfin cohérent.
- MB.5 boot helper _log_empirical_margin_recommendation : observe
  std(active_scores) 7j, log recommandation 1σ. No-op auto-tweak.
- MB.6 probation 0.25× quarter-Kelly pour wallets [10, 50) trades.
  is_probation flag DB (migration 0009). PositionSizer multiplie
  my_size *= 0.25 si is_source_probation. WalletPoller enrichit
  DTO via 1 batch query/cycle. Auto-release au gate full ≥50/≥30.
- MB.7 nouveau gate dur not_arbitrage_bot (net_exposure_ratio ≥
  0.10) — capture les $40M/an d'arbitrageurs YES+NO documentés
  Dev Genius / Claude §9 item 5.
- MB.8 auto-blacklist sur PnL<-$5 OR (count≥30 AND wr<25%). Nouveau
  template Telegram trader_auto_blacklisted.md.j2 MarkdownV2 strict.
  Discipline réversible identique M5_bis.

Hypothèses empiriques validées post-30j-cutover via
scripts/validate_mb_hypotheses.py (H-EMP-3 ρ ∈ [0.1, 0.7],
H-EMP-11 ≥90% pass, H-EMP-13 informatif).

26 tests unit + 3 tests intégration. Backward-compat : v1 default
préservé, v2 / v2.1 registry intacts, version v2.1.1 additive.
Migration 0009 SQLite-friendly batch_alter_table.

Cf. spec [docs/specs/M15-anti-toxic-lifecycle.md](docs/specs/M15-anti-toxic-lifecycle.md).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## 17. Critères d'acceptation

- [ ] 8 items MB.1 → MB.8 implémentés selon §5.
- [ ] Migration Alembic 0009 SQLite-friendly (`batch_alter_table`)
      avec upgrade ET downgrade tested.
- [ ] `MyPosition.source_wallet_address` indexé `(source_wallet_address,
      closed_at, simulated)`.
- [ ] `TargetTrader.is_probation` defaults `False` server-side.
- [ ] `_compute_internal_pnl_score` retourne `None` si `count < 10`
      OR `scale_usd <= 0` (defensive).
- [ ] Sigmoid `internal_pnl_score` clippé `[-50, 50]` avant exp() pour
      éviter overflow flottant.
- [ ] Pondérations v2.1.1 sommen à `1.0 ± 1e-6` (assert ImportError
      au load + test unit).
- [ ] Branche cold-start renormalize aux poids v2.1 (somme=1.0 sur 5
      facteurs).
- [ ] `SCORING_VERSION` literal extension `Literal["v1","v2","v2.1","v2.1.1"]`.
      Default reste `"v1"`.
- [ ] Registry `SCORING_VERSIONS_REGISTRY["v2.1.1"]` exposé.
      `_compute_score_v2_1_1_wrapper` distinct du v2.1 wrapper M14.
- [ ] `_decide_active` ranking-based : pool sub-cap → no demote (test
      MB.3 #13).
- [ ] `_decide_active` safeguard absolute hard floor < 0.30 force-demote
      indépendamment du ranking.
- [ ] `pinned` wallets jamais demote-able (safeguard M5 préservé).
- [ ] `_delta_vs_worst_fresh` consomme `inputs.scores` pour `active_non_pinned`
      (H-007 fix). Wallet absent du dict → fallback `t.score` snapshot
      DB.
- [ ] `_log_empirical_margin_recommendation` no-op si `< 10` samples
      OR `EVICTION_ENABLED=false`.
- [ ] Probation `0.25×` multiplier appliqué dans `_check_buy` après le
      fee/EV check M16, avant `ctx.my_size = raw_my_size`.
- [ ] Probation **ne bypasse pas** `cash_pnl_positive`,
      `not_blacklisted`, `not_wash_cluster`, `not_arbitrage_bot`,
      `zombie_ratio_max`.
- [ ] `WalletPoller._enrich_with_probation_flags` 1 query batch par
      cycle (pas N+1).
- [ ] `_compute_net_exposure_ratio` retourne `1.0` (default safe) si
      aucune `RawPosition` éligible (`outcome_index is None`).
- [ ] Gate `not_arbitrage_bot` ajouté en queue de `check_all_gates` (DTO
      computed lookup en dernier).
- [ ] Auto-blacklist idempotent : wallet déjà `status="blacklisted"` →
      no-op + cooldown_key prévient le re-fire alert.
- [ ] Auto-blacklist break-even exclus du dénominateur win_rate
      (test MB.8 #27 win_rate=None si decided=0).
- [ ] Template `trader_auto_blacklisted.md.j2` MarkdownV2 escape strict
      (pas de char actif unescaped).
- [ ] Test grep secret leak passe sur les nouveaux events structlog +
      template Telegram.
- [ ] Boot log `eviction_margin_empirical_recommendation` apparaît si
      `EVICTION_ENABLED=true` ET ≥ 10 samples.
- [ ] CLAUDE.md §Conventions mise à jour avec mention v2.1.1 + section
      M15 (cf. §10 spec M14 pour pattern).
- [ ] `.env.example` : 9 nouvelles variables M15 documentées avec
      commentaires.
- [ ] Tests M5 / M5_bis / M14 existants adaptés (changement comportemental
      MB.3, additif MB.7 gates). Aucun test v1 ne casse.
- [ ] Script `scripts/validate_mb_hypotheses.py` produit rapport
      HTML/JSON, exit code 0 si seuils atteints (H-EMP-3 + H-EMP-11).
- [ ] **Invariants M5 / M5_bis / M5_ter / M11 / M12 / M13 / M14 / M16
      préservés** : lifecycle existant, eviction core, watcher
      live-reload, latency stages, registry v1/v2/v2.1, dry-run
      executor, fees-aware sizer — tous intacts. Tests ciblés passent
      inchangés.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src --strict` :
      0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur fichiers modifiés.
- [ ] Smoke test 2 cycles `SCORING_VERSION=v2.1.1` : aucun
      `weights_v2_1_1_renormalized_failed`, majorité rows
      `cold_start_internal_pnl=True` (J0).
- [ ] 8 commits atomiques MB.1 → MB.8 poussés sur `main` (pas de
      branche, pas de PR — règle projet).

---

**Fin spec M15.**
