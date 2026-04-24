# MA — Scoring v2.1-ROBUST (foundation)

**Priorité** : 🔥 P1 (fondation — débloque MB, MF, MG)
**Charge estimée** : M (3-4 jours)
**Branche suggérée** : `fix/scoring-v2.1-robust`
**Prérequis** : aucun
**Bloque** : MB (lifecycle dépend de scoring stable), MF (v2.2 capstone), MG (nouveaux facteurs s'ajoutent à v2.1)

---

## 1. Objectif business

Rendre le scoring v2 **stable et interprétable** en corrigeant simultanément 6 défauts structurels identifiés par l'audit code et les 3 deep-searches. Livrable : une formule v2.1-ROBUST qui remplace v2 en shadow period, avec variance cycle-to-cycle divisée par ~3 (de ±30% à ±5-10%) et couverture pool accrue. Débloque le cutover business (14j → 28j shadow v2.1) et toute la suite (MB/MF/MG).

## 2. Contexte & problème observé

### Symptômes dashboard 2026-04-24

- Wallet `0x63d43bbb…a2f1` **verrouillé à exactement 0.45** sur 80+ cycles consécutifs (SQL dump user 2026-04-24).
- Wallet `0x08c95f70…a2ef` oscille **0.25 → 0.48 → 0.26 → 0.38 → 0.43** entre cycles consécutifs (variance ±30-40%).
- Couverture v2 : **13/50 wallets scorés** vs pool v1 (J+5 shadow period).
- Spearman rank v1/v2 sur /scoring affiche encore des ranks du pool entier (33, 45, 52) malgré commit `1ba8ae3` — fix incomplet.

### Findings référencés

- **[F01] 🟢 3/3** (synthèse §2.2) : drop `timing_alpha=0.5` placeholder. Injecté +0.10 uniforme bias. Claude §3.1 : "0% of variance, 20% of paper weight". Gemini §Analysis v2 Factors : "static 0.5 additive constant mathematically dilutes the variance of the entire scoring vector, destroying discriminative power".
- **[F05] 🟢 3/3** (synthèse §2.2) : winsorisation p5-p95 sur N<20 instable. Claude C6 : "at N=13, p5 and p95 correspond to order statistics 1 and 12. You're effectively clipping only the min and max... **the winsorization is the cause, not the mitigation, of the variance at this pool size**. Winsor 1947 requires symmetric distribution; your pool is right-skewed (only survivors pass gates)".
- **[F03] 🟢 3/3** (synthèse §2.2) : Sortino sentinel=3.0 sur curve plate = zombies dominent. Claude C10 : "Traders with 50-100 trades but no losing months have no downside deviation; Sortino denominator → 0; you cap with sentinel 3.0. After normalization, sentinel wallets cluster at the top. You're scoring **absence of evidence as evidence of skill**".
- **[F04] 🟡→🟢** (synthèse §2.2) : Brier calcule prob(side_bought) ≠ prob(YES). Claude C8 + Gneiting-Raftery 2007 JASA : "Strictly proper scoring rules require the forecast be a **probability distribution over outcomes**. What you have is closer to directional accuracy weighted by entry price — a proxy, but not a proper scoring rule. The baseline mismatch (raw Brier uses 0.25, scoring uses pool mean) then compounds this."
- **[F07] 🟡 2/3** (synthèse §2.3 ⚠️ contradiction résolue) : HHI specialization flip. Claude C9 : "Your specialization factor penalizes HHI, so you actively **down-weight** the exact pattern that earned $143M in documented anomalous profit on Polymarket (Mitts & Ofir Magamyman wallets)". Gemini §Analysis v2 Factors : "Polymarket's Gamma tags are highly correlated and overlapping; HHI heavily penalizes versatile traders".
- **[F06] 🟢 3/3** (synthèse §2.2) : rank-based > threshold-based eviction. Claude C11 : "rank is ordinal, 0.60 is cardinal. These live on different metric spaces. When pool size shifts, absolute scores drift without rank change, so a wallet can become rank-worst but stay above 0.60".
- **Audit [H-014]** : `_compute_zombie_ratio` temporal filter documenté mais non implémenté dans [src/polycopy/discovery/metrics_collector_v2.py:173-197](../../src/polycopy/discovery/metrics_collector_v2.py#L173-L197).
- **Audit [C-007]** : normalisation "lower" method + append-only versions = non-monotone re-rankings. Claude C7 : "numpy.quantile(method='lower') maps ties to the same rank and skips values. Combined with 6h cycle and EVICTION_HYSTERESIS_CYCLES=3, you need a wallet to be out-of-band for 18h before demoting — but the band itself is moving with pool composition. This is the formal mechanism behind your 'wallet locked at 0.45 for 80 cycles'".

### Session originale mappée

**Session B** (brouillon `docs/bug/session_B_scoring_v2_reliability.md`) items B1-B7 absorbés ici, **sauf** :
- B4 "débloquer couverture v2" → partiellement traité (relaxation gates passe en MB via probation)
- B6 "dashboard stability metric" → migre en **MH**

## 3. Scope (items détaillés)

### MA.1 — Drop `timing_alpha` weight à 0 + renormaliser les 5 autres

- **Location** : [src/polycopy/discovery/scoring/v2/aggregator.py:43-48](../../src/polycopy/discovery/scoring/v2/aggregator.py#L43-L48) + [src/polycopy/discovery/scoring/v2/factors/timing_alpha.py:25-31](../../src/polycopy/discovery/scoring/v2/factors/timing_alpha.py#L25-L31)
- **Ce qu'il faut faire** :
  - Mettre le poids de `timing_alpha` à **0** dans `aggregator.py` (pondérations restantes : `risk_adjusted=0.31, calibration=0.25, specialization=0.19, consistency=0.13, discipline=0.13` — renormaliser 0.25/0.20/0.15/0.10/0.10 pour sommer à 1.0).
  - Alternative retenue : **redistribution proportionnelle** (chaque facteur garde sa proportion relative, juste sans timing_alpha). Formule : `new_weight[i] = old_weight[i] / (1 - 0.20) = old_weight[i] / 0.80`.
  - **Ne pas** supprimer la fonction `compute_timing_alpha()` (pour éventuel re-enable en MG / v2.2). Juste weight=0.
  - Ajouter garde-fou dans `aggregator.py` : `assert sum(weights) == 1.0` ± epsilon.
  - Bump `SCORING_VERSION` default → `"v2.1"`.
- **Tests requis** :
  - `test_aggregator_weights_sum_to_one_after_timing_alpha_drop`
  - `test_aggregator_score_v2.1_no_uniform_0.10_bias` (assert delta vs v2 = +0.10 uniform removed)
  - `test_aggregator_same_pool_different_timing_alpha_returns_identical_score_v2.1`
- **Sources deep-search** : Claude §4.1 "v2.1-ROBUST" item 1 + Gemini §5 réponse "timing_alpha mathematically indefensible" + Perplexity implicite (aucune formule commerciale n'utilise placeholder).
- **Charge item** : 0.5 jour

### MA.2 — Remplacer winsorisation p5-p95 par rank transform

- **Location** : [src/polycopy/discovery/scoring/v2/normalization.py:16-33](../../src/polycopy/discovery/scoring/v2/normalization.py#L16-L33)
- **Ce qu'il faut faire** :
  - Remplacer la fonction `winsorize_p5_p95()` par `rank_normalize(values: list[float]) -> list[float]` qui retourne `rank(w) / N` ∈ [0, 1] (interpolation "average" pour les ties).
  - Éliminer l'appel à `numpy.quantile(method='lower')` qui crée le fixed-point trap (C7).
  - Préserver la signature externe : les callers `factors/*.py` reçoivent toujours un `list[float]` normalisé [0,1].
  - **Décision D1** : rank transform plutôt que log-transform (Gemini alternative). Justification Claude §4.1 : "ranks are discrete, small pool movement only shifts ranks on local swaps — variance drops from ±30% to ±5-10%".
  - Renommer `normalization.py` → garder le nom, mais déprécier `winsorize_*` en favorisant `rank_normalize`. Tests existants à adapter.
- **Tests requis** :
  - `test_rank_normalize_returns_values_in_unit_interval`
  - `test_rank_normalize_preserves_order`
  - `test_rank_normalize_stable_on_small_pool_addition` (pool N=13 + ajout wallet = shifts ranks mais pas endpoints)
  - `test_rank_normalize_handles_ties_with_average_interpolation`
  - Suppression / adaptation de `test_apply_pool_normalization_*` existants si signature change
- **Sources deep-search** : Claude §4.1 + §6 audit mapping F05 + Gemini §"Additive vs Multiplicative" "Winsorization destroys fat tails" + Wicker stratified winsorizing [Winsorizing PDF](https://twicker97.github.io/JM_documents/Winsorizing.pdf).
- **Charge item** : 1 jour

### MA.3 — Fix Sortino sentinel 3.0 (exiger variance minimale)

- **Location** : [src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py:79-88](../../src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py#L79-L88)
- **Ce qu'il faut faire** :
  - Remplacer `_RATIO_CAP_SENTINEL = 3.0` pattern par : si `pstdev(returns) < 1e-3` → retourner **0.0** (facteur inobservable, pas "génial inactif").
  - Pour les wallets avec downside_deviation=0 mais variance totale > 1e-3 : utiliser Sharpe comme fallback (Sortino/Sharpe corrélation r>0.95 per Rollinger & Hoffman 2013 [CME PDF](https://www.cmegroup.com/education/files/rr-sortino-a-sharper-ratio.pdf)).
  - **Décision D2** : utiliser `median(Sortino, Calmar)` au lieu de `0.6 × Sortino + 0.4 × Calmar`. Justification Claude §4.1 : "median is robust to sentinel clusters". Zombie wallets qui saturent Sortino ne dominent plus via la médiane.
  - Renommer la fonction `compute_risk_adjusted()` → garder la signature (input : equity curve, output : float ∈ [0, ∞)).
- **Tests requis** :
  - `test_risk_adjusted_returns_zero_on_flat_curve` (pstdev < 1e-3)
  - `test_risk_adjusted_median_robust_to_sentinel_cluster` (pool avec 5 wallets sentinel + 5 real → median picks real)
  - `test_risk_adjusted_sharpe_fallback_on_zero_downside_with_variance` (edge case)
  - Adapter `test_compute_sortino_*` existants si signature change
- **Sources deep-search** : Claude C10 + §4.1 + Gemini §v2 Failure Modes "Sortino undefined when downside deviation = 0".
- **Charge item** : 1 jour

### MA.4 — Fix Brier : P(YES) + baseline pool-mean cohérent

- **Location** : [src/polycopy/discovery/metrics_collector_v2.py:165-170](../../src/polycopy/discovery/metrics_collector_v2.py#L165-L170) + [src/polycopy/discovery/scoring/v2/factors/calibration.py](../../src/polycopy/discovery/scoring/v2/factors/calibration.py)
- **Ce qu'il faut faire** :
  - Dans `_compute_brier(positions)` : remplacer `pred = avg_price` (prix du side acheté) par `pred = P(YES_at_entry)`. Nécessite capturer le prix YES au moment de l'entrée (pas le side pris).
  - Pour une position BUY YES à $0.40 : `pred = 0.40`, `outcome = 1 if YES won else 0`.
  - Pour une position BUY NO à $0.60 : `pred = 1 - 0.60 = 0.40` (prob YES), `outcome = 0 if YES lost else 1`. Brier sur P(YES) uniforme.
  - Dans `aggregator.py` : utiliser **baseline pool-mean** cohérent (baseline = moyenne Brier du pool courant, pas 0.25 littéral). Supprimer le `brier_baseline_pool=0.25` hardcodé ([orchestrator.py:651-653](../../src/polycopy/discovery/orchestrator.py#L651-L653)).
  - Brier-skill = `1 - brier_wallet / brier_baseline_pool`. Si pool_mean = 0.22, baseline = 0.22. Un wallet à brier=0.15 a skill = `1 - 0.15/0.22 = 0.32`.
  - **Décision D3** : conserver la sémantique "Brier-skill vs pool" (pas "Brier absolu"). Claude §6 audit mapping : "pool mean preferred, since climatological forecast ≠ 0.25 on Polymarket — most markets are not 50/50".
- **Tests requis** :
  - `test_brier_computes_prob_yes_not_prob_side_bought`
  - `test_brier_symmetric_between_buy_yes_and_buy_no_at_equivalent_prob`
  - `test_brier_baseline_uses_pool_mean_not_hardcoded_0.25`
  - `test_calibration_skill_formula_1_minus_ratio`
- **Sources deep-search** : Claude C8 + §6 audit mapping + Gneiting & Raftery 2007 JASA [PDF](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf) + Brier 1950 MWR [AMS](https://journals.ametsoc.org/view/journals/mwre/78/1/1520-0493_1950_078_0001_vofeit_2_0_co_2.xml).
- **Charge item** : 1 jour

### MA.5 — Flip HHI specialization (pénalité → signal positif)

- **Location** : [src/polycopy/discovery/scoring/v2/factors/specialization.py](../../src/polycopy/discovery/scoring/v2/factors/specialization.py)
- **Ce qu'il faut faire** :
  - Remplacer `specialization = 1 - HHI` par `specialization = HHI` (direct, pas inversé).
  - Documenter dans le docstring : "High HHI (concentration sur 1-2 catégories Gamma) = signal insider-like per Mitts & Ofir 2026. We reward concentration, not penalize it."
  - Attention : ce changement **inverse le ranking** pour les wallets diversifiés — ils ne sont plus *pénalisés*, juste moins *récompensés*. Les HHI-hauts remontent naturellement.
  - **Décision D4** : garder le poids 0.15 (renormalisé à 0.19 post-drop timing_alpha — voir MA.1). Ne pas *augmenter* le poids — on inverse le signe seulement. Pour une amplification "insider-first", voir MF qui ajoute Mitts-Ofir composite complet.
  - Ajouter un champ `main_category` calculé (hors scope factor mais utile pour MH dashboard).
- **Tests requis** :
  - `test_specialization_now_rewards_high_hhi`
  - `test_specialization_diversified_wallet_gets_lower_score`
  - Adapter les tests existants `test_compute_hhi_*` (résultats inversés attendus)
- **Sources deep-search** : Claude C9 + Gemini §"Analysis v2 Factors" + Mitts & Ofir 2026 [Harvard Corpgov](https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/).
- **Charge item** : 0.5 jour

### MA.6 — Implémenter zombie_ratio temporal filter <30j

- **Location** : [src/polycopy/discovery/metrics_collector_v2.py:173-197](../../src/polycopy/discovery/metrics_collector_v2.py#L173-L197)
- **Ce qu'il faut faire** :
  - Le docstring actuel dit "excluded du dénominateur : positions ouvertes depuis < 30j" mais **le code ne filtre pas temporellement** (audit H-014).
  - Enrichir `RawPosition` DTO (ou le lookup `/positions` Data API) avec `opened_at` si disponible. Si non disponible via `/positions`, utiliser la date du premier trade sur cet `(asset_id, wallet)` depuis `detected_trades` comme proxy.
  - Filtrer `zombie_ratio` pour exclure positions `opened_at > now - 30d`.
  - **Décision D5** : si `opened_at` absent ET pas inférable, **exclure la position du ratio** (conservatrice) — mieux vaut under-score que sur-score un wallet.
  - Documenter clairement dans le docstring le comportement exact.
- **Tests requis** :
  - `test_zombie_ratio_excludes_positions_opened_within_30d`
  - `test_zombie_ratio_fallback_on_missing_opened_at`
- **Sources** : Audit H-014 + Claude §6 audit mapping.
- **Charge item** : 0.5 jour

### MA.7 — Rank-based eviction margin 1σ (= 0.10) + recalibrer `EVICTION_SCORE_MARGIN`

- **Location** : [src/polycopy/config.py:800-828](../../src/polycopy/config.py#L800-L828) + [src/polycopy/discovery/eviction/cascade_planner.py:119-125](../../src/polycopy/discovery/eviction/cascade_planner.py#L119-L125)
- **Ce qu'il faut faire** :
  - Baisser le default `EVICTION_SCORE_MARGIN` de **0.15** à **0.10** (= 1σ empirique selon Claude §3.1 variance decomposition).
  - **Attention** : post-MA.1 à MA.5, la variance v2.1 attendue est réduite de ±30% à ±5-10%. Donc l'écart σ du pool va baisser. **Re-mesurer** après ship v2.1 sur ~7 jours et ajuster à 1σ effectif (H-EMP-2).
  - Ajouter validator Pydantic : `EVICTION_SCORE_MARGIN ∈ [0.02, 0.30]` (borne basse pour éviter flip-flop, haute pour éviter impossible).
  - Documenter dans `.env.example` : "Recommended value: 1 standard deviation of observed score distribution. For v2.1-ROBUST shadow period, start at 0.10."
- **Tests requis** :
  - `test_eviction_score_margin_validator_bounds`
  - `test_cascade_planner_fires_with_margin_0.10` (fixture pool réaliste)
- **Sources** : Claude §3.1 + §9 item 4 + F06.
- **Charge item** : 0.5 jour

### MA.8 — Ship `SCORING_VERSION="v2.1"` + shadow period config

- **Location** : [src/polycopy/discovery/scoring/__init__.py](../../src/polycopy/discovery/scoring/__init__.py) + [src/polycopy/config.py](../../src/polycopy/config.py)
- **Ce qu'il faut faire** :
  - Ajouter `"v2.1"` au registry `SCORING_VERSIONS_REGISTRY: dict[Literal["v1", "v2", "v2.1"], Callable]`.
  - `compute_score_v2_1()` appelle la formule v2.1-ROBUST assemblée (MA.1-MA.7).
  - Définir comportement shadow period : `SCORING_VERSION="v2.1"` par défaut en nouveau preset, `SCORING_V2_1_SHADOW_DAYS=14` (même pattern que v2).
  - **Décision D6** : la v2.1 **remplace directement la v2** comme formule pilote (pas de shadow v2.1 vs v2 — v2 est jugé non-viable par l'audit). Le shadow period compare v2.1 vs **v1** (comme avant), pour permettre cutover propre vers v2.1 après 14j de validation.
  - Append-only `trader_scores` : chaque row porte `scoring_version="v2.1"`. **Pas de rewrite** des rows v2 historiques.
  - Mettre à jour CLAUDE.md §Conventions avec la nouvelle version.
- **Tests requis** :
  - `test_scoring_v2_1_registered_in_registry`
  - `test_scoring_v2_1_shadow_period_writes_parallel_to_v1`
  - `test_no_retroactive_rewrite_of_v2_scores`
  - Test integration : full cycle discovery avec `SCORING_VERSION="v2.1"` + `SCORING_V2_1_SHADOW_DAYS=14`
- **Sources** : Claude §4.1 v2.1-ROBUST + invariant CLAUDE.md append-only scoring versions.
- **Charge item** : 0.5 jour

## 4. Architecture / décisions clefs

- **D1** : rank transform au lieu de winsorisation p5-p95. Justification : Winsor 1947 requiert N≥20 symmetric, notre pool est N=13 right-skewed. Rank est robuste par construction (Claude §4.1).
- **D2** : `median(Sortino, Calmar)` au lieu de `0.6·Sortino + 0.4·Calmar`. Justification : robuste au sentinel cluster zombies (Claude C10).
- **D3** : Brier-skill vs pool-mean, pas baseline 0.25 hardcodé. Justification : Gneiting-Raftery strict propriety requires consistent baseline (Claude C8).
- **D4** : HHI positif (reward concentration), pas `1-HHI`. Justification : Mitts & Ofir empirical — insider wallets ont HHI → 1.0, c'est leur pattern (Claude C9).
- **D5** : zombie_ratio exclut positions < 30j. Justification : docstring existant, audit H-014 bug de drift code-spec.
- **D6** : v2.1 remplace v2 directement, shadow compare v2.1 vs v1. Justification : v2 est jugé non-viable par l'audit (3 contradictions internes), shadow v2.1 vs v2 serait du bruit.
- **D7** : renormalisation proportionnelle des poids (0.80 divisor), pas redistribution egalitaire. Justification : préserve l'intention de design initiale sans re-débattre les ratios relatifs (c'est le job de MF/MG).

## 5. Invariants sécurité

- **Append-only scoring versions** : chaque row `trader_scores` porte sa `scoring_version`. Jamais de `UPDATE` rétroactif. MA.8 ajoute v2.1, ne modifie pas v1/v2.
- **Triple garde-fou M3 + 4ᵉ M8** : MA est purement scoring, zéro touch à `ClobWriteClient`, `DryRunResolutionWatcher`, `executor/pipeline.py`. Intact.
- **Aucun secret loggé** : les nouveaux events structlog (`rank_normalize_applied`, `brier_pool_baseline_computed`, etc.) ne contiennent que des wallets publics + valeurs numériques. Ajouter grep automatisé au test `test_no_secret_leak_in_scoring_v2_1_logs`.
- **Gates durs préservés** : MA.6 corrige l'implémentation du gate zombie_ratio mais ne supprime aucun gate existant (`cash_pnl_90d>0`, `trade_count_90d≥50`, `days_active≥30`, `zombie_ratio<0.40`, not blacklisted, not in wash cluster).
- **Blacklist double-check** : `DecisionEngine.decide` et `list_wallets_to_poll` continuent à vérifier blacklist. MA ne touche pas.

## 6. Hypothèses empiriques à valider AVANT ship

- **H-EMP-1** (synthèse §8) : la variance v2 est dominée par `risk_adjusted` (58% de σ totale selon Claude §3.1). **Méthode** : dump `trader_scores` breakdown par facteur sur 280 cycles SQL, calculer `std` par facteur. **Seuil go** : `risk_adjusted` contribue ≥40% de la variance totale. Si <40%, re-investiguer l'hypothèse cluster sentinel.
- **H-EMP-2** : rank transform réduit variance cycle-to-cycle de ±30% à ±5-10%. **Méthode** : simuler v2.1 offline sur les 280 cycles historiques avec le code de MA.2. **Seuil go** : σ cycle-to-cycle observé < 10% sur ≥80% des wallets. Si ≥10%, rollback rank transform ou ajuster.
- **H-EMP-3** (partiel) : corrélation score v1/v2.1 vs realized copy PnL sur 14j. **Méthode** : post-ship, dump `my_positions.realized_pnl` + `trader_scores.v2.1` et calculer Spearman par wallet actif avec ≥30 positions closed. **Seuil go** : ρ > 0.15 (modeste mais non-nul). Si ρ ≤ 0.0, v2.1 n'est pas prédictif et il faut prioriser MB internal_pnl_score.

**Script de validation** : créer `scripts/validate_ma_hypotheses.py` qui exécute H-EMP-1 et H-EMP-2 **avant** ship (sur les data SQL actuelles). Ship uniquement si les deux passent.

## 7. Out of scope

- **Internal PnL feedback factor** : migre en **MB** (nécessite collecte 30j post-ship MA).
- **CLV (Closing Line Value)** : migre en **MG**.
- **Kelly proxy (conviction_sigma)** : migre en **MG**.
- **Liquidity-adjusted ROI (Kyle's λ)** : migre en **MG**.
- **Wash cluster continuous score (Sirolly)** : migre en **MF**.
- **Mitts-Ofir composite informed_score** : migre en **MF** (ici on inverse juste HHI, pas le composite complet).
- **v2.3-LIGHTWEIGHT two-tier architecture** : hors scope roadmap actuelle, évaluer post-MF si besoin.
- **Fenêtre 180j + exponential decay half-life 30j** : hors scope, v3 future si besoin.
- **Thompson Sampling ranking** : hors scope MA, approximation rank-based suffisante pour v2.1 (Claude §4.1 + §7.3).
- **Dashboard stability metric** (std sur N cycles par wallet) : migre en **MH**.
- **Fix Spearman rank /scoring display** : migre en **MH** (UX fix indépendant).

## 8. Success criteria

1. **Tests ciblés verts** : ~18 nouveaux tests unit + 2 tests integration (cf. items MA.1-MA.8).
2. **Hypothèses empiriques validées** : H-EMP-1 et H-EMP-2 passent leur seuil go sur data actuelles avant ship.
3. **Variance cycle-to-cycle post-ship** : sur 7j d'observation post-ship v2.1, la std relative des scores pour un même wallet est **< 10%** sur ≥80% des wallets ACTIVE.
4. **Plus de wallet locked** : aucun wallet ne doit afficher la même valeur exacte sur ≥10 cycles consécutifs (grep SQL `SELECT wallet_address, COUNT(*) FROM trader_scores WHERE scoring_version='v2.1' GROUP BY wallet_address, score HAVING COUNT(*) >= 10`).
5. **Couverture v2.1 pool ≥ v1 pool × 0.8** à J+14 : si v1 score 50 wallets, v2.1 en score ≥40 (actuellement 13/50 = 26%).
6. **Top-10 delta v1/v2.1** : changement significatif vs v1 (Δ rank moyen > 3 ranks), signifiant v2.1 discrimine vraiment.
7. **Temps de cycle scoring** : pas de régression perf. Cycle discovery 6h doit rester <15 min de temps CPU.

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MA.1 | — | B (new item) | F01 (3/3), Claude §4.1 item 1, Gemini §5 réponse | #1 |
| MA.2 | [H-006] partiel, [C-006] | B (B3) | F05 (3/3), Claude C6 + §4.1 | #2 |
| MA.3 | [H-009] | B (B1 extend) | F03 (3/3), Claude C10 + §4.1 | #6 |
| MA.4 | [M-001] | B (new) | F04 (Claude C8, Gneiting-Raftery) | #4 |
| MA.5 | — | B (new) | F07 (Claude C9 + Gemini), Mitts-Ofir | #3 |
| MA.6 | [H-014] | B (cause racine) | — (audit seul) | #8 |
| MA.7 | — | A (A3 partiel) | F06 (3/3), Claude §3.1 + §9 item 4 | roadmap #4 partiel |
| MA.8 | — | B (core) | Claude §4.1 v2.1-ROBUST ship | #6 ship |

## 10. Prompt de génération de spec

Bloc à coller dans une **nouvelle conversation Claude Code** pour produire `docs/specs/M<n>-scoring-v2.1-robust.md` suivant le format M1..M13 existant.

````markdown
# Contexte

Lis `docs/next/MA.md` en entier. C'est le brief actionnable du module MA
(Scoring v2.1-ROBUST foundation). Il référence audit, session B, deep-search
synthèse, et les 3 rapports sources. Tous les chemins sont dans le document.

# Tâche

Produire `docs/specs/M14-scoring-v2.1-robust.md` suivant strictement le format
des specs M1..M13 existantes (§ numérotées : TL;DR, Motivation, Scope, User
stories, Architecture, Algorithmes, DTOs, Settings, Invariants sécurité, Test
plan, Impact existant, Migration, Commandes vérif, Hors scope, Notes
implémentation, Prompt implémentation, Commit message proposé).

Bump number : prochain après M13 = M14.

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Conventions + §Sécurité (scoring versions append-only, grep
  anti-leak secrets)
- `docs/specs/M13_dry_run_observability_spec.md` comme **template de forme**
  (le plus récent et le mieux structuré, 1200 lignes)
- `docs/specs/M12-scoring-v2.md` comme **référence de contenu** (précédente
  itération scoring, format DTO v2, migration Alembic 0006)
- Sections deep-search citées dans MA.md §2 (Contexte) : synthèse §2.2 F01,
  F03, F04, F05, F07 ; Claude §3.1, §4.1 v2.1-ROBUST, §6 audit mapping ;
  Gemini §Analysis v2 Factors + §5 réponses
- Audit findings H-006, H-009, H-014, M-001, C-006, C-007 dans
  `docs/audit/2026-04-24-polycopy-code-audit.md`

# Contraintes

- **Lecture seule** sur `src/`, `tests/`, `docs/audit/`, `docs/deepsearch/`,
  `docs/bug/`, `docs/next/` (sauf MA.md à référencer)
- **Écriture uniquement** `docs/specs/M14-scoring-v2.1-robust.md`
- **Ne pas inventer** : chaque item spec doit tracer vers un item MA.§3 (MA.1-MA.8)
- **Longueur cible** : 1000-1500 lignes (cohérent avec M12 scoring v2 et M13)
- **Détailler les algos** dans §Algorithmes avec pseudocode Python dense
  (rank_normalize, compute_brier_p_yes, median_sortino_calmar, etc.)
- **Migration Alembic** : aucune (MA est scoring-only, pas de nouveau schéma DB).
  La table `trader_scores` accepte déjà `scoring_version: str` (migration 0006 M12).
- **Hypothèses empiriques H-EMP-1 + H-EMP-2** : inclure dans §Notes implémentation
  + §Commandes de vérification (script `scripts/validate_ma_hypotheses.py`).

# Livrable

- Le fichier `docs/specs/M14-scoring-v2.1-robust.md` complet
- Un ping final ≤ 10 lignes : nombre de tests ajoutés estimés, charge cumulée,
  ordre de commits recommandé (MA.1 → MA.8 séquentiel ou grouping pertinent).
````

## 11. Notes d'implémentation

### Piège : renormalisation des poids post-drop timing_alpha

Après `timing_alpha=0`, les poids restants **ne somment pas à 1.0**. La renormalisation proportionnelle donne :
```
risk_adjusted : 0.25 / 0.80 = 0.3125
calibration   : 0.20 / 0.80 = 0.2500
specialization: 0.15 / 0.80 = 0.1875
consistency   : 0.10 / 0.80 = 0.1250
discipline    : 0.10 / 0.80 = 0.1250
sum = 1.0000 ✓
```
Attention en testing : le `pytest.approx(score, abs=1e-6)` ne passera pas si les poids ne somment pas exactement à 1.0.

### Piège : ordering des corrections

Faire MA.1 → MA.8 dans l'**ordre strict** suivant pour éviter cascades de test breakage :
1. **MA.1** (drop timing_alpha) : simple, pas de dépendance
2. **MA.7** (margin 0.10) : config only, pas de dépendance
3. **MA.5** (flip HHI) : simple, change un signe
4. **MA.6** (zombie_ratio fix) : localisé
5. **MA.3** (Sortino sentinel) : impacte risk_adjusted, donc les pondérations post-MA.1 matter
6. **MA.2** (rank transform) : le plus invasif, impacte tous les facteurs normalisés
7. **MA.4** (Brier P(YES)) : nécessite data re-capture des entry prices YES (peut nécessiter migration de `detected_trades` si pas déjà là)
8. **MA.8** (ship v2.1) : assemblage final, commit version bump

### Référence spéciale : Winsor 1947 et rank transforms

Winsor's method publiée par Dixon 1960 ([Grokipedia](https://grokipedia.com/page/Winsorizing)) suppose :
- Distribution symétrique (notre pool est right-skewed après gates)
- N ≥ 20 pour p5-p95 meaningful (notre pool est N=13)

Rank transform (Spearman 1904) est robuste par construction sur small N. C'est pourquoi Convexly Edge Score V3b (Perplexity E2) utilise pool 8656 wallets : ils peuvent se permettre winsorisation. Nous non.

### Références literature

- **Brier 1950** : définition Brier score, exige forecast = probability distribution over mutually exclusive outcomes ([AMS](https://journals.ametsoc.org/view/journals/mwre/78/1/1520-0493_1950_078_0001_vofeit_2_0_co_2.xml))
- **Gneiting & Raftery 2007** : JASA 102(477):359-378, strict propriety proof ([PDF UW](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf))
- **Sortino & van der Meer 1991** : Downside risk, semi-deviation formula ([Groningen repo](https://research.rug.nl/en/publications/downside-risk-capturing-whats-at-stake-in-investment-situations/))
- **Rollinger & Hoffman 2013** : Sortino vs Sharpe corrélation r>0.95 ([CME PDF](https://www.cmegroup.com/education/files/rr-sortino-a-sharper-ratio.pdf))
- **Mitts & Ofir 2026** : informed trading Polymarket, 69.9% WR >60σ ([Harvard Corpgov](https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/))
- **MSCI 2018** : equal-weighted multi-factor robust sur 36y ([PDF](https://www.msci.com/documents/10199/248121/MULTI-FACTOR+INDEXES+MADE+SIMPLE/1c426b20-0947-4d20-88b1-8da45b77a742))

### Questions ouvertes pertinentes à MA

- **Q2** (synthèse §11) : latence WSS market channel end-to-end — pas directement MA mais post-MA.8 si couverture v2.1 n'atteint pas 40+, investiguer si la latence contribue à l'asymétrie.
- **Q3** : corrélation Brier/PnL négative Convexly — H-EMP-3 testera sur nos data post-ship MA.8.
