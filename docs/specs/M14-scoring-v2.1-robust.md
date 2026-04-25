# M14 — Scoring v2.1-ROBUST (foundation)

**Status** : Draft — 2026-04-25
**Depends on** : M5 (lifecycle discovery), M5_bis (eviction opt-in), M12 (scoring v2 — formule pilote actuelle), M13 (dry-run observability — débloque les données de réalisation copy PnL nécessaires à H-EMP-3)
**Bloque** : MB (lifecycle dépend de scoring stable), MF (v2.2-DISCRIMINATING capstone), MG (nouveaux facteurs s'ajoutent à v2.1)
**Workflow git** : commits directement sur `main` (pas de branche ni PR — règle projet)
**Charge estimée** : M (3-4 jours dev + 14j shadow calendaire avant cutover v2.1 ↔ v1)

---

## 0. TL;DR

M14 livre **`Score_v2.1-ROBUST`** : foundation scoring stable et interprétable
qui remplace **directement v2** comme formule shadow vs v1 (cf. décision **D6**
infra). Ne touche ni au lifecycle M5, ni à l'eviction M5_bis, ni à la migration
DB. **8 items couplés** mappés MA.1 → MA.8 du brief
[docs/next/MA.md](../next/MA.md) :

- **MA.1** — Drop `timing_alpha` (poids → 0). Le placeholder `0.5` injectait
  +0.10 uniforme sur tous les scores ([H-008] audit).
- **MA.2** — Remplacer winsorisation p5-p95 par **rank transform**
  `rank(w) / N`. Élimine le fixed-point trap C7 ("wallet locked at 0.45 sur
  80 cycles", session B). Variance cycle-to-cycle ±30% → ±5-10% projetée.
- **MA.3** — Fix Sortino sentinel 3.0 sur curve plate ([H-009]). Exiger
  `pstdev(returns) > 1e-3` ou retourner 0.0. Combiner via
  `median(Sortino, Calmar)` plutôt que moyenne pondérée.
- **MA.4** — Fix Brier `P(YES)` au lieu de `P(side_bought)` ([M-001]).
  Baseline = pool-mean cohérent (suppression du `0.25` hardcodé `[M-003]`).
- **MA.5** — Flip `specialization = HHI` (pas `1 - HHI`). Corrige C9 / Mitts &
  Ofir : insider wallets concentrent (HHI → 1.0).
- **MA.6** — Implémenter le filtre temporel `< 30j` du `_compute_zombie_ratio`
  ([H-014] — docstring documente le filtre, code ne l'applique pas).
- **MA.7** — Recalibrer `EVICTION_SCORE_MARGIN` 0.15 → **0.10** (≈ 1σ
  empirique post-rank-transform).
- **MA.8** — Ship `SCORING_VERSION="v2.1"` dans le registry, shadow vs **v1**
  (pas v2 — jugé non-viable). Append-only `trader_scores`, **aucune** migration
  Alembic (la colonne `String(16)` accepte déjà `"v2.1"` depuis 0006 M12).

Résultat attendu ≤ 14j post-merge :

- **Aucun wallet locked** sur ≥ 10 cycles consécutifs (grep SQL automatisable).
- **Couverture v2.1 ≥ v1 × 0.8** (actuel : v2 score 13/50 = 26 %).
- **Variance cycle-to-cycle < 10 %** sur ≥ 80 % des wallets ACTIVE.
- **Top-10 v2.1 vs v1** : Δ rank moyen > 3 ranks (preuve discrimination réelle).
- **Hypothèses empiriques H-EMP-1 + H-EMP-2 validées** sur les 280 cycles
  historiques avant ship (cf. §14.4).

Tests cumulés estimés : **~28 tests unit** + **2 tests intégration** (H-EMP).
Prérequis : aucun. Bloque : MB / MF / MG.

---

## 1. Motivation & use case concret

### 1.1 Le symptôme observé — 2026-04-24

Dump SQL utilisateur sur 280 cycles `trader_scores` (v2 shadow actif depuis
2026-04-13) :

> - Wallet `0x63d43bbb…a2f1` : score = **0.450 strictement** sur **80 cycles
>   consécutifs** (12 jours). Aucune autre valeur observée.
> - Wallet `0x08c95f70…a2ef` : oscillation `0.25 → 0.48 → 0.26 → 0.38 → 0.43`
>   entre cycles consécutifs (variance ±30-40 %).
> - Couverture v2 : **13/50 wallets** scorés (26 %) à J+5 vs pool v1 = 50/50.
> - Onglet `/traders/scoring` (commit `1ba8ae3`) : Spearman rank affiche encore
>   des ranks du pool **entier** (33, 45, 52) au lieu du **subset commun v1∩v2**.
>   Fix incomplet — migré en MH (UX).

Diagnostic croisé via 3 deep-searches + audit ([Claude
§3.1-§4.1](../deepsearch/claude_Architectural_Review_of_Scoring_Discovery_and_Latency_for_a_Single-Process_asyncio_Polymarket_Copy-Trader_(April_2026).md),
[Gemini §"Analysis v2
Factors"](../deepsearch/gemini_Polycopy%20_esearch_Brief_%20Smart_Money.md),
[synthèse §2.2-§2.3](../deepsearch/SYNTHESIS-2026-04-24-polycopy-scoring-discovery-latency.md))
+ audit code 2026-04-24 :

| Symptôme | Root cause | Référence |
|---|---|---|
| Wallet locked à 0.45 | Fixed-point trap rank-normalization "lower" sur small N (C7) | Claude C7, [audit C-007](../audit/2026-04-24-polycopy-code-audit.md) |
| Variance ±30% cycle-to-cycle | Winsorisation p5-p95 sur N=13 = quasi pas de winsorisation, p95 bouge à chaque ajout/retrait wallet | Claude C6 + F05 (3/3 sources) |
| Zombies au top du pool | Sortino sentinel=3.0 sur curve plate domine post-normalisation | Claude C10 + F03 (3/3) + [H-009] |
| +0.10 uniforme sur tous | `timing_alpha=0.5` placeholder + pool-normalization → tous wallets ont le même | [H-008] + F01 (3/3) |
| Wallets nouveaux faussement zombies | `_compute_zombie_ratio` docstring documente filtre <30j, code ne l'applique pas | [H-014] |
| Brier comparable entre wallets | `_compute_brier` mélange `P(YES)` et `P(side_bought)` ; baseline `0.25` hardcodé vs scoring pool-mean | [M-001] + [M-003] + F04 |
| Wallets diversifiés sur-récompensés | `1 - HHI` pénalise concentration, exact opposé de Mitts-Ofir empirical | Claude C9 + F07 + [Mitts-Ofir 2026](https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/) |
| Eviction jamais déclenchée | `EVICTION_SCORE_MARGIN=0.15` = 50% du range observé `[0.3, 0.7]` | Claude §3.1 + §9 item 4 |

**5 défauts simultanés**, indépendants par la cause mais couplés dans leurs
effets. Une correction unitaire ne suffit pas — il faut le bundle.

### 1.2 Pourquoi un seul livrable bundle (pas 8 commits indépendants espacés)

Le brouillon session B `docs/bug/session_B_scoring_v2_reliability.md`
proposait initialement 7 items (B1-B7) à shipper en série. Triangulation
deep-search a montré que :

1. **Les fixes sont chaînés mathématiquement** : la pondération renormalisée
   après MA.1 (drop `timing_alpha`) dépend du résultat MA.2 (rank transform)
   pour évaluer la nouvelle variance. MA.3 (Sortino fix) est dominé par
   l'effet MA.2 (rank ne souffre plus du sentinel cluster). MA.7 (margin)
   exige les 2 précédents pour mesurer le 1σ effectif.
2. **Tester par moitié = bruit** : si on ship MA.1 seul, la variance reste
   ±30 % (winsorisation domine), on attribue à tort le "succès" à autre chose.
3. **Bump version unique** : `v2.1` = un score, une formule, une règle de
   versioning sacré. Bumper 5× vers `v2.0.1`, `v2.0.2`, ... violerait
   l'invariant CLAUDE.md "scoring versions append-only".
4. **Migration cutover propre** : MA.8 active le shadow v2.1 vs v1. Cohérence
   = un seul flip.

Donc M14 = **un seul bundle, 1 commit final unique** suivant pattern M12 / M13.

### 1.3 Ce qui ne change PAS dans M14

Diff M14 strictement additif sur les invariants suivants — aucune ligne
modifiée :

- **Lifecycle M5** : `shadow → active → paused → pinned`, cap
  `MAX_ACTIVE_TRADERS`, `BLACKLISTED_WALLETS`, `pinned` jamais demote-able.
- **M5_bis eviction core** : `EvictionScheduler`, `CascadePlanner`,
  `state_machine`, `HysteresisTracker`. Seul le **default** de
  `EVICTION_SCORE_MARGIN` change (0.15 → 0.10) — la logique eviction reste.
- **M5_ter watcher live-reload** : `WalletPoller`, `DataApiClient`. M14 ne
  touche pas au watcher.
- **M11 latency / WS market** : `ClobMarketWSClient`, 6 stages
  `trade_latency_samples`, `/latency`. Intacts.
- **M12 squelette** : sous-package `scoring/v2/` reste en place, pure
  functions par facteur, gates.py, normalization.py exposées identiques.
  M14 modifie le **contenu** de quelques modules (4 facteurs + normalization
  + aggregator), pas la structure.
- **M13 dry-run** : 5 sujets shippés ([commits d287fbc → 92edac8]). M14 ne
  touche ni `PositionSizer`, ni `DryRunResolutionWatcher`, ni les queries
  `/home`.
- **Triple garde-fou M3 + 4ᵉ M8** : M14 reste 100 % read-only (Data API +
  Gamma + DB locale). Aucune creds CLOB touchée.

### 1.4 Ce que change explicitement M14 (vue de haut)

| Module | Diff | Référence MA |
|---|---|---|
| [src/polycopy/discovery/scoring/v2/aggregator.py](../../src/polycopy/discovery/scoring/v2/aggregator.py) | Pondérations renormalisées (drop `timing_alpha`), bump version → `"v2.1"` | MA.1 + MA.8 |
| [src/polycopy/discovery/scoring/v2/normalization.py](../../src/polycopy/discovery/scoring/v2/normalization.py) | Nouvelle fonction `rank_normalize`. `winsorize_p5_p95` + `apply_pool_normalization` conservées (dépréciation) mais plus appelées par v2.1 | MA.2 |
| [src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py](../../src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py) | Variance minimale exigée + `median(Sortino, Calmar)` | MA.3 |
| [src/polycopy/discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py) | `_compute_brier` recalculé sur `P(YES)`, `_compute_zombie_ratio` filtre temporel <30j (avec fallback `RawPosition.opened_at`) | MA.4 + MA.6 |
| [src/polycopy/discovery/scoring/v2/factors/calibration.py](../../src/polycopy/discovery/scoring/v2/factors/calibration.py) | Baseline pool-mean cohérente (suppression `0.25` hardcodé pool_context) | MA.4 |
| [src/polycopy/discovery/orchestrator.py:651-653](../../src/polycopy/discovery/orchestrator.py#L651) | Suppression `brier_baseline_pool=0.25` hardcodé, calcul dynamique pool-mean | MA.4 |
| [src/polycopy/discovery/scoring/v2/factors/specialization.py](../../src/polycopy/discovery/scoring/v2/factors/specialization.py) | `1 - HHI` → `HHI` direct (signal positif) | MA.5 |
| [src/polycopy/config.py:691](../../src/polycopy/config.py#L691) | `Literal["v1", "v2"]` → `Literal["v1", "v2", "v2.1"]`. Default reste `"v1"`. Default `EVICTION_SCORE_MARGIN` 0.15 → 0.10. Nouveau flag `SCORING_V2_1_SHADOW_DAYS=14` | MA.7 + MA.8 |
| [src/polycopy/discovery/scoring/__init__.py](../../src/polycopy/discovery/scoring/__init__.py) | Registry `SCORING_VERSIONS_REGISTRY["v2.1"] = _compute_score_v2_1_wrapper` | MA.8 |
| [src/polycopy/discovery/dtos.py](../../src/polycopy/discovery/dtos.py) (`RawPosition`) | +1 champ optionnel `opened_at: datetime \| None` (pour MA.6 filtre temporel) | MA.6 |
| Tests | +28 unit + 2 intégration empirique | tous |

### 1.5 Pourquoi pas v2.2-DISCRIMINATING tout de suite

[Claude §4.2 v2.2-DISCRIMINATING](../deepsearch/claude_Architectural_Review_of_Scoring_Discovery_and_Latency_for_a_Single-Process_asyncio_Polymarket_Copy-Trader_(April_2026).md#42-v22-discriminating--polymarket-specific-with-internal-pnl-feedback)
propose une formule plus puissante : `0.30·internal_pnl + 0.25·informed_score
(Mitts-Ofir composite) + ...`. **3 raisons pour ne pas la livrer maintenant** :

1. **Internal PnL feedback (poids 0.30) exige 30j de copy data réelle**.
   M13 vient de débloquer la cristallisation realized_pnl (commit
   `d287fbc`). Il faut laisser couler 30j → MB.
2. **Sirolly wash cluster détection** (poids 0.15) = ~3 jours dev port
   Python du graph clustering algorithm → MF.
3. **MA est le bloquant immédiat**. Les 8 items MA sont **mathématiquement
   exigés** par les 5 défauts observés. Sans MA, MB / MF / MG empilent de
   la sophistication sur une base instable. **Foundation first**.

Roadmap consolidée : MA (M14, 3-4j) → MB (lifecycle internal_pnl, ~5-7j) →
MF (Mitts-Ofir + Sirolly, ~1-2 sem) → MG (CLV + Kelly + Kyle's λ, ~1 sem).
Cf. [docs/next/README.md](../next/README.md) pour le séquencement complet.

---

## 2. Scope / non-goals

### 2.1 Dans le scope

**MA.1 — Drop `timing_alpha` weight + renormaliser** :

- `_WEIGHT_TIMING_ALPHA: 0.20 → 0.0` dans
  [aggregator.py:45](../../src/polycopy/discovery/scoring/v2/aggregator.py#L45).
- 5 autres pondérations renormalisées **proportionnellement** (cf. décision
  **D7** infra) : `0.25/0.80, 0.20/0.80, 0.15/0.80, 0.10/0.80, 0.10/0.80` →
  `0.3125, 0.2500, 0.1875, 0.1250, 0.1250`. Somme = 1.0000 (vérifié par test).
- Fonction `compute_timing_alpha()` **conservée** (pour re-enable v2.2 ou
  via RTDS prix temps réel — MG / M16). Juste appelée avec poids 0.
- Garde-fou `assert sum(weights) == pytest.approx(1.0, abs=1e-6)` ajouté.

**MA.2 — Rank transform remplace winsorisation** :

- Nouvelle fonction
  [normalization.py::rank_normalize](../../src/polycopy/discovery/scoring/v2/normalization.py)
  `(values: list[float]) -> list[float]` retourne `rank(w) / N` ∈ [0, 1] avec
  interpolation **"average"** sur les ties (élimine le fixed-point trap
  "lower").
- `apply_pool_normalization` v2.1 utilise `rank_normalize(pool ∪ {wallet})`
  au lieu de `winsorize → rescale`.
- Préservation API : `winsorize_p5_p95` + ancien `apply_pool_normalization`
  **conservés** mais marqués deprecated dans le docstring (callers v2.1
  appellent la nouvelle fonction).

**MA.3 — Sortino sentinel fix + median combination** :

- Dans
  [risk_adjusted.py::_sortino_ratio](../../src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py#L73),
  remplacer `if downside_dev == 0.0: return _RATIO_CAP_SENTINEL` par :
  ```
  if pstdev(returns) < _MIN_VARIANCE_THRESHOLD:  # 1e-3
      return 0.0  # facteur inobservable, pas "skill inactif"
  if not downside or downside_dev == 0.0:
      return _sharpe_fallback(returns)  # corrélation r > 0.95 (Rollinger 2013)
  ```
- Dans `compute_risk_adjusted`, remplacer `0.6 * sortino + 0.4 * calmar`
  par `median([sortino, calmar])` — robuste au sentinel cluster (Claude C10).

**MA.4 — Brier P(YES) + baseline pool-mean cohérent** :

- Dans
  [metrics_collector_v2.py::_compute_brier](../../src/polycopy/discovery/metrics_collector_v2.py#L153),
  capturer la prob `YES_at_entry` au lieu du prix du side acheté :
  ```
  for p in resolved:
      yes_at_entry = p.avg_price if p.outcome == "YES" else (1.0 - p.avg_price)
      yes_won = (p.outcome == "YES" and p.cash_pnl > 0) or (p.outcome == "NO" and p.cash_pnl <= 0)
      sq_errors.append((float(yes_won) - yes_at_entry) ** 2)
  ```
- Dans
  [orchestrator.py:651-653](../../src/polycopy/discovery/orchestrator.py#L651),
  supprimer `brier_baseline_pool=0.25` hardcodé. Calculer dynamiquement :
  `baseline = mean([p.brier_at_pool_entry for p in all_pool_resolved_positions])`.
- Calibration formule reste `1 - brier_wallet / brier_baseline_pool` mais
  désormais sur même unité (P(YES) + pool-mean cohérent).
- Floor `brier_baseline_pool >= 0.10` (vs `0.15` actuel §11.5 M12) — pool
  trop homogène = sentinel anti-divergence.

**MA.5 — Flip HHI specialization** :

- Dans
  [specialization.py:32](../../src/polycopy/discovery/scoring/v2/factors/specialization.py#L32),
  remplacer `return max(0.0, min(1.0, 1.0 - metrics.hhi_categories))` par
  `return max(0.0, min(1.0, metrics.hhi_categories))`.
- Mise à jour docstring : "High HHI (concentration sur 1-2 catégories) =
  signal Mitts-Ofir insider-like. Reward, not penalty."
- Pondération **inchangée** (0.15 → renormalisée à 0.1875 post-MA.1). On
  inverse le sens, pas l'amplitude.

**MA.6 — `_compute_zombie_ratio` filtre temporel <30j implémenté** :

- Enrichir
  [discovery/dtos.py::RawPosition](../../src/polycopy/discovery/dtos.py)
  avec champ optionnel `opened_at: datetime | None = None`.
- `MetricsCollectorV2._fetch_raw_positions` essaye d'extraire `opened_at`
  du payload Data API `/positions` (champ `firstTradeTimestamp` si
  disponible). À défaut, fallback proxy = date du premier trade observé sur
  cet `(asset_id, wallet)` dans `detected_trades`.
- Dans
  [_compute_zombie_ratio](../../src/polycopy/discovery/metrics_collector_v2.py#L173),
  filtrer le dénominateur :
  ```
  cutoff = now - timedelta(days=30)
  eligible = [p for p in positions
              if float(p.initial_value) > 0
              and (p.opened_at is None or p.opened_at <= cutoff)]
  ```
- Décision **D5** : si `opened_at` reste `None` (ni source ni proxy) et
  position trop récente → exclure conservativement (under-score plutôt que
  sur-score).

**MA.7 — `EVICTION_SCORE_MARGIN` 0.10** :

- [config.py:800-808](../../src/polycopy/config.py#L800) : default `0.15` →
  `0.10`. Borne min `0.05` → `0.02` (autorise margin plus fin si pool calme).
- `.env.example` mise à jour avec commentaire :
  `EVICTION_SCORE_MARGIN=0.10  # 1σ empirique post-rank-transform v2.1`.
- Validator Pydantic : `Field(ge=0.02, le=0.30)` (vs `0.05, 0.50` actuel).
- Documentation §11.7 : re-mesurer empiriquement à J+7 post-ship et ajuster
  via H-EMP (cf. §14.4).

**MA.8 — Ship `SCORING_VERSION="v2.1"` + shadow config** :

- [config.py:691](../../src/polycopy/config.py#L691) :
  `Literal["v1", "v2"]` → `Literal["v1", "v2", "v2.1"]`. Default reste `"v1"`.
- Nouveau flag `scoring_v2_1_shadow_days: int [0, 90] = 14` (clone pattern
  `scoring_v2_shadow_days`).
- [scoring/__init__.py](../../src/polycopy/discovery/scoring/__init__.py) :
  ajouter `SCORING_VERSIONS_REGISTRY["v2.1"] = _compute_score_v2_1_wrapper`
  (wrapper distinct — appelle `compute_score_v2_1` qui consomme un
  `PoolContext` v2.1-aware avec `rank_normalize`).
- Décision **D6** : v2.1 **remplace v2** comme formule shadow vs v1. Le
  shadow compare v2.1 vs **v1**, pas v2.1 vs v2 (v2 = défaut écarté).
- Append-only strict : chaque row `trader_scores` porte `scoring_version`
  in `{"v1", "v2", "v2.1"}`. Aucun `UPDATE` rétroactif.
- Mettre à jour `CLAUDE.md §Conventions` avec la nouvelle version (cf. §10.1).

### 2.2 Hors scope explicites

- ❌ **Internal PnL feedback factor** (`internal_pnl_score = sigmoid(realized_copy_pnl_30d / $10)`) → migre en **MB** (nécessite 30j de données copy post-M13).
- ❌ **CLV (Closing Line Value)** : exige snapshots orderbook time-series → migre en **MG**.
- ❌ **Kelly proxy via conviction_sigma** : feature engineering complexe → **MG**.
- ❌ **Liquidity-adjusted ROI (Kyle's λ)** : exige depth historique → **MG**.
- ❌ **Wash cluster continuous score (Sirolly graph clustering)** : ~3 jours port Python → **MF**.
- ❌ **Mitts-Ofir composite informed_score** complet (timing_alpha vrai + market_hhi + size anomaly) : MA.5 inverse seulement le signe HHI, pas le composite → **MF**.
- ❌ **v2.3-LIGHTWEIGHT two-tier architecture** : hors roadmap actuelle, ré-évaluer post-MF.
- ❌ **Fenêtre 180j + exponential decay half-life 30j** : v3 future si besoin (déjà §14.1 spec M12).
- ❌ **Thompson Sampling Trend-Aware ranking** : approximation rank-based v2.1 suffisante (Claude §4.1 + §7.3 + Wang 2025). Migre en spec dédiée si besoin de dynamic rebalancing prouvé.
- ❌ **Dashboard stability metric** (std sur N cycles par wallet pour visualiser la stabilité v2.1) : migre en **MH**.
- ❌ **Fix Spearman rank /scoring display** (UX bug — affiche ranks pool entier au lieu de subset commun v1∩v2.1) : migre en **MH**.
- ❌ **Auto-detection wash cluster** : reste manuel via `WASH_CLUSTER_WALLETS` env var → **M17+**.
- ❌ **Alertes Telegram sur transitions v2.1 vs v1** : événements `gate_rejected` non émis (déjà décision M12).
- ❌ **Apify Leaderboard scraper** : reportable post-M14 si pool v2.1 reste < 50.
- ❌ **Backfill rétroactif des scores v2 historiques en v2.1** : viole versioning sacré. Les rows `scoring_version="v2"` restent inchangées.
- ❌ **Suppression du code `timing_alpha`** : la fonction `compute_timing_alpha` reste en place, juste avec poids 0 (re-enable propre v2.2 / via RTDS).
- ❌ **Migration Alembic** : aucune. La colonne `trader_scores.scoring_version: String(16)` accepte `"v2.1"` depuis migration 0006 M12.

---

## 3. User stories

### 3.1 Story A — Wallet locked à 0.45 sur 80 cycles débloqué (MA.2)

**Avant M14** (observation 2026-04-24) :

- Wallet `0x63d43bbb…a2f1` shadow depuis 12 jours.
- Scoring v2 : 80 cycles consécutifs → score = `0.450` strictement (pas
  `0.4499`, pas `0.4501` — exactement `0.450`).
- `EVICTION_SCORE_MARGIN=0.15` jamais dépassé puisque la valeur ne bouge
  pas.
- Wallet stuck en shadow alors qu'il est manifestement borderline (devrait
  promote ou demote selon évolution réelle).

**Diagnostic Claude C7** : `numpy.quantile(method='lower')` mappe les ties
au même rank et skip des valeurs. Combiné avec un pool de N=13 et
`EVICTION_HYSTERESIS_CYCLES=3` (= 18h out-of-band), la "band" elle-même
bouge avec la composition du pool → c'est un **fixed-point structurel**,
pas un bug.

**Après M14** (avec MA.2 rank transform) :

- Cycle T+0 : pool [0.31, 0.42, 0.45, 0.48, 0.51, 0.55, 0.62, 0.71]. Wallet
  `0x63d43bbb…` rang 3/8 → score normalisé = `3/8 = 0.375`.
- Cycle T+1 : un wallet sort du pool (eviction MB), nouveau wallet entre
  avec score brut 0.50. Pool [0.31, 0.42, 0.45, **0.50**, 0.51, 0.55, 0.62, 0.71].
  Wallet `0x63d43bbb…` rang 3/8 → score = `0.375` (inchangé localement).
- Cycle T+2 : une nouvelle position résolue augmente Sortino brut du wallet
  de 1.8 → 2.3. Rank passe 3 → 5. Score = `5/8 = 0.625`. **Le wallet
  bouge proportionnellement à son changement réel**, sans drift artificiel.

### 3.2 Story B — Tous les scores +0.10 disparaît (MA.1)

**Avant M14** :

- Tous les wallets ont `timing_alpha_weighted = 0.5` (placeholder M12 décision D3).
- Pool `[0.5, 0.5, 0.5, ..., 0.5]` → `winsorize_p5_p95 → (0.5, 0.5)` →
  `apply_pool_normalization` retourne `0.5` sentinel pour tous.
- Score final = `0.20 × 0.5 + 0.80 × (autres facteurs)` = `0.10 +
  0.80 × X`. **+0.10 uniforme** sur chaque score.
- Le seuil `SCORING_PROMOTION_THRESHOLD=0.65` est effectivement `0.55` sur
  les 5 facteurs utiles. Tous les gates de décision sont décalés de +0.10
  ([H-008] audit).

**Après M14** (avec MA.1 drop poids) :

- Pondérations : 0.3125 / 0.2500 / 0.1875 / 0.1250 / 0.1250 (somme = 1.0).
- `compute_timing_alpha` toujours appelée (return value clippé), mais le
  facteur n'est **pas multiplié** dans l'aggregator (poids = 0).
- Score final discrimine sur les 5 facteurs réellement informatifs.
- Promotion threshold revient à sa valeur intuitive (un wallet à 0.65
  réel = top-tier, pas top-tier + bias).

### 3.3 Story C — Zombie holder n'usurpe plus le top du pool (MA.3)

**Avant M14** :

- Wallet `0xZOMBIE…` : 51 trades sur l'élection 2024, profitable, plus
  de trades depuis 6 mois. Equity curve plate (pas de mouvement). 51 trades
  ≥ 50 → passe le gate `trade_count_90d` (cold-start mode v1.1).
- `_sortino_ratio(returns)` : `downside = []` → cap `_RATIO_CAP_SENTINEL = 3.0`.
- `_calmar_ratio(curve)` : `max_dd < 1e-4` → cap `3.0`.
- `risk_adjusted = 0.6 × 3.0 + 0.4 × 3.0 = 3.0` brut.
- Après normalisation pool, ce wallet et ses pairs zombies clusterent au
  top → score `risk_adjusted` ≈ 1.0 → score final ≈ 0.85+.
- `DecisionEngine` promote à `active`. Bot copie un wallet **inactif** =
  zéro trade détecté = capital gaspillé en queue.

**Après M14** (avec MA.3 variance min + median) :

- `pstdev(returns) < 1e-3` (curve plate quasi parfaite) → `_sortino_ratio`
  retourne **0.0** (pas 3.0 sentinel).
- `_calmar_ratio` : pas de drawdown observé → encore sentinel 3.0.
- `risk_adjusted = median([0.0, 3.0]) = 1.5` brut. Après rank-normalisation
  pool, ce wallet est rangé **mid-pack**, pas top.
- Wallet reste shadow / never promoted. Capital alloué aux wallets
  réellement actifs.

### 3.4 Story D — HHI specialization récompense l'insider (MA.5)

**Avant M14** :

- Wallet `0xMagamyman-like…` : 100 % volume sur 2 catégories (Politics
  & Geopolitics). HHI = 0.50². + 0.50² = 0.50.
- Wallet `0xDIVERSIFIED…` : volume égal sur 5 catégories. HHI = 5 × 0.20² = 0.20.
- v2 : `specialization = 1 - HHI` → insider = 0.50, diversifié = 0.80.
- Insider **pénalisé**. Pondération 0.15 → -0.045 sur le score insider vs
  diversifié.

**Après M14** (avec MA.5 flip) :

- v2.1 : `specialization = HHI` direct → insider = 0.50, diversifié = 0.20.
- Insider rangé **plus haut** (0.50 > 0.20) → bénéfice +0.045 vs avant
  fix → +0.090 swing total.
- Mitts-Ofir empirical (Polymarket) : insider wallets réalisent 69.9 % WR
  > 60σ above chance (citation Harvard Corpgov 2026). **On veut copier
  ce pattern, pas l'éviter**.

### 3.5 Story E — Eviction enfin déclenchée (MA.7)

**Avant M14** :

- Pool active : 8 wallets, scores `[0.42, 0.51, 0.55, 0.58, 0.61, 0.63, 0.65, 0.68]`.
- Pool shadow : 1 candidat à `0.55` (pas le candidat top, mais en
  hystérésis 3 cycles avec stabilité — sain).
- Worst active = `0.42`. Delta candidat - worst = `0.55 - 0.42 = 0.13`.
- `EVICTION_SCORE_MARGIN=0.15` : **eviction NE se déclenche PAS** (0.13 < 0.15).
- Le worst active à 0.42 reste, le candidat à 0.55 attend indéfiniment.
- Sur les 280 cycles observés, **0 eviction triggered** (Claude §3.1).

**Après M14** (avec MA.7 margin 0.10) :

- Même pool, même candidat. Delta = 0.13, margin = **0.10**.
- Eviction **se déclenche** : worst active 0.42 → `sell_only`, candidat
  0.55 → `active`. Cascade 1 swap (cf. spec M5_bis EC-2).
- Pool rotation enfin fonctionnelle. Capital alloué aux meilleurs wallets
  observables.

### 3.6 Story F — Couverture v2.1 ≥ v1 × 0.8 à J+14 (impact bundle MA.1-MA.5)

**Avant M14** : v2 score 13/50 = 26 % du pool v1. Cause majeure : facteur
`risk_adjusted` retourne 0 trop souvent (curve trop courte ou variance
sentinel collée à 1.0 → écart pool dégénéré → tout le monde
↦ 0.5).

**Après M14** : avec rank transform (MA.2), variance fix (MA.3), zombie
filtre (MA.6), brier coherent (MA.4) → v2.1 score plus de wallets
interprétables → coverage ≥ 40/50 attendu (Claude §4.1 + §7.3 prediction).

---

## 4. Architecture

### 4.1 Diagramme bundle MA.1 → MA.8

```
                          ┌─────────────────────────────────────────────────┐
                          │  DiscoveryOrchestrator._run_one_cycle (M5+M12)   │
                          └────┬────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  _build_pool_context_v2_1                              (MA.4)        │
│  ──────────────────────                                              │
│  - Itère candidate_wallets, fetch metrics_v2 partiels                │
│  - Calcule brier_baseline_pool DYNAMIQUEMENT                          │
│    (mean Brier P(YES) sur toutes positions résolues)                  │
│  - Floor 0.10 (vs 0.15 actuel)                                        │
│  - Suppression hardcoded 0.25                                         │
└──────┬────────────────────────────────────────────────────────────────┘
       │ PoolContext v2.1-aware
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  MetricsCollectorV2.collect(wallet)                       (MA.4+6)   │
│  ──────────────────────                                              │
│  - _compute_brier(positions) : YES_at_entry au lieu de side_bought   │
│  - _compute_zombie_ratio(positions) : filtre <30j via                │
│    p.opened_at (Data API /positions firstTradeTimestamp)             │
│  - Reste inchangé (Sortino raw, Calmar, HHI cat, sizing CV...)       │
└──────┬────────────────────────────────────────────────────────────────┘
       │ TraderMetricsV2 (cleaned)
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  compute_score_v2_1(metrics, pool_context)              (aggregator) │
│  ──────────────────────                                              │
│  raw = (                                                             │
│    risk_adjusted= median(sortino, calmar)        (MA.3)              │
│    calibration  = 1 - brier / brier_baseline    (MA.4)               │
│    timing_alpha = compute_timing_alpha()         (kept, weight=0)    │
│    specialization = HHI direct                   (MA.5)              │
│    consistency  = unchanged                                          │
│    discipline   = unchanged                                          │
│  )                                                                   │
│  normalized = rank_normalize(pool ∪ {wallet})    (MA.2)              │
│  final = (                                                           │
│    0.3125·risk_adjusted +                                            │
│    0.2500·calibration +                                              │
│    0.1875·specialization +                                           │
│    0.1250·consistency +                                              │
│    0.1250·discipline +                                               │
│    0.0   ·timing_alpha                          (MA.1)               │
│  )                                                                   │
│  return ScoreV2Breakdown(score=final, scoring_version="v2.1")  (MA.8)│
└──────┬────────────────────────────────────────────────────────────────┘
       │ float ∈ [0, 1]
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  trader_scores INSERT scoring_version="v2.1"      (MA.8 append-only) │
└──────┬────────────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  EvictionScheduler (M5_bis, opt-in EVICTION_ENABLED=true)             │
│  EVICTION_SCORE_MARGIN=0.10 (default M14, MA.7)                       │
└───────────────────────────────────────────────────────────────────────┘
```

### 4.2 Fichiers touchés (récapitulatif)

| Fichier | Type changement | Lignes estimées |
|---|---|---|
| [src/polycopy/discovery/scoring/v2/aggregator.py](../../src/polycopy/discovery/scoring/v2/aggregator.py) | Rebalance pondérations + bump version + appel `rank_normalize` | +25 / -10 |
| [src/polycopy/discovery/scoring/v2/normalization.py](../../src/polycopy/discovery/scoring/v2/normalization.py) | Nouvelle fonction `rank_normalize` | +35 / -0 |
| [src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py](../../src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py) | Variance min + Sharpe fallback + `median()` | +30 / -8 |
| [src/polycopy/discovery/scoring/v2/factors/calibration.py](../../src/polycopy/discovery/scoring/v2/factors/calibration.py) | Adapter à nouveau Brier + floor pool baseline | +10 / -5 |
| [src/polycopy/discovery/scoring/v2/factors/specialization.py](../../src/polycopy/discovery/scoring/v2/factors/specialization.py) | Flip `1 - HHI` → `HHI` | +3 / -3 |
| [src/polycopy/discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py) | `_compute_brier` P(YES), `_compute_zombie_ratio` filtre <30j | +35 / -15 |
| [src/polycopy/discovery/dtos.py](../../src/polycopy/discovery/dtos.py) (`RawPosition`) | +1 champ optionnel `opened_at` | +3 / -0 |
| [src/polycopy/discovery/orchestrator.py](../../src/polycopy/discovery/orchestrator.py) | `_build_pool_context_v2_1` + flag `scoring_v2_1` | +30 / -5 |
| [src/polycopy/discovery/scoring/__init__.py](../../src/polycopy/discovery/scoring/__init__.py) | Registry entry `"v2.1"` | +5 / -0 |
| [src/polycopy/config.py](../../src/polycopy/config.py) | Literal extended, default margin 0.10, +1 flag shadow_days | +15 / -3 |
| [.env.example](../../.env.example) | Bloc M14 documenté | +12 / -0 |
| Tests unit | +28 tests ciblés | +600 / -50 |
| Test intégration H-EMP | +2 tests | +200 / -0 |
| `scripts/validate_ma_hypotheses.py` | Nouveau — H-EMP-1 + H-EMP-2 | +120 / -0 |

### 4.3 Dépendances avec autres milestones

- **M5 / M5_bis / M5_ter** : aucune modification structurelle. Seul le
  default `EVICTION_SCORE_MARGIN` change (recalibration empirique).
- **M11 (latency)** : aucun. Le scoring ne touche pas la couche WS / cache
  Gamma adaptive.
- **M12 (scoring v2)** : v2 reste **présente** dans le registry (audit
  trail intact, append-only). v2.1 s'ajoute en parallèle, devient la
  formule shadow contre v1.
- **M13 (dry-run observability)** : aucun, M13 n'a pas touché au scoring.

---

## 5. Algorithmes

### 5.1 MA.1 — Drop `timing_alpha` weight (aggregator.py)

**Contexte** : [aggregator.py:43-48](../../src/polycopy/discovery/scoring/v2/aggregator.py#L43)
définit 6 pondérations. M14 met `_WEIGHT_TIMING_ALPHA = 0.0` et **renormalise
proportionnellement** les 5 autres :

```python
# src/polycopy/discovery/scoring/v2/aggregator.py — MA.1 + MA.8

# Pondérations renormalisées proportionnellement après drop timing_alpha (M14).
# Conservation des proportions relatives M12 (synthèse §1.2 + décision D7) :
#   risk_adjusted   : 0.25 / 0.80 = 0.3125
#   calibration     : 0.20 / 0.80 = 0.2500
#   specialization  : 0.15 / 0.80 = 0.1875
#   consistency     : 0.10 / 0.80 = 0.1250
#   discipline      : 0.10 / 0.80 = 0.1250
#   timing_alpha    : 0.0 (gardée 0 jusqu'à re-enable v2.2 / RTDS)
_WEIGHT_RISK_ADJUSTED: float = 0.3125
_WEIGHT_CALIBRATION: float = 0.2500
_WEIGHT_TIMING_ALPHA: float = 0.0
_WEIGHT_SPECIALIZATION: float = 0.1875
_WEIGHT_CONSISTENCY: float = 0.1250
_WEIGHT_DISCIPLINE: float = 0.1250

# Garde-fou : la somme doit être == 1.0 ± epsilon. Vérifié au boot par
# DiscoveryOrchestrator + par test unit `test_aggregator_weights_sum_to_one`.
_WEIGHTS_SUM_TOLERANCE: float = 1e-6
assert abs(
    _WEIGHT_RISK_ADJUSTED + _WEIGHT_CALIBRATION + _WEIGHT_TIMING_ALPHA
    + _WEIGHT_SPECIALIZATION + _WEIGHT_CONSISTENCY + _WEIGHT_DISCIPLINE - 1.0
) < _WEIGHTS_SUM_TOLERANCE, "Pondérations v2.1 ne somment pas à 1.0"


def compute_score_v2_1(
    metrics: TraderMetricsV2,
    pool_context: PoolContext,
) -> ScoreV2Breakdown:
    """Aggregator v2.1-ROBUST. Diff vs v2 :

    - timing_alpha : appelé pour le breakdown audit, mais poids=0 dans le mix.
    - normalisation : `rank_normalize` au lieu de `apply_pool_normalization`
      (cf. §5.2 MA.2).
    - risk_adjusted : `median(sortino, calmar)` au lieu de moyenne pondérée
      (cf. §5.3 MA.3).
    - calibration : Brier P(YES) + baseline pool-mean dynamique (cf. §5.4 MA.4).
    - specialization : HHI direct, pas inversé (cf. §5.5 MA.5).

    Retourne `scoring_version="v2.1"` (cf. §5.8 MA.8).
    """
    raw = RawSubscores(
        risk_adjusted=compute_risk_adjusted(metrics),  # MA.3
        calibration=compute_calibration(metrics, pool_context.brier_baseline_pool),  # MA.4
        timing_alpha=compute_timing_alpha(metrics),  # kept, weight=0
        specialization=compute_specialization(metrics),  # MA.5
        consistency=compute_consistency(metrics),  # unchanged
        discipline=compute_discipline(metrics),  # unchanged
    )
    # MA.2 : rank_normalize sur pool ∪ {wallet}. Cf. §5.2.
    normalized = ScoringNormalizedSubscores(
        risk_adjusted=rank_normalize_one(raw.risk_adjusted, pool_context.risk_adjusted_pool),
        calibration=rank_normalize_one(raw.calibration, pool_context.calibration_pool),
        timing_alpha=rank_normalize_one(raw.timing_alpha, pool_context.timing_alpha_pool),
        specialization=rank_normalize_one(raw.specialization, pool_context.specialization_pool),
        consistency=rank_normalize_one(raw.consistency, pool_context.consistency_pool),
        discipline=rank_normalize_one(raw.discipline, pool_context.discipline_pool),
    )
    final = (
        _WEIGHT_RISK_ADJUSTED * normalized.risk_adjusted
        + _WEIGHT_CALIBRATION * normalized.calibration
        + _WEIGHT_TIMING_ALPHA * normalized.timing_alpha   # = 0
        + _WEIGHT_SPECIALIZATION * normalized.specialization
        + _WEIGHT_CONSISTENCY * normalized.consistency
        + _WEIGHT_DISCIPLINE * normalized.discipline
    )
    return ScoreV2Breakdown(
        wallet_address=metrics.base.wallet_address,
        score=max(0.0, min(1.0, final)),
        raw=raw,
        normalized=normalized,
        brier_baseline_pool=pool_context.brier_baseline_pool,
        scoring_version="v2.1",  # MA.8
    )
```

**Note** : `compute_score_v2` (M12) reste exporté inchangé. Le registry M14
ajoute `compute_score_v2_1` en parallèle. Audit trail v2 préservé.

### 5.2 MA.2 — `rank_normalize` (normalization.py)

**Contrat** : remplacer winsorisation p5-p95 par rank transform `rank(w) /
N` avec interpolation `"average"` sur les ties.

```python
# src/polycopy/discovery/scoring/v2/normalization.py — MA.2

from __future__ import annotations
from statistics import mean


def rank_normalize(values: list[float]) -> list[float]:
    """Rank transform avec interpolation 'average' pour les ties.

    Pour chaque valeur dans `values` :
    1. Calculer son rang (1-indexé) parmi `sorted(values)`.
    2. Si plusieurs valeurs identiques (ties), retourner la **moyenne** des
       rangs occupés (élimine le fixed-point trap "lower").
    3. Diviser par N pour obtenir un score ∈ ]0, 1].

    Pure function. Déterministe. Stable cycle-to-cycle (les ranks ne bougent
    que par swap local).

    Pool vide → retourne [].
    Pool avec 1 seul élément → retourne [1.0] (sentinel : 1 wallet = top).

    Exemples :
        >>> rank_normalize([3.0, 1.0, 2.0])
        [1.0, 0.333..., 0.666...]
        >>> rank_normalize([1.0, 1.0, 1.0, 4.0])
        [0.5, 0.5, 0.5, 1.0]   # 3 valeurs au rang moyen 2 → 2/4=0.5
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0]

    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks: list[float] = [0.0] * n

    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        # Rangs [i+1, ..., j+1] tous égaux → moyenne arithmétique.
        avg_rank = mean(range(i + 1, j + 2))
        for k in range(i, j + 1):
            original_idx = indexed[k][0]
            ranks[original_idx] = avg_rank / n
        i = j + 1

    return ranks


def rank_normalize_one(wallet_value: float, pool_values: list[float]) -> float:
    """Helper convenience : retourne le rank du wallet_value dans le pool ∪ {wallet}.

    Préserve l'API consommateur (callers passent valeur + pool comme avant
    `apply_pool_normalization`).

    Pool vide → wallet seul = 1.0.
    Pool dégénéré (toutes valeurs identiques + wallet identique) → 0.5
    (sentinel pool-flat).
    """
    extended = pool_values + [wallet_value]
    if not extended:
        return 0.5
    ranks = rank_normalize(extended)
    return ranks[-1]  # Le dernier élément correspond au wallet_value


# --- Deprecated mais conservé (M12 compat tests) ---

def winsorize_p5_p95(values: list[float]) -> tuple[float, float]:
    """⚠️ Deprecated v2.1 : utiliser `rank_normalize` (MA.2).

    Conservé pour :
    - Backward-compat avec `compute_score_v2` (M12, registry entry "v2").
    - Tests unitaires existants.
    """
    # Body inchangé M12 — voir spec M12 §3.8.
    ...


def apply_pool_normalization(wallet_value: float, pool_values: list[float]) -> float:
    """⚠️ Deprecated v2.1 : utiliser `rank_normalize_one` (MA.2).

    Conservé pour backward-compat M12 v2 registry.
    """
    # Body inchangé M12.
    ...
```

**Justification décision D1** (rank vs log-transform — Gemini propose
log) : rank est **strictement plus robuste** sur small N et **élimine** le
fixed-point trap C7 par construction. Log-transform compresse les outliers
mais préserve la dépendance à p95 cyclique. Cf. Claude §4.1.

### 5.3 MA.3 — Sortino sentinel fix + median (risk_adjusted.py)

```python
# src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py — MA.3

from statistics import median, mean, pstdev

# Variance min sous laquelle un wallet est "inobservable" (pas génial).
_MIN_VARIANCE_THRESHOLD: float = 1e-3
# Cap sentinel conservé pour Calmar (cf. ci-dessous), mais Sortino l'évite.
_RATIO_CAP_SENTINEL: float = 3.0


def compute_risk_adjusted(metrics: TraderMetricsV2) -> float:
    """Combinaison robuste Sortino + Calmar via `median()`.

    MA.3 changes :
    1. Variance minimale `pstdev(returns) > 1e-3` exigée. Sinon → 0.0.
       Évite le bug "zombie sentinel cluster" ([H-009]).
    2. Si downside vide mais variance OK → fallback Sharpe (corrélation
       Sharpe/Sortino r > 0.95, Rollinger 2013).
    3. Combinaison `median(sortino, calmar)` au lieu de
       `0.6 × sortino + 0.4 × calmar`. Robuste au sentinel cluster
       (Claude C10).

    Pure function — aucun I/O, aucun state.
    """
    curve = list(metrics.monthly_equity_curve)
    if len(curve) < _MIN_CURVE_POINTS:
        return 0.0

    returns = _daily_returns(curve)
    if not returns:
        return 0.0

    # MA.3 critical : variance minimale pour considérer le facteur observable.
    if pstdev(returns) < _MIN_VARIANCE_THRESHOLD:
        return 0.0  # curve plate = pas de skill mesurable, pas "skill inactif"

    sortino = _sortino_ratio_robust(returns, risk_free_rate=0.0)
    calmar = _calmar_ratio(curve, returns)
    # MA.3 : median au lieu de moyenne pondérée. Robust to sentinel cluster.
    return median([sortino, calmar])


def _sortino_ratio_robust(returns: list[float], *, risk_free_rate: float) -> float:
    """Sortino avec fallback Sharpe quand downside vide mais variance présente.

    Cas :
    - Variance < threshold : caller a déjà early-return 0.0.
    - Downside vide (pas de returns négatifs) ET variance présente →
      `mean / pstdev(all_returns)` (Sharpe).
    - Downside présent → Sortino classique = `mean / pstdev(downside)`.
    """
    if not returns:
        return 0.0
    mean_ret = mean(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        # MA.3 : pas de downside mais variance OK → Sharpe fallback.
        # Pas le sentinel 3.0 qui dominerait la winsorisation.
        return (mean_ret - risk_free_rate) / pstdev(returns)
    downside_dev = pstdev(downside) if len(downside) > 1 else abs(downside[0])
    if downside_dev == 0.0:
        return (mean_ret - risk_free_rate) / pstdev(returns)
    return (mean_ret - risk_free_rate) / downside_dev


def _calmar_ratio(curve: list[float], returns: list[float]) -> float:
    """Calmar = annualized_return / max_drawdown.

    Inchangé MA.3. Le sentinel `_RATIO_CAP_SENTINEL` reste dans Calmar
    (drawdown=0 sur curve plate est legitimately ratio-undefined). C'est
    le `median()` aval qui absorbe cette singularité.
    """
    if not returns or not curve:
        return 0.0
    annualized_ret = mean(returns) * 365.0
    max_dd = _max_drawdown(curve)
    if max_dd < 1e-4:
        return _RATIO_CAP_SENTINEL
    return annualized_ret / max_dd
```

**Cas limites couverts par tests §9.B** :
- `pstdev < 1e-3` (curve plate) → `0.0`.
- 5 wallets avec downside vide mais variance OK → fallback Sharpe → ranking
  réaliste (pas top-cluster).
- 1 wallet avec downside non-vide, 4 avec downside vide → median(downside,
  fallback) discrimine entre les deux populations.

### 5.4 MA.4 — Brier P(YES) + baseline pool-mean (metrics_collector_v2.py + calibration.py + orchestrator.py)

**Étape 1 — `_compute_brier` accepte un mapping `outcome` (YES/NO)** :

```python
# src/polycopy/discovery/metrics_collector_v2.py — MA.4

def _compute_brier(positions: list[RawPosition]) -> float | None:
    """Brier score sur P(YES) (Gneiting-Raftery 2007 strictly proper).

    Pour chaque position résolue :
    1. Calculer `yes_at_entry` (probabilité YES à l'instant de l'entrée).
       - Si position était BUY YES à $0.40 : `yes_at_entry = 0.40`.
       - Si position était BUY NO à $0.60 : `yes_at_entry = 1 - 0.60 = 0.40`.
    2. Déterminer si YES a gagné (`yes_won` ∈ {0, 1}).
       - `outcome="YES" AND cash_pnl > 0` → YES won.
       - `outcome="NO" AND cash_pnl <= 0` → YES won (le wallet a perdu en
         pariant NO, donc YES est sorti).
       - autres cas → YES lost.
    3. Brier = mean(`(yes_won - yes_at_entry) ** 2`).

    Plus fiable sur tous types de marchés (binaire OU neg_risk).
    Returns None si < `_BRIER_MIN_RESOLVED` positions résolues.
    """
    resolved = [p for p in positions if p.is_resolved]
    if len(resolved) < _BRIER_MIN_RESOLVED:
        return None
    sq_errors: list[float] = []
    for p in resolved:
        # MA.4 : convertir le prix d'entrée en P(YES) selon le side acheté.
        if p.outcome == "YES":
            yes_at_entry = float(p.avg_price)
            yes_won = float(p.cash_pnl) > 0
        elif p.outcome == "NO":
            yes_at_entry = 1.0 - float(p.avg_price)
            yes_won = float(p.cash_pnl) <= 0  # NO perd ⇒ YES gagne
        else:
            # Outcome inconnu (rare, marché invalide) → skip défensif.
            continue
        sq_errors.append((float(yes_won) - yes_at_entry) ** 2)
    if not sq_errors:
        return None
    return mean(sq_errors)
```

**Étape 2 — `_build_pool_context_v2_1` calcule `brier_baseline_pool` dynamiquement** :

```python
# src/polycopy/discovery/orchestrator.py — MA.4

async def _build_pool_context_v2_1(
    self,
    candidate_wallets: list[str],
) -> PoolContext:
    """Pool context v2.1-aware (MA.4 : brier_baseline pool-mean dynamique).

    Diff M12 :
    - Pas de `brier_baseline_pool=0.25` hardcodé. Calculé via mean Brier
      P(YES) sur toutes les positions résolues du pool actuel.
    - Floor `0.10` (vs `0.15` actuel — pool trop homogène = sentinel).
    - Tous les autres champs (`risk_adjusted_pool`, `calibration_pool`, ...)
      collectés via `MetricsCollectorV2.collect` standard.
    """
    metrics_v2_list: list[TraderMetricsV2] = []
    all_resolved_positions: list[RawPosition] = []
    for wallet in candidate_wallets:
        # Lazy fetch — réutilise cache MetricsCollectorV2.
        m = await self._metrics_collector_v2.collect(wallet)
        metrics_v2_list.append(m)
        all_resolved_positions.extend(m._raw_positions or [])  # via attr exposé

    # MA.4 : baseline = mean Brier P(YES) sur le pool entier.
    pool_brier = _compute_brier(all_resolved_positions)
    if pool_brier is None or pool_brier < 0.10:
        # Floor sentinel : pool trop homogène, fallback heuristique 0.20
        # (climatological forecast moyen Polymarket per Polymarket native
        # leaderboard, cf. Perplexity §A2 = 0.187).
        pool_brier = 0.20

    return PoolContext(
        risk_adjusted_pool=[m.sortino_90d for m in metrics_v2_list if m.sortino_90d != 0.0],
        # ⚠️ ATTENTION : on stocke maintenant le sortino_brut, pas
        # `compute_risk_adjusted` raw — incompat M12. Cf. note implémentation §14.2.
        calibration_pool=[m.brier_90d for m in metrics_v2_list if m.brier_90d is not None],
        timing_alpha_pool=[m.timing_alpha_weighted for m in metrics_v2_list],
        specialization_pool=[m.hhi_categories for m in metrics_v2_list],
        consistency_pool=[m.monthly_pnl_positive_ratio for m in metrics_v2_list],
        discipline_pool=[m.zombie_ratio for m in metrics_v2_list],  # utilisé mod _by_ aggregator
        brier_baseline_pool=pool_brier,
    )
```

**Étape 3 — `compute_calibration` factor inchangé** (formule
`1 - brier / brier_baseline_pool`), mais maintenant Brier et baseline sur
même unité (P(YES)).

### 5.5 MA.5 — Flip HHI specialization

```python
# src/polycopy/discovery/scoring/v2/factors/specialization.py — MA.5

def compute_specialization(metrics: TraderMetricsV2) -> float:
    """Retourne `HHI_categories` direct, **pas inversé**.

    MA.5 change : `1 - HHI` → `HHI` (Mitts-Ofir 2026, Claude C9).

    Justification : Insider wallets concentrent sur 1-2 catégories Gamma
    (HHI → 1.0). Récompenser la concentration, pas la pénaliser.

    HHI ∈ [0, 1] par construction. Aucun clip nécessaire mais conservé
    en défense en profondeur.

    Cf. Mitts & Ofir 2026 (Polymarket informed trading, 69.9% WR > 60σ
    above chance) : https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/
    """
    return max(0.0, min(1.0, metrics.hhi_categories))
```

**Note** : changement minimal **mais critique**. Inverser le signe d'un
facteur change le ranking de **l'ensemble du pool** sur cette dimension.
Le smoke test §12.4 doit explicitement valider que le top-10 v2.1 contient
plus de high-HHI wallets que le top-10 v2 sur la même base de candidats.

### 5.6 MA.6 — `_compute_zombie_ratio` filtre temporel <30j

**Étape 1 — Enrichir `RawPosition`** :

```python
# src/polycopy/discovery/dtos.py — MA.6

class RawPosition(BaseModel):
    model_config = ConfigDict(frozen=True)
    # ... champs existants ...
    opened_at: datetime | None = None  # MA.6 : optionnel, fallback None
```

**Étape 2 — Implémenter le filtre dans `_compute_zombie_ratio`** :

```python
# src/polycopy/discovery/metrics_collector_v2.py — MA.6

from datetime import datetime, timedelta, timezone

_ZOMBIE_RECENCY_DAYS: int = 30  # MA.6 : aligné avec docstring M12 §3.7


def _compute_zombie_ratio(
    positions: list[RawPosition],
    *,
    now: datetime | None = None,
) -> float:
    """Proportion capital immobilisé dans des positions "zombies" (Gemini §1.1).

    MA.6 : filtre temporel <30j enfin implémenté.

    - Position zombie : `current_value / initial_value < 2%` ET
      `is_resolved=False` (jamais liquidée).
    - **Excluded du dénominateur** : positions ouvertes depuis < 30j (récentes,
      pas pénaliser injustement les wallets nouveaux).
    - **Décision D5** : si `opened_at` absent ET position dans la fenêtre
      potentiellement récente → exclure conservativement (under-score plutôt
      que sur-score).

    Pure function (`now` injectable pour test reproductibilité).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=_ZOMBIE_RECENCY_DAYS)

    eligible = []
    for p in positions:
        if float(p.initial_value) <= 0:
            continue
        # MA.6 : filtre temporel sur opened_at.
        if p.opened_at is not None:
            if p.opened_at > cutoff:
                continue  # position trop récente, exclue
        else:
            # D5 : sans opened_at, exclusion conservatrice → on n'inclut pas
            # dans le dénominateur. Le wallet nouvellement découvert ne sera
            # jamais zombie tant qu'on n'a pas son age inféré.
            # (Trade-off : sous-estime zombie_ratio des wallets sans data —
            # conservateur, préfère under-score que sur-score.)
            continue
        eligible.append(p)

    if not eligible:
        return 0.0
    capital_total = sum(float(p.initial_value) for p in eligible)
    zombies = [
        p
        for p in eligible
        if not p.is_resolved
        and float(p.current_value) < _ZOMBIE_CURRENT_VALUE_PCT * float(p.initial_value)
    ]
    capital_zombie = sum(float(p.initial_value) for p in zombies)
    if capital_total == 0:
        return 0.0
    return capital_zombie / capital_total
```

**Étape 3 — Source `opened_at` dans `MetricsCollectorV2._fetch_raw_positions`** :

```python
# src/polycopy/discovery/metrics_collector_v2.py — MA.6 (suite)

async def _fetch_raw_positions(self, wallet: str) -> list[RawPosition]:
    """Fetch /positions Data API + tente d'extraire `opened_at`.

    Source primaire : champ `firstTradeTimestamp` du payload Data API
    `/positions?user=<addr>`. Si absent, fallback secondaire :
    requête `detected_trades.first(asset_id=A, wallet=W).timestamp`
    (proxy raisonnable mais bruité — un wallet copié récemment a son
    `detected_trades` plus jeune que sa position réelle).

    Si les deux échouent → `opened_at = None` → exclu zombie_ratio par D5.
    """
    raw = await self._data_api.get_positions(wallet)
    positions: list[RawPosition] = []
    for r in raw:
        opened_at = self._extract_opened_at(r) or await self._fallback_opened_at(
            wallet, r.get("asset"),
        )
        positions.append(RawPosition(
            # ... mappings existants ...
            opened_at=opened_at,
        ))
    return positions


def _extract_opened_at(self, payload: dict) -> datetime | None:
    """Cherche un timestamp d'ouverture dans le payload Data API.

    Champs essayés (par ordre, premier non-null gagne) :
    - `firstTradeTimestamp` (epoch seconds)
    - `openedAt` (ISO 8601)
    - `entryTimestamp` (epoch seconds)
    """
    for key, parser in [
        ("firstTradeTimestamp", _parse_epoch),
        ("openedAt", _parse_iso),
        ("entryTimestamp", _parse_epoch),
    ]:
        if value := payload.get(key):
            try:
                return parser(value)
            except (TypeError, ValueError):
                continue
    return None
```

### 5.7 MA.7 — `EVICTION_SCORE_MARGIN` 0.10 (config.py)

```python
# src/polycopy/config.py — MA.7

eviction_score_margin: float = Field(
    0.10,  # MA.7 : 0.15 → 0.10 (≈ 1σ empirique post-rank-transform v2.1)
    ge=0.02,  # MA.7 : était 0.05 — autorise margin plus fin sur pool calme
    le=0.30,  # MA.7 : était 0.50 — au-delà, eviction trop conservatrice
    description=(
        "Delta minimum score(candidat) - score(worst_active) requis pour "
        "déclencher une eviction. Recalibrée v2.1 : 0.10 ≈ 1σ empirique du "
        "score distribution après rank-transform (MA.2 + Claude §3.1). "
        "Re-mesurer empiriquement à J+7 post-ship M14 et ajuster via H-EMP-2."
    ),
)
```

**Mise à jour `.env.example`** :

```dotenv
# --- M5_bis : eviction (recalibration M14, MA.7) ---
# 0.10 = 1σ empirique post-rank-transform v2.1. Re-mesurer post-ship.
# EVICTION_SCORE_MARGIN=0.10
```

### 5.8 MA.8 — Ship `SCORING_VERSION="v2.1"` (config.py + scoring/__init__.py + aggregator.py)

```python
# src/polycopy/config.py — MA.8

scoring_version: Literal["v1", "v2", "v2.1"] = Field(
    "v1",
    description=(
        "Version de la formule de scoring. M14 ajoute 'v2.1' (M14 spec). "
        "Default 'v1' inchangé. Loggée + écrite avec chaque score pour "
        "reproductibilité (versioning sacré, append-only)."
    ),
)

# Nouveau flag M14 (clone pattern scoring_v2_shadow_days M12).
scoring_v2_1_shadow_days: int = Field(
    14,
    ge=0,
    le=90,
    description=(
        "Durée de coexistence v1/v2.1 en shadow. Pendant la fenêtre, v2.1 "
        "calcule + écrit trader_scores (scoring_version='v2.1') mais NE "
        "PILOTE PAS DecisionEngine. 0 = pas de calcul parallèle. M14: "
        "v2.1 remplace v2 comme formule shadow vs v1 (D6)."
    ),
)
```

```python
# src/polycopy/discovery/scoring/__init__.py — MA.8

from polycopy.discovery.scoring.v2.aggregator import (
    _compute_score_v2_wrapper,
    _compute_score_v2_1_wrapper,  # nouveau M14
)

# Registry étendu M14 avec v2.1.
SCORING_VERSIONS_REGISTRY["v2.1"] = _compute_score_v2_1_wrapper

__all__ = [
    "SCORING_VERSIONS_REGISTRY",
    "compute_score",
    # ...
]
```

```python
# src/polycopy/discovery/scoring/v2/aggregator.py — MA.8

def _compute_score_v2_1_wrapper(metrics: TraderMetrics) -> float:
    """Wrapper registry signature `(TraderMetrics) -> float` pour v2.1.

    Diff vs `_compute_score_v2_wrapper` :
    - Appelle `compute_score_v2_1(...)` (M14 §5.1).
    - Pool context posé par orchestrator via `_CURRENT_POOL_CONTEXT_V2_1`
      (nouveau contextvar — coexiste avec `_CURRENT_POOL_CONTEXT` v2 pour
      shadow period).

    Cf. M12 §5.2 pour le pattern contextvar.
    """
    pool_ctx = _CURRENT_POOL_CONTEXT_V2_1.get()
    if pool_ctx is None:
        log.warning("scoring_v2_1_no_pool_context", wallet=metrics.wallet_address)
        return 0.0
    if not isinstance(metrics, TraderMetricsV2):
        log.warning(
            "scoring_v2_1_wrong_metrics_type",
            wallet=metrics.wallet_address,
            type_received=type(metrics).__name__,
        )
        return 0.0
    return compute_score_v2_1(metrics, pool_ctx).score
```

---

## 6. DTOs / signatures

### 6.1 `RawPosition` étendu (MA.6)

```python
# src/polycopy/discovery/dtos.py

class RawPosition(BaseModel):
    """Position bruite extraite de Data API /positions.

    M14: ajout `opened_at` (optional) pour `_compute_zombie_ratio` filtre
    temporel.
    """
    model_config = ConfigDict(frozen=True)
    # ... champs existants ...
    opened_at: datetime | None = None  # MA.6
```

### 6.2 Nouveau wrapper registry (MA.8)

```python
# src/polycopy/discovery/scoring/v2/aggregator.py

def _compute_score_v2_1_wrapper(metrics: TraderMetrics) -> float: ...  # cf. §5.8
```

### 6.3 Nouvelle fonction `rank_normalize` + helper (MA.2)

```python
# src/polycopy/discovery/scoring/v2/normalization.py

def rank_normalize(values: list[float]) -> list[float]: ...
def rank_normalize_one(wallet_value: float, pool_values: list[float]) -> float: ...
```

### 6.4 Nouveau contextvar v2.1 (MA.8)

```python
# src/polycopy/discovery/scoring/v2/pool_context.py

_CURRENT_POOL_CONTEXT_V2_1: ContextVar[PoolContext | None] = ContextVar(
    "scoring_v2_1_pool_context", default=None,
)
```

(Coexiste avec `_CURRENT_POOL_CONTEXT` M12 v2 — pattern double-write pendant shadow.)

### 6.5 `PoolContext` inchangé structurellement

Le DTO `PoolContext` (M12) accepte déjà la sémantique pool-mean dynamique
sans modification — seul le code qui le **construit** change (cf. §5.4
étape 2).

---

## 7. Settings

Nouvelles env vars + modification de defaults (toutes backward-compat) :

| Variable env | Champ Settings | Default M14 | Default M13 (avant) | Description |
|---|---|---|---|---|
| `SCORING_VERSION` | `scoring_version: Literal["v1","v2","v2.1"]` | `"v1"` | `"v1"` | Inchangé default. Literal étendu à `"v2.1"`. |
| `SCORING_V2_1_SHADOW_DAYS` | `scoring_v2_1_shadow_days: int [0, 90]` | `14` | (n'existait pas) | M14 : durée coexistence v1/v2.1 (shadow). Clone pattern `SCORING_V2_SHADOW_DAYS` M12. |
| `EVICTION_SCORE_MARGIN` | `eviction_score_margin: float [0.02, 0.30]` | **`0.10`** | `0.15` | M14 : recalibré 1σ post-rank-transform. Bornes resserrées. |

**`.env.example` mise à jour** :

```dotenv
# --- M14 : Scoring v2.1-ROBUST ---
# v2.1 remplace v2 comme formule shadow vs v1 (décision D6 spec M14).
# Cutover manuel post-shadow + validation H-EMP-1/H-EMP-2 (cf. §14).
# SCORING_VERSION=v1                       # "v1" / "v2" / "v2.1"
# SCORING_V2_1_SHADOW_DAYS=14              # calcul parallèle v1+v2.1 pendant N jours

# --- M5_bis : eviction (recalibration M14, MA.7) ---
# 0.10 = 1σ empirique post-rank-transform v2.1. Re-mesurer post-ship.
# EVICTION_SCORE_MARGIN=0.10
```

**Cross-field validators** :

- Si `SCORING_VERSION=v2.1` ET `TRADER_DAILY_PNL_ENABLED=false` → log
  WARNING au boot ("v2.1 sans equity curve = Sortino dégradé").
- Si `SCORING_V2_1_SHADOW_DAYS=0` ET `SCORING_VERSION=v1` → log INFO
  "v2.1 shadow disabled — pas de double-compute". Cohérent M12 default
  rollback.

---

## 8. Invariants sécurité

### 8.1 Versioning sacré (append-only) préservé

- Chaque row `trader_scores` porte sa `scoring_version` ∈ `{"v1", "v2",
  "v2.1"}`. Aucun `UPDATE` rétroactif.
- v2 reste accessible via le registry (audit trail intact). Les rows
  historiques `scoring_version="v2"` ne sont **jamais** réécrites en v2.1.
- M14 ajoute une nouvelle entrée registry mais ne modifie aucune existante.
- Test de non-régression : `test_scoring_versions_registry_extends_not_replaces`.

### 8.2 Triple garde-fou M3 + 4ᵉ M8 intacts

Confirmer : **aucun fichier executor / strategy / monitoring touché par M14**.
- ❌ Pas de `ClobWriteClient`, `_persist_realistic_simulated`, `RiskManager`,
  `PnlSnapshotWriter`, `DryRunResolutionWatcher`.
- ✅ Seuls les modules `discovery/scoring/v2/` + `discovery/metrics_collector_v2.py`
  + `discovery/orchestrator.py` + `config.py` + `discovery/dtos.py` modifiés.

### 8.3 Pas de fuite de secret (grep automatisé)

Tous les nouveaux events structlog M14 (`scoring_v2_1_pool_context_built`,
`pool_brier_baseline_floored`, `zombie_ratio_filtered_recent`,
`weights_v2_1_renormalized`, `rank_normalize_applied`, etc.) ne contiennent
que :
- Wallet adresses (publiques, on-chain).
- Valeurs numériques calculées (scores, ratios, deltas).

Test à ajouter : `test_no_secret_leak_in_scoring_v2_1_logs` — grep
defensive `POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, `CLOB_API_SECRET`,
`REMOTE_CONTROL_TOTP_SECRET`, `GOLDSKY_API_KEY` dans tous les events
loggés sur 1 cycle simulé.

### 8.4 Gates durs M12 préservés

MA.6 corrige l'**implémentation** du gate `zombie_ratio < 0.40` (filtre
temporel <30j enfin appliqué) mais **ne supprime pas le gate**. Les 6
gates M12 restent :
- `cash_pnl_90d > 0`
- `trade_count_90d ≥ 50` (cold-start mode 20)
- `days_active ≥ 30`
- `zombie_ratio < 0.40` (corrigé M14)
- `not in BLACKLISTED_WALLETS`
- `not in WASH_CLUSTER_WALLETS`

### 8.5 Blacklist double-check M5_ter préservé

`DecisionEngine.decide` et `list_wallets_to_poll` continuent à vérifier
la blacklist en SQL + Python. M14 ne touche pas à ces chemins.

### 8.6 Aucune nouvelle surface de credentials

M14 reste 100 % read-only (Data API + Gamma + DB locale). Aucune creds
CLOB L1/L2 touchée. Aucun nouvel endpoint POST. La fonction
`_extract_opened_at` lit uniquement le payload Data API public.

---

## 9. Test plan

Total : **28 tests unit** + **2 tests intégration** (H-EMP).

### 9.1 MA.1 — Drop `timing_alpha` (4 tests)

Dans `tests/unit/test_scoring_v2_aggregator.py` (étendre fichier M12).

1. **`test_aggregator_weights_sum_to_one_after_timing_alpha_drop`**
   - Action : assert `_WEIGHT_RISK_ADJUSTED + ... + _WEIGHT_DISCIPLINE == pytest.approx(1.0, abs=1e-6)`.
   - Assertion : passe.

2. **`test_aggregator_score_v2_1_no_uniform_0_10_bias`**
   - Preconditions : 5 metrics fixtures avec `timing_alpha_weighted=0.5`
     (placeholder M12) et autres facteurs variés.
   - Action : `compute_score_v2_1(m, pool_ctx)` pour chaque + `compute_score_v2(m, pool_ctx_v2)`.
   - Assertion : `mean(scores_v2_1) - mean(scores_v2) == pytest.approx(-0.10, abs=0.01)`
     (vérifie que +0.10 uniforme est retiré).

3. **`test_aggregator_same_pool_different_timing_alpha_returns_identical_score_v2_1`**
   - Preconditions : 2 metrics identiques sauf `timing_alpha_weighted` (0.3 vs 0.7).
   - Action : `compute_score_v2_1(m1, pool)` + `compute_score_v2_1(m2, pool)`.
   - Assertion : `score_m1 == score_m2` (timing_alpha n'influence plus).

4. **`test_aggregator_proportional_renormalization`**
   - Action : assert ratios entre poids préservés vs M12 :
     `_WEIGHT_RISK_ADJUSTED / _WEIGHT_CALIBRATION == 0.25 / 0.20`.
   - Assertion : `1.25 ± 1e-6`.

### 9.2 MA.2 — Rank transform (5 tests)

Dans `tests/unit/test_scoring_v2_normalization.py` (étendre).

5. **`test_rank_normalize_returns_values_in_unit_interval`**
   - Action : `rank_normalize([-5.0, 0.0, 100.0, 1.0, -1.0])`.
   - Assertion : tous les éléments ∈ [0, 1].

6. **`test_rank_normalize_preserves_order`**
   - Action : `rank_normalize([1.0, 5.0, 3.0])`.
   - Assertion : index correspondant à `5.0` a le rank max ; `1.0` le min.

7. **`test_rank_normalize_stable_on_small_pool_addition`**
   - Preconditions : pool N=13 fixe, ranks calculés.
   - Action : ajouter 1 wallet avec valeur médiane → re-calculer ranks.
   - Assertion : les ranks des wallets aux extrêmes (top/bottom 3) bougent
     d'au plus 1 position. (Régression-clé contre fixed-point trap C7.)

8. **`test_rank_normalize_handles_ties_with_average_interpolation`**
   - Action : `rank_normalize([1.0, 1.0, 1.0, 4.0])`.
   - Assertion : 3 premiers égaux à `2.0/4 = 0.5` (mean of ranks 1+2+3=6 → 6/3=2 → /4=0.5),
     dernier à `4.0/4 = 1.0`.

9. **`test_rank_normalize_one_helper_appends_wallet`**
   - Action : `rank_normalize_one(5.0, [1.0, 2.0, 3.0, 4.0])`.
   - Assertion : `1.0` (le wallet est top du pool ∪ {wallet}).

### 9.3 MA.3 — Sortino sentinel + median (4 tests)

Dans `tests/unit/test_scoring_v2_factors.py` (étendre).

10. **`test_risk_adjusted_returns_zero_on_flat_curve`**
    - Preconditions : `monthly_equity_curve = [1000.0] * 30`.
    - Action : `compute_risk_adjusted(metrics)`.
    - Assertion : `0.0` (pas sentinel 3.0).

11. **`test_risk_adjusted_median_robust_to_sentinel_cluster`**
    - Preconditions : 1 wallet réel (Sortino=2.0, Calmar=1.5) vs 4 wallets
      sentinel (Sortino=3.0, Calmar=3.0).
    - Action : compute pour chaque + rank_normalize → top 1 ?
    - Assertion : le wallet réel **n'est pas** dépassé par les sentinels
      (median(2.0, 1.5) = 1.75 vs median(3.0, 3.0) = 3.0 — 4 sentinels au top
      mais ils ranknent ensemble → wallet réel rank 5/5 = top mid-pack).

12. **`test_risk_adjusted_sharpe_fallback_on_zero_downside_with_variance`**
    - Preconditions : returns = `[0.01, 0.02, 0.03, 0.005]` (tous positifs,
      pstdev > 1e-3).
    - Action : `_sortino_ratio_robust(returns, 0.0)`.
    - Assertion : `mean(returns) / pstdev(returns)` (Sharpe), pas sentinel 3.0.

13. **`test_risk_adjusted_uses_median_not_weighted_mean`**
    - Preconditions : metrics avec `sortino_brut=10.0, calmar_brut=0.5`.
    - Action : `compute_risk_adjusted(metrics)`.
    - Assertion : `median([10.0, 0.5]) = 5.25` (pas `0.6×10 + 0.4×0.5 = 6.2`).

### 9.4 MA.4 — Brier P(YES) + baseline (4 tests)

Dans `tests/unit/test_metrics_collector_v2.py` (étendre).

14. **`test_brier_computes_prob_yes_not_prob_side_bought`**
    - Preconditions : 2 positions résolues : (1) BUY YES @ 0.40, YES won →
      `yes_at_entry=0.40`, `yes_won=1` ; (2) BUY NO @ 0.60, NO won →
      `yes_at_entry=0.40`, `yes_won=0`.
    - Action : `_compute_brier(positions)`.
    - Assertion : `mean([(1-0.40)², (0-0.40)²]) = mean([0.36, 0.16]) = 0.26`.

15. **`test_brier_symmetric_between_buy_yes_and_buy_no_at_equivalent_prob`**
    - Preconditions : pos1 BUY YES @ 0.30, YES won (yes_at_entry=0.30, yes_won=1).
      pos2 BUY NO @ 0.70, NO won (yes_at_entry=0.30, yes_won=0).
    - Action : `_compute_brier(p1)` vs `_compute_brier(p2)`.
    - Assertion : ils ont exactement le même Brier (`(1-0.30)² == (0-0.30)²` partagent même unité).

16. **`test_brier_baseline_uses_pool_mean_not_hardcoded_0_25`**
    - Preconditions : pool de 50 positions résolues toutes Brier=0.30
      individuellement.
    - Action : `_build_pool_context_v2_1(...)`.
    - Assertion : `pool_context.brier_baseline_pool == 0.30 ± 0.001` (pas 0.25).

17. **`test_brier_baseline_floored_at_0_10`**
    - Preconditions : pool homogène où `_compute_brier(...) = 0.05`.
    - Action : `_build_pool_context_v2_1(...)`.
    - Assertion : `pool_context.brier_baseline_pool == 0.20` (sentinel
      heuristique cf. §5.4 floor).

### 9.5 MA.5 — Flip HHI specialization (3 tests)

Dans `tests/unit/test_scoring_v2_factors.py` (étendre).

18. **`test_specialization_now_rewards_high_hhi`**
    - Preconditions : `metrics.hhi_categories = 0.85` (concentration forte).
    - Action : `compute_specialization(metrics)`.
    - Assertion : `0.85` (pas `1 - 0.85 = 0.15`).

19. **`test_specialization_diversified_wallet_gets_lower_score`**
    - Preconditions : `metrics.hhi_categories = 0.20` (diversifié).
    - Action : `compute_specialization(metrics)`.
    - Assertion : `0.20` (rank-normalisation pool aval positionne ce wallet bas).

20. **`test_specialization_extreme_values_clipped`**
    - Preconditions : `metrics.hhi_categories = 1.5` (théoriquement impossible
      mais défensif).
    - Action : `compute_specialization(metrics)`.
    - Assertion : `1.0` (clippé).

### 9.6 MA.6 — `_compute_zombie_ratio` filtre <30j (3 tests)

Dans `tests/unit/test_metrics_collector_v2.py` (étendre).

21. **`test_zombie_ratio_excludes_positions_opened_within_30d`**
    - Preconditions : 4 positions, 2 récentes (`opened_at = now - 5d`),
      2 anciennes (`opened_at = now - 90d`). Toutes "zombies"
      (`current_value < 0.02 × initial_value`).
    - Action : `_compute_zombie_ratio(positions, now=now)`.
    - Assertion : seul les anciennes zombies comptent → ratio = 1.0
      (capital_zombie_ancien / capital_total_ancien).

22. **`test_zombie_ratio_fallback_on_missing_opened_at`**
    - Preconditions : 4 positions, 2 avec `opened_at=None` (data Data API
      absente), 2 avec `opened_at = now - 60d`.
    - Action : `_compute_zombie_ratio(positions, now=now)`.
    - Assertion : seules les 2 avec `opened_at` connu sont éligibles
      (D5 : exclusion conservatrice). Test que ratio basé sur ces 2 seules.

23. **`test_zombie_ratio_no_eligible_returns_zero`**
    - Preconditions : 3 positions toutes < 30d.
    - Action : `_compute_zombie_ratio(positions, now=now)`.
    - Assertion : `0.0` (pas eligibles → no zombies → ratio defined as 0).

### 9.7 MA.7 — Eviction margin 0.10 (2 tests)

Dans `tests/unit/test_config.py` (étendre).

24. **`test_eviction_score_margin_default_is_0_10`**
    - Action : instancier `Settings()` defaults.
    - Assertion : `settings.eviction_score_margin == 0.10`.

Dans `tests/unit/test_eviction_cascade_planner.py` (étendre).

25. **`test_cascade_planner_fires_with_margin_0_10_realistic_pool`**
    - Preconditions : pool active 8 wallets `[0.42, 0.51, 0.55, 0.58, 0.61,
      0.63, 0.65, 0.68]`. Candidat shadow score 0.55. `eviction_score_margin=0.10`.
      `eviction_hysteresis_cycles=3`. 3 cycles consécutifs.
    - Action : `CascadePlanner.plan(...)`.
    - Assertion : eviction triggered (delta = 0.13 > 0.10), worst active 0.42
      → `sell_only`, candidat → `active`.

### 9.8 MA.8 — Ship `SCORING_VERSION="v2.1"` (3 tests)

Dans `tests/unit/test_scoring_versions.py` (à créer ou étendre).

26. **`test_scoring_v2_1_registered_in_registry`**
    - Action : `SCORING_VERSIONS_REGISTRY`.
    - Assertion : contient les 3 clés `{"v1", "v2", "v2.1"}`.

27. **`test_scoring_v2_1_shadow_period_writes_parallel_to_v1`**
    - Preconditions : `SCORING_VERSION=v1`, `SCORING_V2_1_SHADOW_DAYS=14`.
      Mock `compute_score_v2_1` → 0.7. Mock `compute_score_v1` → 0.5.
    - Action : 1 cycle discovery sur 5 wallets candidats.
    - Assertion : `trader_scores` contient 5 rows v1 ET 5 rows v2.1.
      `target_traders.score` = 0.5 (piloté par v1, pas v2.1).

28. **`test_no_retroactive_rewrite_of_v2_scores`**
    - Preconditions : seed 10 rows `trader_scores(scoring_version="v2", score=0.5)`.
    - Action : run cycle v2.1 sur les mêmes wallets.
    - Assertion : les 10 rows v2 originales **inchangées** + 10 nouvelles
      rows v2.1 ajoutées.

### 9.9 Tests d'intégration H-EMP (2 tests, opt-in `pytest -m hypothesis_validation`)

Dans `tests/integration/test_ma_hypotheses.py` (nouveau).

29. **`test_h_emp_1_risk_adjusted_dominates_variance`** (offline, sur fixtures
    SQL captures de prod)
    - Preconditions : dump `trader_scores` v2 sur 280 cycles
      (`tests/fixtures/h_emp_280_cycles.sql`).
    - Action : `validate_ma_hypotheses.compute_factor_variance_breakdown(rows)`.
    - Assertion : `risk_adjusted` contribue ≥ 40 % de la variance totale
      (seuil go MA cf. §14.4).

30. **`test_h_emp_2_rank_transform_reduces_cycle_variance`** (offline)
    - Preconditions : dump `trader_scores` v2 sur 280 cycles.
    - Action : simuler v2.1 offline avec `rank_normalize` + recalculer
      cycle-to-cycle σ par wallet.
    - Assertion : σ relatif < 10 % sur ≥ 80 % des wallets ACTIVE
      (seuil go MA cf. §14.4).

**Total : 28 unit + 2 intégration = 30 tests**.

---

## 10. Impact sur l'existant

### 10.1 Modules touchés

| Module | Changement | Backwards compat |
|---|---|---|
| [discovery/scoring/v2/aggregator.py](../../src/polycopy/discovery/scoring/v2/aggregator.py) | Nouveau `compute_score_v2_1` + wrapper. Pondérations renormalisées. | `compute_score_v2` (M12) intact. Tests M12 passent. |
| [discovery/scoring/v2/normalization.py](../../src/polycopy/discovery/scoring/v2/normalization.py) | Nouvelle `rank_normalize`. `winsorize_p5_p95` + `apply_pool_normalization` deprecated mais conservées. | M12 v2 continue à utiliser les fns deprecated. |
| [discovery/scoring/v2/factors/risk_adjusted.py](../../src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py) | `compute_risk_adjusted` modifie sa formule (variance min, median, fallback Sharpe). | Tests unit M12 sur `compute_risk_adjusted` doivent être **adaptés** (changement comportemental). |
| [discovery/scoring/v2/factors/specialization.py](../../src/polycopy/discovery/scoring/v2/factors/specialization.py) | Flip `1 - HHI` → `HHI`. | Tests unit M12 sur `compute_specialization` doivent être **adaptés** (résultat inverse). |
| [discovery/scoring/v2/factors/calibration.py](../../src/polycopy/discovery/scoring/v2/factors/calibration.py) | Adapté pour Brier P(YES). Floor pool baseline. | Tests M12 à adapter (formule légèrement différente). |
| [discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py) | `_compute_brier` P(YES), `_compute_zombie_ratio` filtre <30j, `_extract_opened_at`, `_fallback_opened_at`. | Tests M12 partiellement à adapter. |
| [discovery/dtos.py](../../src/polycopy/discovery/dtos.py) | `RawPosition` +`opened_at: datetime \| None = None`. | Default `None` non-cassant. |
| [discovery/orchestrator.py](../../src/polycopy/discovery/orchestrator.py) | `_build_pool_context_v2_1` + branche shadow v1 vs v2.1. | M12 shadow v2 préservé en parallèle (registry conservé). |
| [discovery/scoring/__init__.py](../../src/polycopy/discovery/scoring/__init__.py) | +1 entrée registry. | M12 entries inchangées. |
| [config.py](../../src/polycopy/config.py) | Literal v2.1, defaults `eviction_score_margin=0.10`, +1 flag. | Default `scoring_version="v1"` préservé. |

### 10.2 Changements de valeurs par défaut

- `EVICTION_SCORE_MARGIN=0.15` → **`0.10`** : breaking pour les utilisateurs
  qui n'ont pas overridé. Acceptable car `EVICTION_ENABLED=false` par
  défaut → pas d'impact comportemental immédiat. Documenté dans §11
  rollback path.
- `SCORING_VERSION="v1"` reste default → pas de breaking.

### 10.3 Tests M12 existants à adapter (estimation)

À vérifier lors de l'implémentation :
- `tests/unit/test_scoring_v2_factors.py::test_compute_risk_adjusted_*` (~3-4 tests)
- `tests/unit/test_scoring_v2_factors.py::test_compute_specialization_*` (~2 tests)
- `tests/unit/test_scoring_v2_normalization.py::test_apply_pool_normalization_*`
  (vérifier que les tests M12 ne testent pas `rank_normalize` à tort)
- `tests/unit/test_metrics_collector_v2.py::test_brier_*` (~2-3 tests)
- `tests/unit/test_metrics_collector_v2.py::test_zombie_ratio_*` (~2 tests)

Charge totale adaptation : ~10-15 tests existants modifiés (changement
formule).

### 10.4 Pas de modification CLI / cli runner

- `src/polycopy/cli/runner.py` : aucun.
- Boot log inchangé (la valeur `scoring_version` est déjà loggée par M12).

### 10.5 Pas de modification dashboard structurelle

- Onglet `/traders/scoring` continue à fonctionner. Les rows v2.1 apparaissent
  comme une **3ᵉ colonne** "Score v2.1" via étape MH (UX fix Spearman) — pas
  dans M14.
- Si MH pas encore livré : v2.1 affiché à la place de v2 (registry remplacement
  côté template — un seul score "v2" affiché, qui correspond en réalité à v2.1
  si `SCORING_VERSION=v2.1`). Acceptable temporairement.

---

## 11. Migration / Backwards compat

### 11.1 Aucune migration Alembic

M14 n'ajoute **aucun schéma DB**. La colonne `trader_scores.scoring_version:
String(16)` créée par migration 0006 M12 accepte déjà `"v2.1"` (3 chars,
limite 16).

**Confirmer avant commit** : `alembic revision --autogenerate -m "m14_check"`
doit produire un fichier vide ou `# no changes detected`.

### 11.2 Rollback path

Si une régression v2.1 apparaît post-merge :

- **Option A (runtime, recommandé)** : `SCORING_V2_1_SHADOW_DAYS=0` dans
  `.env` → arrête le calcul parallèle. `SCORING_VERSION=v1` (default) → pilote
  reste v1 strict. Restart bot suffit.
- **Option B (eviction si trop agressive)** : `EVICTION_SCORE_MARGIN=0.15`
  → revient au default M12. (Note : `EVICTION_ENABLED=false` par défaut, donc
  même sans override, eviction ne tournait pas.)
- **Option C (git)** : `git revert <sha>`. Aucune migration à rollback (DB
  inchangée).

### 11.3 Coexistence v2 vs v2.1

Pendant la shadow period v2.1 (J0 à J+14) :

- Si l'utilisateur avait déjà `SCORING_V2_SHADOW_DAYS>0` actif (M12) →
  **3 versions** calculées en parallèle (v1 pilote, v2 shadow M12, v2.1
  shadow M14). Coût compute multiplié par 3.
- Recommandation : à J0 du déploiement M14, set
  `SCORING_V2_SHADOW_DAYS=0` (arrête v2 inutile) et garder
  `SCORING_V2_1_SHADOW_DAYS=14`. Tests M14 validés sur v1 vs v2.1 seul.

### 11.4 Cutover post-shadow (J+14)

**Séquence explicite** :

1. **T0** = merge M14 sur `main`. `SCORING_VERSION=v1` default.
2. **T0 + 14j** = fin shadow period. Lancer
   `python scripts/validate_ma_hypotheses.py --output /tmp/ma_validation.html`
   pour vérifier H-EMP-1, H-EMP-2 sur 14 jours réels.
3. **T0 + 14j** : si H-EMP OK ET visualisation manuelle dashboard
   `/traders/scoring` cohérente (couverture ≥ 40, pas de wallet locked, top-10
   v2.1 différent de v1 par ≥ 3 ranks moyen) → user flip
   `SCORING_VERSION=v2.1` dans `.env` + redémarre.
4. **T0 + 28j** : double shadow inverse (v1 continue 14j supplémentaires).
   Si pas de régression détectée via dashboard `/pnl` → user set
   `SCORING_V2_1_SHADOW_DAYS=0`.

**Aucun auto-flip** : décision 100 % humaine.

### 11.5 Gestion des rows `trader_scores` existantes

- `scoring_version="v1"` : intactes.
- `scoring_version="v2"` : intactes (audit trail v2 préservé indéfiniment).
- Nouvelles rows post-merge M14 :
  - Si `SCORING_VERSION=v1` ET `SCORING_V2_1_SHADOW_DAYS>0` → 1 row v1 + 1 row v2.1 par cycle.
  - Si `SCORING_VERSION=v2.1` ET `SCORING_V2_1_SHADOW_DAYS>0` (post-cutover) → 1 row v1 + 1 row v2.1 par cycle.
  - Si `SCORING_V2_1_SHADOW_DAYS=0` ET `SCORING_VERSION=v1` → 1 row v1 par cycle (seul v1 calculé).

---

## 12. Commandes de vérification finale

Bloc copiable pour l'implémenteur M14 :

```bash
# 1. Environnement
cd /home/nexium/code/polycopy
source .venv/bin/activate

# 2. Lint + type-check (après chaque commit MA.x)
ruff check .
ruff format --check .
mypy src --strict

# 3. Tests ciblés par item MA (entre commits)
pytest tests/unit/test_scoring_v2_aggregator.py -x --tb=short -k "v2_1"     # MA.1
pytest tests/unit/test_scoring_v2_normalization.py -x --tb=short            # MA.2
pytest tests/unit/test_scoring_v2_factors.py -x --tb=short -k "risk_adjusted or specialization"  # MA.3 + MA.5
pytest tests/unit/test_metrics_collector_v2.py -x --tb=short -k "brier or zombie"  # MA.4 + MA.6
pytest tests/unit/test_eviction_cascade_planner.py -x --tb=short            # MA.7
pytest tests/unit/test_scoring_versions.py -x --tb=short                    # MA.8

# 4. Full suite (à la fin uniquement, ~3 min)
pytest

# 5. Validation hypothèses empiriques avant ship MA.8 (BLOQUANT)
python scripts/validate_ma_hypotheses.py \
  --sql-dump tests/fixtures/h_emp_280_cycles.sql \
  --output /tmp/ma_validation.html
# Vérifier dans le rapport :
# - H-EMP-1 : risk_adjusted contribue ≥ 40% de la variance totale
# - H-EMP-2 : σ relatif rank_normalize < 10% sur ≥ 80% des wallets ACTIVE
# Si l'une des 2 échoue → STOP, investiguer avant ship.

# 6. Pas de migration Alembic (vérifier)
alembic revision --autogenerate -m "m14_check" --sql | head -5
# Devrait être vide.

# 7. Smoke test shadow period (sur 2 cycles)
SCORING_VERSION=v1 \
SCORING_V2_1_SHADOW_DAYS=1 \
DISCOVERY_ENABLED=true \
DISCOVERY_INTERVAL_SECONDS=3600 \
TRADER_DAILY_PNL_ENABLED=true \
DASHBOARD_ENABLED=true \
python -m polycopy --verbose &
sleep 7200   # 2 cycles
# Vérifier :
# - trader_scores contient rows v1 ET v2.1
# - target_traders.score (colonne overwrite) = score v1
# - Aucun wallet ne montre la même valeur exacte sur ≥ 10 cycles
# - Coverage v2.1 ≥ 40 wallets
kill %1 && wait

# 8. Grep sécurité (aucun secret leak dans nouveaux logs)
pytest tests/unit/test_no_secret_leak_in_scoring_v2_1_logs.py -v

# 9. Smoke rollback (flag v1 strict)
SCORING_VERSION=v1 \
SCORING_V2_1_SHADOW_DAYS=0 \
python -m polycopy --verbose
# Vérifier : comportement M12 strict v1 (pas de calcul v2.1, pas de
# row trader_scores avec scoring_version="v2.1").
```

Après `git push` sur `main`, côté production :

```bash
ssh uni-debian
cd ~/Documents/GitHub/polycopy
git pull
# Bot auto-restart si systemd unit en place
# Surveiller dans les 14j qui suivent :
# - /traders/scoring : aucun wallet locked
# - /pnl : pas de régression vs v1 baseline
# - logs : pas de WARNING `weights_v2_1_renormalized_failed`
```

---

## 13. Hors scope M14 (à ne pas implémenter)

- ❌ **MB — Internal PnL feedback factor** (`internal_pnl_score`). Reportable
  après 30j de copy data post-M13 (commit `d287fbc` 2026-04-25).
- ❌ **MF — Mitts-Ofir composite + Sirolly wash cluster** (informed_score
  complet, graph clustering détection wash). MA.5 inverse seulement le
  signe HHI, pas le composite.
- ❌ **MG — CLV (Closing Line Value) + Kelly proxy + Liquidity-adjusted ROI**.
  Exigent storage time-series + features avancées.
- ❌ **MH — Dashboard stability metric** (std sur N cycles par wallet).
  Visualisation post-merge mais pas le bundle MA.
- ❌ **MH — Spearman rank fix** (UX bug onglet `/traders/scoring`).
- ❌ **Auto-detection wash cluster** (M17+).
- ❌ **Apify Leaderboard scraper integration** (M15).
- ❌ **RTDS timing alpha refactor** (M16, prérequis WS user channel M14
  phase 2 latence).
- ❌ **Nouveau template Telegram pour transitions v2.1 vs v1**. Pas
  d'alerte. `trader_scores` + `trader_events` (gate_rejected) suffisent
  pour audit.
- ❌ **Backfill scores v2 historiques en v2.1**. Versioning sacré.
- ❌ **Dashboard widget dédié histogramme distribution v2.1**. UX migré
  en MH.
- ❌ **Optimisation perf cycle scoring** (cf. §14.5). Pas dans M14.

---

## 14. Notes d'implémentation & zones d'incertitude

### 14.1 Piège : ordre d'implémentation strict

L'ordre testable isolément MA.1 → MA.8 listé dans
[docs/next/MA.md §11](../next/MA.md#11-notes-dimplémentation) est repris ici :

| Ordre | Item | Dépendance | Charge | Justification |
|---|---|---|---|---|
| 1 | MA.1 (drop timing_alpha) | aucune | 0.5j | Simple modif pondérations + bump version comp. |
| 2 | MA.7 (eviction margin 0.10) | aucune | 0.5j | Config-only. |
| 3 | MA.5 (flip HHI) | aucune | 0.5j | 1 ligne logique. |
| 4 | MA.6 (zombie filtre <30j) | DTO RawPosition update | 0.5j | Localisé, mais touche dtos.py. |
| 5 | MA.3 (Sortino sentinel + median) | indep | 1j | Modifie comportement risk_adjusted, dépendances post-MA.1 weights. |
| 6 | MA.2 (rank transform) | indep | 1j | **Le plus invasif** — change tous les facteurs normalisés. |
| 7 | MA.4 (Brier P(YES) + baseline pool-mean) | possiblement migration data si capture entry prices YES manquante | 1j | Dépend de la disponibilité des entry prices YES dans `RawPosition` (cf. §14.3). |
| 8 | MA.8 (ship v2.1 registry) | tous précédents | 0.5j | Assemblage final + commit version bump. |

Total séquentiel : **~5 jours** (4 jours dev + 1 jour shadow setup +
backtest validation). M est une estimation conservatrice — 8 commits atomiques
poussés directement sur `main` (un par MA.x), tests verts entre chaque push.
Pas de PR, pas de branche éphémère (règle projet — workflow trunk-based).

### 14.2 Décision D7 — Renormalisation proportionnelle vs égalitaire

[Claude §4.1 v2.1-ROBUST](../deepsearch/claude_Architectural_Review_of_Scoring_Discovery_and_Latency_for_a_Single-Process_asyncio_Polymarket_Copy-Trader_(April_2026).md#41-v21-robust--rank-aggregation-median-based-no-winsorization)
propose `mean(rank_f) across 5 factors` = équipondération `0.20 / 0.20 /
0.20 / 0.20 / 0.20`. **M14 retient la renormalisation proportionnelle**
(0.3125 / 0.2500 / 0.1875 / 0.1250 / 0.1250) plutôt que équipondération.

**Justification (D7 spec MA.md §4)** :
- Préserve l'**intention de design M12** (Sortino-Calmar plus important
  que sizing_stability, Brier plus que consistency).
- Évite de re-débattre les ratios relatifs — c'est le job de MF (avec data
  internal_pnl pour calibrer empiriquement).
- Le gain de variance attendu vient de **MA.2 (rank transform)** + **MA.1
  (drop placeholder)**, pas du choix équi vs proportionnel.

Si en H-EMP post-ship la variance résiduelle reste > 10 %, ré-évaluer
en MA.2.1 / v2.2.

### 14.3 Piège : capture entry prices YES manquante (impacte MA.4)

`_compute_brier` actuel ([metrics_collector_v2.py:153-170](../../src/polycopy/discovery/metrics_collector_v2.py#L153))
utilise `p.avg_price` qui est le **prix payé** (= prob du side acheté), pas
P(YES_at_entry).

Pour reformuler en P(YES), on doit savoir **quel side a été acheté**
(`outcome` ∈ {"YES", "NO"}). [src/polycopy/discovery/dtos.py::RawPosition](../../src/polycopy/discovery/dtos.py)
expose-t-il déjà `outcome` ?

**Validation pré-implémentation** :
- Vérifier la définition de `RawPosition.outcome` (déjà str ?).
- Si absent : enrichir le DTO + extraire de Data API
  `/positions[*].outcome` (Polymarket payload standard).
- Si déjà présent : aucun change DTO, juste reformuler `_compute_brier`.

Si Data API ne fournit pas `outcome` directement → **STOP MA.4** et reportabilité
à MF (où la collecte est plus exhaustive). Documenter §15 open question 1.

### 14.4 H-EMP-1 + H-EMP-2 : script de validation pré-ship

**Script `scripts/validate_ma_hypotheses.py`** (nouveau, ~120 LOC) :

```python
"""Validation H-EMP-1 + H-EMP-2 avant ship MA.8.

Lit un dump SQL `trader_scores` historique (280 cycles v2 shadow), calcule :
- H-EMP-1 : variance de chaque facteur (raw + normalisé) sur tout le pool.
- H-EMP-2 : simulation v2.1 offline avec rank_normalize, σ cycle-to-cycle.

Outputs : rapport HTML + JSON. Exit code 0 si seuils atteints, 1 sinon.

Seuils (cf. spec M14 §1.4 + brief MA.md §6) :
- H-EMP-1 : risk_adjusted contribue ≥ 40% de la variance totale
- H-EMP-2 : σ relatif < 10% sur ≥ 80% des wallets ACTIVE

Usage :
    python scripts/validate_ma_hypotheses.py \
      --sql-dump tests/fixtures/h_emp_280_cycles.sql \
      --output /tmp/ma_validation.html
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

# imports stdlib + pandas


def compute_h_emp_1(rows: list[dict]) -> dict:
    """Décompose σ totale par facteur. Retourne {factor: contribution_pct}."""
    by_factor = {}
    for factor in ["risk_adjusted", "calibration", "specialization", "consistency", "discipline"]:
        # Extraction depuis trader_scores.metrics_snapshot["breakdown"]["normalized"]
        values = [r["breakdown"]["normalized"].get(factor, 0.0) for r in rows if r.get("breakdown")]
        if values:
            by_factor[factor] = statistics.pstdev(values) ** 2
    total_var = sum(by_factor.values())
    return {f: (v / total_var) for f, v in by_factor.items()} if total_var > 0 else {}


def compute_h_emp_2(rows: list[dict]) -> dict:
    """Simule v2.1 offline avec rank_normalize, calcule σ cycle-to-cycle par wallet."""
    # Group by wallet
    by_wallet = {}
    for r in rows:
        by_wallet.setdefault(r["wallet_address"], []).append(r)
    cycle_to_cycle_sigma = {}
    for wallet, wallet_rows in by_wallet.items():
        # ... appel rank_normalize sur chaque cycle ...
        # ... calcul σ sur les scores rank-normalized ...
        sorted_rows = sorted(wallet_rows, key=lambda r: r["cycle_at"])
        scores = [r["score"] for r in sorted_rows]
        if len(scores) >= 5:
            cycle_to_cycle_sigma[wallet] = statistics.pstdev(scores) / max(0.001, statistics.mean(scores))
    return cycle_to_cycle_sigma


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sql-dump", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("/tmp/ma_validation.html"))
    args = parser.parse_args()

    rows = _load_sql_dump(args.sql_dump)

    h_emp_1 = compute_h_emp_1(rows)
    h_emp_2 = compute_h_emp_2(rows)

    risk_adjusted_pct = h_emp_1.get("risk_adjusted", 0.0)
    h_emp_1_pass = risk_adjusted_pct >= 0.40

    relative_sigmas = list(h_emp_2.values())
    pct_under_10 = sum(1 for s in relative_sigmas if s < 0.10) / max(1, len(relative_sigmas))
    h_emp_2_pass = pct_under_10 >= 0.80

    _write_report(args.output, h_emp_1, h_emp_2, h_emp_1_pass, h_emp_2_pass)

    if h_emp_1_pass and h_emp_2_pass:
        print(f"✅ H-EMP validation PASSED")
        return 0
    print(f"❌ H-EMP validation FAILED — ship blocked")
    print(f"H-EMP-1: risk_adjusted={risk_adjusted_pct:.1%} (need ≥40%) → {'OK' if h_emp_1_pass else 'FAIL'}")
    print(f"H-EMP-2: pct_under_10pct={pct_under_10:.1%} (need ≥80%) → {'OK' if h_emp_2_pass else 'FAIL'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

**Exécution avant le commit final MA.8** :

```bash
python scripts/validate_ma_hypotheses.py \
  --sql-dump tests/fixtures/h_emp_280_cycles.sql \
  --output /tmp/ma_validation.html
```

Si retour code != 0 → STOP. Investiguer avant de merger.

### 14.5 Coût compute estimation

MA.4 ajoute 1 fetch supplémentaire (`_fetch_raw_positions` doit
extraire `opened_at` via Data API `/positions` ou fallback
`detected_trades`). Sur ~50 candidats × 5 calls API ≈ 250 calls/cycle.
À cadence 6h = 1000 calls/jour. **Acceptable** sous le rate limit Data API
(1000/10s global).

MA.2 `rank_normalize_one` est `O(N log N)` par appel × 5 facteurs × 50
wallets = 250 appels × log(50) ≈ 1500 ops par cycle. Négligeable.

MA.3 `median()` sur liste de 2 (sortino, calmar) = O(1). Négligeable.

**Budget cycle scoring v2.1** : reste sous 5 min/cycle (cohérent M12 §11.4).

### 14.6 Race condition — shadow v2 vs v2.1 simultanés

Si l'utilisateur conserve `SCORING_V2_SHADOW_DAYS>0` (M12 v2 shadow)
ET active `SCORING_V2_1_SHADOW_DAYS>0` (M14 v2.1 shadow) →
DiscoveryOrchestrator calcule les **3 versions** par cycle :

1. v1 (pilote) : 1 row trader_scores.
2. v2 (shadow M12) : 1 row.
3. v2.1 (shadow M14) : 1 row.

Coût compute multiplié par 3, mais aucun conflit DB (3 rows distinctes par
`(wallet, cycle_at, scoring_version)`). Acceptable transitoirement.

**Recommandation §11.3** : poser `SCORING_V2_SHADOW_DAYS=0` dès J0 du
deploy M14 pour économiser le compute. v2 reste disponible via le registry
si besoin de comparer historiquement.

### 14.7 Open questions M14

À résoudre **après** ship si les seuils ne sont pas atteints :

1. **Q1 (BLOQUANT MA.4)** : `RawPosition.outcome` exposé directement par
   Data API ? Si non, capture une fixture
   `tests/fixtures/data_api_positions_with_outcome_sample.json` et adapte
   le DTO. STOP MA.4 si Data API ne fournit pas l'info.
2. **Q2 (post-ship)** : si H-EMP-2 montre σ > 10 % sur > 20 % des wallets
   après 7j → ré-évaluer si on doit passer à équipondération D7
   (alternative `mean(rank_f)`) ou ajuster `EVICTION_HYSTERESIS_CYCLES`.
3. **Q3 (post-ship)** : si couverture v2.1 < 30 wallets après 14j →
   investigate `_compute_zombie_ratio` filtre <30j (MA.6) — peut-être
   `opened_at` rarely présent dans Data API → exclusion conservatrice trop
   agressive. Considérer fallback sur le 1er trade de `detected_trades`.
4. **Q4 (post-ship)** : H-EMP-3 (corrélation v2.1 vs realized copy PnL
   sur 14j post-cutover) — calculée à partir des données accumulées par
   MB internal_pnl_score. Si ρ ≤ 0 → v2.1 n'est pas prédictif → prioriser
   MB.

### 14.8 Versioning de la formule v2.1

- **Si une pondération est modifiée post-ship** (suite H-EMP) → bumper
  `SCORING_VERSION` à `"v2.1.1"` (literal étendu, registry +1 entrée).
  **Jamais** réécrire les rows v2.1 historiques.
- Cf. CLAUDE.md §Conventions : "changer une pondération = bumper version".

---

## 15. Prompt d'implémentation

Bloc à coller tel quel dans une nouvelle conversation Claude Code.

````markdown
# Contexte

polycopy scoring v2 (M12) souffre de 5 défauts structurels documentés
dans [docs/audit/2026-04-24-polycopy-code-audit.md](docs/audit/2026-04-24-polycopy-code-audit.md)
et triangulés par 3 deep-searches (cf. synthèse
[docs/deepsearch/SYNTHESIS-2026-04-24-polycopy-scoring-discovery-latency.md](docs/deepsearch/SYNTHESIS-2026-04-24-polycopy-scoring-discovery-latency.md)).
M14 livre **`Score_v2.1-ROBUST`** = bundle 8 items (MA.1 → MA.8) qui
remplace v2 comme formule shadow vs v1.

# Prérequis

- Lire `docs/specs/M14-scoring-v2.1-robust.md` **en entier** (spécifiquement
  §5 Algorithmes + §9 Test plan + §14 Notes implémentation).
- Lire [CLAUDE.md](CLAUDE.md) §Conventions + §Sécurité (versioning sacré,
  grep secrets).
- Lire [docs/next/MA.md](docs/next/MA.md) pour le brief actionnable + tableau
  origines.
- Vérifier que H-EMP fixture existe :
  `tests/fixtures/h_emp_280_cycles.sql`. Si absente, exporter via :
  ```
  sqlite3 ~/.polycopy/data/polycopy.db ".dump trader_scores" > tests/fixtures/h_emp_280_cycles.sql
  ```

# Ordre de commits recommandé

1. `feat(discovery): MA.1 drop timing_alpha weight to 0 + renormalize` (4 tests §9.1)
2. `feat(config): MA.7 recalibrate EVICTION_SCORE_MARGIN to 0.10 (1σ post-rank)` (2 tests §9.7)
3. `fix(discovery): MA.5 flip HHI specialization (HHI direct, Mitts-Ofir)` (3 tests §9.5)
4. `fix(discovery): MA.6 implement zombie_ratio temporal <30d filter` (3 tests §9.6)
5. `fix(discovery): MA.3 require min variance + median(Sortino, Calmar)` (4 tests §9.3)
6. `feat(discovery): MA.2 rank_normalize replaces winsorize_p5_p95` (5 tests §9.2)
7. `fix(discovery): MA.4 Brier P(YES) + dynamic pool-mean baseline` (4 tests §9.4)
8. `feat(discovery): MA.8 ship SCORING_VERSION="v2.1" + shadow config` (3 tests §9.8)

**Push sur main après chaque commit.** Tests verts avant chaque push.

# Validation pré-MA.8 (BLOQUANT)

Avant le commit final MA.8, exécuter le script de validation hypothèses :

```bash
python scripts/validate_ma_hypotheses.py \
  --sql-dump tests/fixtures/h_emp_280_cycles.sql \
  --output /tmp/ma_validation.html
echo "Exit code: $?"
```

Vérifier :
- H-EMP-1 : `risk_adjusted` contribue ≥ 40% de variance totale
- H-EMP-2 : σ relatif < 10% sur ≥ 80% des wallets ACTIVE

Si l'un des deux échoue → **STOP**. Investiguer la cause avant ship MA.8.
Causes probables : implémentation MA.2 incorrecte (rank pas appliqué),
implémentation MA.3 incorrecte (median vs moyenne), ou seuil mal calibré.

# Tests + quality gates

- Tests ciblés entre commits (cf. memory `feedback_test_scope`).
- Full `pytest` + `ruff check .` + `ruff format --check .` + `mypy src --strict` à la fin.
- Si tests M12 existants cassent (changement comportemental MA.3 ou MA.5) →
  **adapter** plutôt que skip. Documenter dans le commit message la nouvelle
  expected behavior.

# Git workflow

- **Tout commit directement sur `main`** — pas de branche, pas de PR
  (règle projet, workflow trunk-based).
- 8 commits atomiques (1 par item MA) poussés en série sur `main` après
  validation tests verts entre chaque push.
- Update CLAUDE.md §Conventions avec la nouvelle version `"v2.1"` dans le
  même run (commit additionnel ou agrégé à MA.8).

# Plan à confirmer

Commence par me confirmer ton plan en 1 message bref (1 phrase par commit MA.x),
puis enchaîne les 8 commits dans l'ordre ci-dessus. Tests verts entre chaque
push. Avant MA.8, exécute `validate_ma_hypotheses.py` et reporte le résultat.

# Contraintes non négociables

- `SCORING_VERSION="v1"` reste default. v2.1 ne pilote pas tant que l'utilisateur
  n'a pas explicitement flip après shadow + H-EMP validation.
- **Versioning sacré** : aucune row `trader_scores` v1 ou v2 n'est réécrite.
  v2.1 ajoute des rows en parallèle.
- **Aucune migration Alembic** — la colonne `String(16)` accepte déjà `"v2.1"`.
  Si `alembic revision --autogenerate` produit un fichier non-vide → STOP,
  investiguer.
- **Triple garde-fou M3 + 4ᵉ M8 préservés** : aucun fichier executor /
  strategy / monitoring touché.
- **Aucune creds CLOB consommée** — M14 100 % read-only.
- **Conventions CLAUDE.md** : async, Pydantic v2 frozen, SQLAlchemy 2.0,
  structlog, docstrings FR / code EN, pas de print.
- **mypy strict propre, ruff propre, coverage ≥ 80%** sur fichiers modifiés.

# Demande-moi confirmation AVANT

- Modifier la signature `RawPosition` (MA.6 ajoute `opened_at`).
- Toucher `discovery/orchestrator.py` (ajout branche `_build_pool_context_v2_1`).
- Si Data API ne fournit pas `outcome` directement (MA.4 STOP cf. §14.7 Q1).
- Update CLAUDE.md (§10).

# STOP et signale si

- Test H-EMP échoue (cf. §14.4) — ne pas ship MA.8 sans validation.
- Tests M12 cassent en cascade et > 20 tests à adapter (signal de scope creep).
- `RawPosition.outcome` n'existe pas et Data API ne le fournit pas (impacte MA.4).
- Cycle scoring > 10 min sur 50 wallets après MA.4 (régression compute,
  cf. §14.5).

# Smoke test final obligatoire avant merge

```bash
SCORING_VERSION=v1 \
SCORING_V2_1_SHADOW_DAYS=1 \
DISCOVERY_ENABLED=true \
DISCOVERY_INTERVAL_SECONDS=3600 \
TRADER_DAILY_PNL_ENABLED=true \
DASHBOARD_ENABLED=true \
python -m polycopy --verbose
```

Sur 2 cycles minimum, vérifier :
- `trader_scores` contient des rows `scoring_version="v2.1"`.
- Aucun wallet ne montre la même valeur exacte sur 2 cycles consécutifs.
- Top-10 v2.1 ≠ top-10 v1 (Δ rank moyen > 3).
- Coverage v2.1 ≥ 30 wallets (cible 40+ post 14j shadow réelle).
- Logs : aucun WARNING `weights_v2_1_renormalized_failed` ou
  `scoring_v2_1_no_pool_context`.

Pas de commit récap final — la séquence des 8 commits MA.x sur `main`
constitue le bundle. Si tu veux un repère grossissable dans `git log`,
ajoute juste un commit doc `docs: M14 scoring v2.1-ROBUST shipped` après
MA.8 (optionnel).
````

---

## 16. Commit message proposé

```
feat(discovery): M14 scoring v2.1-ROBUST (rank transform + median Sortino + flip HHI + Brier P(YES) + zombie filter + ship v2.1)

Bundle 8 items (MA.1 → MA.8) qui remplace v2 comme formule shadow vs v1 :

- MA.1 drop `timing_alpha` weight (placeholder 0.5 injectait +0.10 uniforme,
  audit H-008). Pondérations renormalisées proportionnellement
  (0.3125 / 0.2500 / 0.1875 / 0.1250 / 0.1250).
- MA.2 `rank_normalize` remplace `winsorize_p5_p95`. Élimine le fixed-point
  trap C7 ("wallet locked à 0.45 sur 80 cycles"). Variance cycle-to-cycle
  ±30% → ±5-10% projetée.
- MA.3 Variance minimale `pstdev > 1e-3` exigée pour Sortino + fallback
  Sharpe quand downside vide. `median(Sortino, Calmar)` au lieu de moyenne
  pondérée (Claude C10, audit H-009 fix).
- MA.4 Brier sur P(YES) au lieu de P(side_bought) (Gneiting-Raftery 2007,
  audit M-001). Baseline pool-mean dynamique (suppression hardcoded 0.25).
- MA.5 Flip `specialization = HHI` direct (pas `1 - HHI`). Récompense la
  concentration Mitts-Ofir-like (Claude C9 + Harvard Corpgov 2026).
- MA.6 `_compute_zombie_ratio` filtre temporel <30j enfin implémenté
  (audit H-014). Source `opened_at` via Data API `firstTradeTimestamp` +
  fallback `detected_trades`.
- MA.7 `EVICTION_SCORE_MARGIN` 0.15 → 0.10 (≈ 1σ empirique post-rank-transform).
- MA.8 Ship `SCORING_VERSION="v2.1"` dans le registry. Shadow vs v1
  (v2 jugé non-viable). Append-only strict — aucune migration Alembic.

Hypothèses empiriques validées avant ship via
`scripts/validate_ma_hypotheses.py` (H-EMP-1 + H-EMP-2 sur 280 cycles
historiques).

28 tests unit + 2 tests intégration. Backward-compat : v1 default
préservé, v2 registry intact, version v2.1 additive.

Cf. spec [docs/specs/M14-scoring-v2.1-robust.md](docs/specs/M14-scoring-v2.1-robust.md).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## 17. Critères d'acceptation

- [ ] 8 items MA.1 → MA.8 implémentés selon §5.
- [ ] Pondérations v2.1 sommen à `1.0 ± 1e-6` (assert dans aggregator + test).
- [ ] `rank_normalize` retourne ranks ∈ ]0, 1] avec interpolation `"average"`
      sur les ties.
- [ ] `compute_risk_adjusted` retourne `0.0` sur curve plate (pstdev < 1e-3).
- [ ] `_compute_brier` calcule P(YES) symétriquement pour BUY YES @ x et BUY NO @ (1-x).
- [ ] `brier_baseline_pool` calculé dynamiquement (pas 0.25 hardcodé).
- [ ] `compute_specialization` retourne `HHI` direct (pas `1 - HHI`).
- [ ] `_compute_zombie_ratio` filtre les positions avec `opened_at > now - 30d`.
- [ ] `EVICTION_SCORE_MARGIN` default = `0.10`. Validator bornes `[0.02, 0.30]`.
- [ ] `Literal["v1", "v2", "v2.1"]` extension. Default reste `"v1"`.
- [ ] Registry `SCORING_VERSIONS_REGISTRY["v2.1"]` exposé.
- [ ] `_compute_score_v2_1_wrapper` signe `(TraderMetrics) -> float`,
      utilise `_CURRENT_POOL_CONTEXT_V2_1` contextvar.
- [ ] Aucune migration Alembic.
- [ ] `RawPosition.opened_at: datetime | None = None` ajouté.
- [ ] `MetricsCollectorV2._extract_opened_at` + `_fallback_opened_at` impl.
- [ ] Shadow period dual-compute v1+v2.1 fonctionne (test
      `test_scoring_v2_1_shadow_period_writes_parallel_to_v1`).
- [ ] Aucune row `trader_scores` v1 ou v2 réécrite (test
      `test_no_retroactive_rewrite_of_v2_scores`).
- [ ] Script `scripts/validate_ma_hypotheses.py` produit rapport HTML/JSON,
      exit code 0 si seuils atteints.
- [ ] H-EMP-1 + H-EMP-2 validés avant ship MA.8.
- [ ] Test grep secret leak passe sur les nouveaux events structlog.
- [ ] CLAUDE.md §Conventions mise à jour avec mention v2.1 (cf. §10 spec M12 pour pattern).
- [ ] `.env.example` : 2 variables M14 documentées avec commentaires.
- [ ] Tests M12 existants adaptés (changement comportemental MA.3, MA.4, MA.5).
      Aucun test v1 ne casse.
- [ ] **Invariants M5 / M5_bis / M5_ter / M11 / M12 / M13 préservés** :
      lifecycle, eviction core, watcher, latency stages, registry v1/v2,
      dry-run executor — tous intacts. Tests ciblés passent inchangés.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src --strict` : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur fichiers modifiés.
- [ ] Smoke test shadow period 2 cycles : aucun wallet locked, coverage
      v2.1 ≥ 30, top-10 différent de v1.
- [ ] 8 commits atomiques MA.1 → MA.8 poussés sur `main` (pas de branche, pas de PR — règle projet).

---

**Fin spec M14.**
