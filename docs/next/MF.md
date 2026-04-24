# MF — Wash detection + Mitts-Ofir informed-trader screen (v2.2 capstone)

**Priorité** : 🟠 P2 (capstone scoring, conditionnel à MA+MB shippés + 30j data)
**Charge estimée** : L (6-8 jours)
**Branche suggérée** : `feat/wash-mittsofir-v2.2`
**Prérequis** : **MA shipped** (v2.1 stable), **MB shipped** (internal_pnl factor actif), **30 jours** de data internal_pnl_score collectée post-MB ship
**Bloque** : — (capstone terminal de la pipeline scoring)

---

## 1. Objectif business

Shipper **scoring v2.2-DISCRIMINATING** — la formule scoring finale pour 2026. Remplace la v2.1-ROBUST (MA) + v2.1.1 (MB) par une formule qui intègre trois signaux manquants critiques selon les 3 deep-searches :

1. **Sirolly wash cluster detection continuous** — empêche le scoring de récompenser le wash trading (25% du volume Polymarket selon Columbia 2025).
2. **Mitts-Ofir informed-trader composite** — récompense activement le pattern "insider" (timing_alpha pré-event + concentration HHI + conviction sizing). Ces wallets ont 69.9% WR > 60σ above chance.
3. **Arbitrage bot exclusion already in MB** + **convergence signal (cross-wallet agreement)** pour enrichir le signal qualitatif.

**Livrable** : `SCORING_VERSION="v2.2"`, shadow period 14j contre v2.1.1. Cutover manuel basé sur backtest Brier-skill + realized PnL observed.

## 2. Contexte & problème observé

### Pourquoi c'est la capstone

**Claude §4.2** documente v2.2-DISCRIMINATING comme la formule **avec maximum de discriminating power** :
```
score_v2.2 = 0.30·internal_pnl_score        # POLYCOPY-SPECIFIC (MB)
           + 0.25·informed_score             # MITTS-OFIR COMPOSITE (MF)
           + 0.15·rank(sortino_robust)       # MA.3 already
           + 0.15·rank(calibration_proper)   # MA.4 already (P(YES) + pool mean)
           + 0.15·wash_penalty × not_arb_bot # SIROLLY (MF) + MB.7 gate already
```

v2.1 (MA) corrige les bugs structurels. v2.1.1 (MB) ajoute internal_pnl. **v2.2 (MF) ajoute les deux facteurs manquants** : informed_score composite + wash continuous — et assemble la formule finale.

### Findings référencés

- **[F26] 🟢 3/3** (synthèse §3.2) : Sirolly wash cluster detection indispensable. **Perplexity A4 + B4** : "25% of Polymarket's historical volume 2022-2025 is likely wash trading, suspicious weekly volume peaking near 60% in December 2024, 14% of wallets suspicious, cluster of more than 43,000 wallets". **Gemini §"Detecting Wash-Trading Clusters"** : "up to 45% of trading volume on Polymarket is artificial wash trading... 45% of sports market volume and up to 95% of peak election volume... graph clustering algorithm required". Recommande `networkx` Python. **Claude A7 + §9 item 10** : "Iterative graph-based closed-cluster detection algorithm — directly reusable signal for polycopy's wash-cluster blacklist. ~3-4 days for a Python port of the iterative redistribution algorithm".

- **[F40] 🟡 2/3** (synthèse §3.2) : Mitts-Ofir informed trading pattern. **Gemini §"Literature Survey"** : "Mitts and Ofir (2026) published the seminal work... analyzing specific wallets such as 'Magamyman', they identified statistically anomalous trading patterns occurring hours before public announcements. Massive, sudden volume from newly created, highly concentrated wallets is the primary indicator of asymmetric information." **Claude A6 + C9** : "Flagged wallets = 69.9% win rate (>60σ above chance). This is an empirical factor recipe for Polymarket specifically". Citations [Harvard Corpgov](https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/), [SSRN 6426778](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6426778). **5 facteurs Mitts-Ofir** : (1) bet size anomaly, (2) profitability, (3) pre-event timing, (4) directional concentration (HHI), (5) market-market concentration.

- **[F36] 🟡 2/3** (synthèse §3.2) : Convergence / cross-wallet agreement signal. **Claude §2.3** : "convergence / cross-wallet agreement is a powerful signal (Bullpen smart-money feed; polymarket.tips convergence tag). Your scoring doesn't use this at all."

- **[F07] 🟡 résolu MA.5** : HHI flip déjà fait en MA (pénalité → signal positif). MF utilise la version corrigée de `specialization` dans le composite Mitts-Ofir avec poids augmenté.

- **Synthèse §2.5** formule v2.2-DISCRIMINATING complète documentée.

- **Perplexity B4** : cas concrets wash clusters :
  - Crypto Bubble Tea : 109k accounts trading 932.7M shares, 94.1% intra-cluster
  - Polyloly Louvain clustering : detection framework existant
  - Columbia study cluster 43k wallets dans sub-cent markets

### Pourquoi 30 jours de data internal_pnl

MB.1 démarre la collecte de `internal_pnl_score` au ship. Pour être statistiquement significatif dans la formule v2.2 (poids 0.30), il faut **au minimum 10 positions closed par wallet** (MB.1 cold-start threshold). Avec nos wallets ACTIVE actuels, 10 positions closed = 20-30 jours calendrier minimum. MF attend ce minimum.

### Sessions originales mappées

Aucune session A-E ne couvre directement MF. Les items sont nés **directement** des deep-searches :
- Sirolly wash : Claude §9 item 10, Gemini §wash clusters, Perplexity A4/B4
- Mitts-Ofir composite : Gemini §literature, Claude A6/C9
- Convergence : Claude §2.3

Synthèse §7.1 a proposé "Session F" dont MF hérite directement.

## 3. Scope (items détaillés)

### MF.1 — Sirolly Python port (iterative graph-based wash cluster detection)

- **Location** : nouveau fichier `src/polycopy/discovery/wash_detection/sirolly.py` + module `src/polycopy/discovery/wash_detection/__init__.py`
- **Ce qu'il faut faire** :
  - Port Python de l'algorithme Sirolly et al. 2025 ([SSRN 5714122](https://business.columbia.edu/faculty/research/network-based-detection-wash-trading)). Référence PDF [gamblingharm](https://gamblingharm.org/wp-content/uploads/2025/11/Polymarket-Wash-Trading-Study.pdf).
  - **Input** : adjacency graph depuis `detected_trades` (edges = trades entre wallets). Edges weighted par USD volume.
  - **Algorithm** (Sirolly paper Algorithm 2 iterative redistribution) :
    ```
    1. Construire directed graph G(nodes=wallets, edges=trades weighted by USD)
    2. Pour chaque node, compute ratio bidirectionnel : forward_weight / backward_weight
    3. Iterative redistribution : propager le "wash score" à travers le graph
       via edges avec ratio ≈ 1.0 (bi-directional trading = signal wash)
    4. Convergence : fixed-point du wash_score
    5. Output : wash_cluster_score(wallet) ∈ [0, 1] continu
    ```
  - Utiliser `networkx` (Python pur, pas de C deps). Cap graph size : top 10k wallets par volume (filtre initial).
  - **Décision D1** : recalcul **1× par jour** (nouveau scheduler `WashDetectionScheduler` co-lancé dans `DiscoveryOrchestrator`). Pas chaque cycle discovery (trop coûteux — O(E × iterations) sur milliers d'edges).
  - Nouvelle table `wash_cluster_scores` (migration 0010 nouvelle) :
    ```sql
    CREATE TABLE wash_cluster_scores (
        wallet_address VARCHAR(42) PRIMARY KEY,
        wash_score FLOAT NOT NULL,
        computed_at DATETIME NOT NULL,
        cluster_size INTEGER,
        bi_directional_ratio FLOAT
    );
    ```
  - Usage : `MetricsCollectorV2._compute_wash_penalty(wallet) = 1 - wash_cluster_scores.wash_score`.
- **Tests requis** :
  - `test_sirolly_detects_closed_cluster_in_synthetic_graph` (fixture: 5 wallets A→B→C→A avec ratios ~1.0)
  - `test_sirolly_does_not_flag_directional_trader` (fixture: wallet buying from many, selling to many, pas de loop)
  - `test_sirolly_converges_within_100_iterations`
  - `test_sirolly_handles_10k_wallets_in_reasonable_time` (benchmark <5min)
  - `test_wash_detection_scheduler_runs_daily`
  - `test_wash_cluster_scores_persisted_and_read`
- **Sources deep-search** : F26, Claude §9 item 10, Gemini §wash, Perplexity A4+B4.
- **Charge item** : 3 jours

### MF.2 — Mitts-Ofir informed-trader composite

- **Location** : nouveau fichier `src/polycopy/discovery/scoring/v2/factors/informed_score.py` + extend `src/polycopy/discovery/metrics_collector_v2.py`
- **Ce qu'il faut faire** :
  - Composite 4 sous-facteurs (Claude §4.2 v2.2-DISCRIMINATING) :

    1. **`avg_entry_time_before_resolution(wallet)`** — pour chaque position résolue, calcul `resolution_timestamp - entry_timestamp`. Earlier = more informed. Rank normalize.
    2. **`market_hhi(wallet)`** — déjà calculé par MA.5 (specialization factor). Ici utilisé **positif** (high HHI = signal).
    3. **`conviction_sigma(wallet)`** — `std(trade_size) / mean(trade_size)` normalized per wallet bankroll. High variance = Kelly-like sizing (signal sophistication). Note : **sera étendu en MG** (Kelly proxy dédié). Ici version baseline.
    4. **`liquidity_adjusted_roi(wallet)`** — `PnL / sum(bid_ask_spread × filled_size)`. Pénalise les wallets dont le PnL vient de mover des books illiquides (pas de vrai signal). Référence Kyle's λ framework. Note : **détaillé en MG**. Ici version baseline.

  - Composite :
    ```python
    informed_score(wallet) = 0.25·rank(avg_entry_time_before_resolution(wallet))
                           + 0.25·rank(market_hhi(wallet))
                           + 0.25·rank(conviction_sigma(wallet))
                           + 0.25·rank(liquidity_adjusted_roi(wallet))
    ```
  - **Interaction MG** : si MG ship avant MF, les 4 sous-facteurs utilisent les implémentations plus sophistiquées de MG. Sinon baseline simple.
  - Compute cache : `informed_score` recalculé à chaque cycle discovery (données Data API nécessaires, pas O(N²)).
  - Nouveau stage `trader_events.event_type="informed_trader_flagged"` si `informed_score > 0.8` (top-20% pool).
- **Tests requis** :
  - `test_informed_score_returns_high_on_fixture_insider_profile` (fixture : wallet avec pre-event entries, high HHI, high conviction_sigma, high liquidity-adjusted ROI)
  - `test_informed_score_returns_low_on_diversified_late_entrant`
  - `test_informed_score_composite_normalized_to_unit_interval`
  - `test_informed_trader_event_written_when_top_20_percent`
- **Sources deep-search** : F40, Claude §4.2 v2.2-DISCRIMINATING, Gemini §literature Mitts-Ofir.
- **Charge item** : 2 jours

### MF.3 — Convergence signal (cross-wallet agreement)

- **Location** : nouveau fichier `src/polycopy/discovery/convergence.py` + extend `src/polycopy/discovery/metrics_collector_v2.py`
- **Ce qu'il faut faire** :
  - Pour chaque trade récent sur un market, compter combien de **wallets scorés** (ACTIVE + top shadow) ont pris **la même side** dans une fenêtre 1h.
  - `convergence_score(market, side, wallet) = count(other_scored_wallets_same_side_last_1h) / total_scored_wallets_active`.
  - **Usage** : plutôt qu'un facteur direct, c'est un **booster qualitatif** sur `informed_score`. Si convergence > 0.3 ET informed_score > 0.5, alors `boosted_informed_score = informed_score × (1 + 0.2 × convergence_score)`.
  - **Décision D2** : implémentation **conservative** — bonus max +20% sur informed_score. Évite de sur-peser convergence qui peut être du bruit (30 shadows suivent 1 news sans skill particulier).
  - Nouveau event `convergence_detected` dans `trader_events` quand seuil dépassé (audit + dashboard).
  - **Attention** : la sémantique "same side" requiert un mapping YES/NO cohérent. Utiliser `asset_id` comme clé (pas `side` string qui peut être ambigu après neg_risk).
- **Tests requis** :
  - `test_convergence_score_high_when_many_wallets_agree`
  - `test_convergence_score_low_when_single_wallet`
  - `test_convergence_booster_capped_at_20_percent`
  - `test_convergence_event_written_on_threshold`
- **Sources deep-search** : F36 Claude §2.3, Bullpen Fi smart money feed reference.
- **Charge item** : 1 jour

### MF.4 — Assembly formule v2.2-DISCRIMINATING

- **Location** : [src/polycopy/discovery/scoring/v2/aggregator.py](../../src/polycopy/discovery/scoring/v2/aggregator.py) + [src/polycopy/discovery/scoring/__init__.py](../../src/polycopy/discovery/scoring/__init__.py) registry
- **Ce qu'il faut faire** :
  - Ajouter `"v2.2"` au registry `SCORING_VERSIONS_REGISTRY`.
  - `compute_score_v2_2(wallet)` appelle :
    ```python
    score_v2.2 = (
        0.30 * internal_pnl_score(wallet)           # MB.1
      + 0.25 * informed_score(wallet)               # MF.2 + MF.3 booster
      + 0.15 * rank(sortino_robust(wallet))         # MA.3
      + 0.15 * rank(calibration_proper(wallet))     # MA.4
      + 0.15 * wash_penalty(wallet) * not_arb_bot(wallet)  # MF.1 + MB.7
    )
    ```
  - Cold-start handling (cohérent MB.2) :
    - `internal_pnl_score = None` (wallet <10 copied closed) → weight redistribué
    - `wash_penalty = 1.0` par défaut si Sirolly pas encore tourné pour le wallet
    - `informed_score` cold-start si pas assez de positions résolues pour Mitts-Ofir (<20 résolues)
  - Shadow period : `SCORING_VERSION="v2.1.1"` pilote, `SCORING_V2_2_SHADOW_DAYS=14` active dual-compute v2.1.1 + v2.2 en parallèle.
  - **Décision D3** : append-only — les rows `trader_scores` v2.2 sont **en plus** de v2.1.1, pas à la place. Permet backtest a posteriori.
  - Bump `SCORING_VERSION` default à `"v2.2"` **uniquement après** validation shadow + backtest (cutover manuel, cf. MF.5).
- **Tests requis** :
  - `test_scoring_v2_2_registered_in_registry`
  - `test_scoring_v2_2_shadow_period_dual_compute`
  - `test_scoring_v2_2_cold_start_handling_redistributes_weights`
  - `test_no_retroactive_rewrite_of_v2_1_1_scores`
- **Sources** : Claude §4.2 v2.2-DISCRIMINATING.
- **Charge item** : 1 jour

### MF.5 — Backtest script + cutover criteria

- **Location** : nouveau `scripts/backtest_scoring_v2_2.py` + `assets/scoring_v2_2_labels.csv` (seed labelé)
- **Ce qu'il faut faire** :
  - Script qui tourne post-shadow 14j v2.2 :
    1. Dump `trader_scores` v2.1.1 + v2.2 + `my_positions.realized_pnl` sur la shadow period.
    2. Pour chaque wallet, compute Brier-skill v2.1.1 vs Brier-skill v2.2 sur les positions résolues pendant la shadow period.
    3. Compute Spearman rank correlation entre `score_v2.2` et `realized_copy_pnl_30d` (nos data polycopy-specific).
    4. Report HTML : `docs/development/scoring_v2_2_backtest_report.md` avec tables + distributions.
  - **Cutover criteria** (MA.8 pattern étendu) :
    - `brier_skill_v2.2_top10 > brier_skill_v2.1.1_top10 + 0.01` sur set labelé
    - `ρ(score_v2.2, realized_copy_pnl_30d) > ρ(score_v2.1.1, realized_copy_pnl_30d) + 0.05` (amélioration significative)
    - Aucun wallet top-10 v2.2 flagged wash_cluster_score > 0.5 (sanity check)
  - Flag `SCORING_V2_2_CUTOVER_READY=true` dans `.env` manuel uniquement après ces conditions remplies.
  - Dashboard `/scoring/v2/backtest` (nouveau route read-only, hors scope MH mais mineur) affiche le rapport.
- **Tests requis** :
  - `test_backtest_script_runs_on_fixture_data`
  - `test_cutover_criteria_logic`
- **Sources** : Pattern cohérent M12 cutover process (`SCORING_V2_CUTOVER_READY`).
- **Charge item** : 1 jour

### MF.6 — Migration Alembic 0010 : nouvelle table `wash_cluster_scores`

- **Location** : nouveau fichier `alembic/versions/0010_wash_cluster_scores.py`
- **Ce qu'il faut faire** :
  - Migration structurelle pour nouvelle table (cf. MF.1 schema).
  - Index sur `(wallet_address, computed_at)` pour lookup rapide.
  - Backfill : **vide au start** (MF.1 scheduler va la peupler au premier run).
  - Upgrade + downgrade documentés.
- **Tests requis** :
  - `test_migration_0010_upgrade_downgrade_idempotent`
  - `test_migration_0010_wash_cluster_scores_schema`
- **Charge item** : 0.5 jour

## 4. Architecture / décisions clefs

- **D1** : Sirolly recalcul 1× par jour (`WashDetectionScheduler`), pas chaque cycle discovery. Justification : O(E × iterations) sur milliers d'edges coûteux. 1/jour = fréquence suffisante (wash clusters évoluent lentement).
- **D2** : Convergence booster conservateur (+20% max sur informed_score). Justification : évite sur-pondération convergence qui peut être du bruit news-driven.
- **D3** : v2.2 dual-compute avec v2.1.1, append-only. Justification : permet backtest rigoureux, rollback propre, audit trail sacré (cohérent CLAUDE.md §Scoring versioning).
- **D4** : Mitts-Ofir composite = 4 sous-facteurs équi-pondérés. Justification : Gemini et Claude convergent sur les 4 éléments, équi-pondération évite sur-fitting absent de backtest empirique préalable.
- **D5** : Nouvelle table `wash_cluster_scores` séparée (pas column dans `target_traders`). Justification : données recalculées 1×/jour pour **tous les wallets** (incl. hors pool), séparation propre.
- **D6** : Cold-start `wash_penalty=1.0` si pas de data Sirolly. Justification : neutre par défaut, mieux que None (0.5 biaiserait). Le wallet est "pas encore analysé", pas "suspect".
- **D7** : Cutover v2.2 **manuel** via flag `SCORING_V2_2_CUTOVER_READY=true`. Justification : pattern M12 éprouvé, décision business critique, pas d'auto-flip.

## 5. Invariants sécurité

- **Triple garde-fou M3 + 4ᵉ M8** : intact. MF est pur scoring/discovery.
- **Append-only scoring versions** : v2.2 ajoute, ne modifie pas v2.1/v2.1.1. Rows historiques sacrées.
- **Zéro secret loggé** : nouveaux events (`wash_cluster_computed`, `informed_trader_flagged`, `convergence_detected`, `v2_2_cutover_ready`) n'incluent que wallets publics + numeric. Grep test.
- **Blacklist double-check préservé** : MF.1 ajoute `wash_penalty` score continu, **mais** `BLACKLISTED_WALLETS` env reste la source de vérité pour exclusion absolue. Un wallet wash_score=0.9 n'est pas auto-blacklisté (conservateur — user decision).
- **Read-only Data API + Gamma** : MF.1 + MF.2 + MF.3 lisent uniquement data publique, pas de creds CLOB.
- **Sirolly graph computation** : local (in-memory + SQLite), aucune data exfiltrée.
- **Integrity cross-module** : MF dépend de MB (internal_pnl_score) et MA (rank transform, median Sortino, Brier P(YES)). Si MA/MB non shippés → cold-start neutre partout (v2.2 dégrade gracieusement vers équi-pondération).

## 6. Hypothèses empiriques à valider AVANT ship

- **H-EMP-7** (synthèse §8) : distribution HHI de nos 8 pilot wallets suit Mitts-Ofir (high HHI = insider-like). **Méthode** : calcul HHI par wallet (déjà possible post-MA.5) + cross-ref avec observed performance 14j MA shadow. **Seuil go** : au moins 1 wallet ACTIVE avec HHI > 0.5 ET positive PnL observé. Sinon, le pattern Mitts-Ofir n'est pas présent dans notre pool (pas de wallets insiders ciblés par notre discovery).
- **H-EMP-8** (synthèse §8) : wash ratio catégorie-par-catégorie confirme Sirolly (45% sports). **Méthode** : audit `detected_trades` par Gamma category, calcul `reverse_trades_ratio` (trades entre mêmes paires de wallets). **Seuil informatif** : si notre pool sports a > 30% bi-directional ratio, adoption Sirolly confirmée urgente. Si < 10%, moins critique (mais garder).
- **H-EMP-11** : arbitrage bots dans notre pool shadow — résultat de MB.7 gate. Si MB.7 a filtré beaucoup de wallets, MF est d'autant plus utile (pool cleanup).
- **H-EMP-15** (synthèse §8) : Calibration v2.2 fix (P(YES) vs P(side)) change le ranking significativement. **Méthode** : compute Brier P(YES) vs Brier P(side) en parallèle (MA.4 déjà fait pour P(YES)), measurer Spearman entre les deux rankings. **Seuil go** : ρ < 0.9 (significantly different, donc MA.4 était bien un vrai fix pas cosmétique).

## 7. Out of scope

- **v2.3-LIGHTWEIGHT two-tier architecture** (Claude §4.3) : hors scope MF. Potential future module pour **discovery stage** (ranking pool admission) si besoin de cheap scoring. **Discuté en docs/development/** mais pas prioritaire tant que v2.2 assure le active scoring.
- **Thompson Sampling Trend-Aware** (Gemini §"Promotion/Demotion") : hors scope MF. Rank-based approach (MB.3) suffisant pour v2.2. Future spec si besoin dynamic rebalancing.
- **Sybil heuristics étendus** (gas fingerprinting, timestamp ms clustering, correlated drawdowns) : Gemini §"Sybil Beyond Age" unique, hors scope MF (features exploratoires). Spec future si signal intéressant.
- **Window 180j + exponential decay half-life 30j** : Gemini §"Academic Consensus Window", hors scope v2.2. Claude utilise 90j dans v2.2 formula. Future v3 si besoin.
- **Resolution-path awareness** (Claude §9 item 6) : wallets qui évitent oracle-dispute markets. Hors scope, spec future.
- **Adversarial / anti-copy bait detection** (Claude §9 item 9) : nécessite 30j+ post-MF ship pour observer patterns. Future spec.
- **News-alpha factor** (entry vs public news timestamp) : Gemini §"Alternative Factors". Hors scope, nécessite news API externe (~$20/mo + engineering). Spec future.

## 8. Success criteria

1. **Tests ciblés verts** : ~25 nouveaux tests unit + 5 integration.
2. **Hypothèses empiriques H-EMP-7, H-EMP-8 passées** sur data actuelle pré-ship.
3. **Sirolly scheduler opérationnel** : post-ship, scheduler tourne 1×/jour, peuple `wash_cluster_scores` pour ≥90% des wallets actifs. Temps d'exécution < 10 min.
4. **Couverture informed_score** : post-ship 7j, ≥70% des wallets ACTIVE ont `informed_score != None` (assez de positions résolues).
5. **Shadow v2.2 vs v2.1.1 convergente** : Spearman rank correlation entre les deux rankings > 0.5 (pas complètement décorrélés) mais < 0.95 (apporte information nouvelle).
6. **Backtest cutover report** : post-14j shadow, report HTML montre :
   - Brier-skill v2.2 vs v2.1.1 delta
   - Top-10 v2.2 vs top-10 v2.1.1 delta ranks
   - Wash-flagged wallets dans top-10 v2.2 (attendu : 0)
7. **Manual cutover** : user peut flip `SCORING_V2_2_CUTOVER_READY=true` en confiance après review backtest.
8. **Dashboard `/traders/scoring`** affiche v2.1.1 | v2.2 | delta_rank columns (extension MH ou ici).

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MF.1 | — | — (new, nullpart) | F26 (3/3), Claude §9 item 10, Gemini §wash, Perplexity A4+B4 | #15 |
| MF.2 | — | — (new) | F40 (2/3), Claude §4.2 + A6, Gemini §literature | #16 |
| MF.3 | — | — (new) | F36 (2/3), Claude §2.3, Bullpen Fi | #35 |
| MF.4 | — | — (assembly) | Claude §4.2 v2.2 formula | #27 |
| MF.5 | — | — (new) | Cohérent pattern M12 cutover | — |
| MF.6 | — | — (migration) | — | — |

## 10. Prompt de génération de spec

````markdown
# Contexte

Lis `docs/next/MF.md` en entier. C'est le brief actionnable du module MF
(Wash detection Sirolly + Mitts-Ofir informed composite + v2.2 capstone). C'est
le capstone scoring conditionnel à MA/MB shippés + 30j internal_pnl data
collectée.

# Tâche

Produire `docs/specs/M20-scoring-v2.2-discriminating.md` suivant strictement le
format des specs M1..M19 existantes.

Numéro : M20 (après MA=M14, MB=M15, MC=M16, MD=M17, ME=M18, MG=M19 si shippé
entre temps).

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Scoring v2 + §Conventions (append-only scoring versions, cutover
  manual pattern M12)
- `docs/specs/M14-scoring-v2.1-robust.md` (MA shipped)
- `docs/specs/M15-anti-toxic-lifecycle.md` (MB shipped, internal_pnl_score)
- `docs/specs/M12-scoring-v2.md` comme référence contenu v2 original (gates
  durs, v2 factors)
- `docs/specs/M13_dry_run_observability_spec.md` comme template de forme
- Synthèse §2.2 F01-F05 + §2.3 F07+F11 + §2.4 F14 + §3.2 F26+F40+F36 +
  §5.4 formules
- Claude §4.2 v2.2-DISCRIMINATING + §9 item 10 Sirolly port + A6+A7+A10 literature
- Gemini §"Detecting Wash-Trading Clusters" + §"Literature Survey" Mitts-Ofir
- Perplexity A4 + B4 wash trading evidence + A1 top wallets performance
- Papers :
  - Sirolly et al. 2025 [SSRN 5714122](https://business.columbia.edu/faculty/research/network-based-detection-wash-trading)
  - Mitts & Ofir 2026 [SSRN 6426778](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6426778)

# Contraintes

- Lecture seule src/ + docs
- Écriture uniquement `docs/specs/M20-scoring-v2.2-discriminating.md`
- Longueur cible : 1500-2000 lignes (capstone module, le plus gros)
- **Migration Alembic 0010** : nouvelle table `wash_cluster_scores`, documenter
- **Scripts** : détailler `scripts/backtest_scoring_v2_2.py` en §Commandes vérif
- **Hypothèses empiriques H-EMP-7, H-EMP-8, H-EMP-11, H-EMP-15** : §Notes
  implémentation avec plan validation

# Livrable

- Le fichier `docs/specs/M20-scoring-v2.2-discriminating.md` complet
- Un ping final ≤ 12 lignes : tests estimés, charge cumulée, ordre commits
  (recommandé : MF.6 migration → MF.1 Sirolly port → MF.2 informed → MF.3
  convergence → MF.4 v2.2 assembly → MF.5 backtest script)
````

## 11. Notes d'implémentation

### Piège : Sirolly algorithm complexité

Sirolly paper Algorithm 2 fait **iterative redistribution** du wash score à travers le graph. **Pas juste connected components** (approche naïve qui rate les clusters partiellement connectés via trades légitimes intermédiaires).

Pseudocode Python (simplified) :
```python
def sirolly_wash_scores(graph: nx.DiGraph, iterations: int = 50) -> dict[str, float]:
    # Initial score based on bi-directional ratio
    scores = {node: initial_wash_score(node, graph) for node in graph.nodes}

    for _ in range(iterations):
        new_scores = {}
        for node in graph.nodes:
            # Redistribute from neighbors weighted by bi-directional edge strength
            neighbor_contribution = sum(
                scores[neighbor] * bi_directional_weight(node, neighbor, graph)
                for neighbor in graph.neighbors(node)
            )
            new_scores[node] = alpha * scores[node] + (1 - alpha) * neighbor_contribution

        if max_delta(scores, new_scores) < convergence_threshold:
            return new_scores
        scores = new_scores

    return scores
```

**Attention** : le paper Sirolly n'est pas intégralement accessible (SSRN abstract + gamblingharm PDF partiel). Le spec M20 devra **re-vérifier** l'algorithme exact via les sources disponibles + éventuellement contacter les auteurs si ambiguité. **Fallback conservative** : si reimplementation impossible, utiliser un **connected-components naïf** comme baseline (moins précis mais shippable).

### Piège : Mitts-Ofir `avg_entry_time_before_resolution` sur neg_risk markets

Pour un market neg_risk avec 10 outcomes (ex: "Best Picture 2026"), la `resolution_timestamp` est la même pour tous les tokens → `entry_time_before_resolution` est mesurable.

**Mais** pour les trades intermédiaires (achat puis vente avant résolution), Mitts-Ofir compte le premier entry sur le side final gagnant. Implémentation : pour chaque position closed, prendre `entry_time_of_first_trade_on_winning_side`.

### Piège : convergence cold-start

Au boot, peu de wallets ont scoré récemment → convergence_score est biaisé par la taille du pool en croissance. **Mitigation** : ne pas appliquer le booster convergence tant que `count(scored_wallets_active) < 5`. Cold start, signal inutilisable.

### Piège : Sirolly scheduler timing vs cycle discovery

Discovery cycle 6h. Sirolly scheduler 24h. Ces deux peuvent se croiser. **Invariant** : le `MetricsCollectorV2._compute_wash_penalty()` lit **la version en DB** (peut être stale jusqu'à 24h). Acceptable : wash patterns évoluent lentement.

**Edge case** : si un wallet est nouveau (pas encore scoré par Sirolly), `wash_penalty=1.0` (neutre par défaut D6). Le wallet peut recevoir un top score v2.2 erroné avant d'être analysé. **Mitigation** : ne pas promote ACTIVE un wallet sans au moins 1 passage Sirolly (gate added dans `DecisionEngine`).

### Références externes

- **Sirolly, Ma, Kanoria, Sethi 2025** : [Columbia paper](https://business.columbia.edu/faculty/research/network-based-detection-wash-trading), [gamblingharm PDF partiel](https://gamblingharm.org/wp-content/uploads/2025/11/Polymarket-Wash-Trading-Study.pdf), [Decrypt summary](https://decrypt.co/347842/columbia-study-25-polymarket-volume-wash-trading), [Columbia Network-Based](https://www.researchgate.net/publication/397692662_Network-Based_Detection_of_Wash_Trading).
- **Mitts & Ofir 2026** : [Harvard Corpgov](https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/), [SSRN 6426778](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6426778), [Columbia Law Mitts](https://www.law.columbia.edu/faculty/joshua-mitts).
- **Crypto Bubble Tea wash patterns** : [blog](https://www.cryptobbt.com/blog/massive-wash-trading-uncovered-on-polymarket). 109k accounts, 94.1% intra-cluster.
- **Polyloly Louvain clustering** : [blog](https://polyloly.com/blog/detecting-polymarket-whale-syndicates-louvain-clustering). Alternative algo (à consulter si Sirolly reimpl difficile).
- **Bullpen smart money feed** : [docs.bullpen.fi](https://docs.bullpen.fi/) + [track-polymarket-whales](https://bullpen.fi/bullpen-blog/track-polymarket-whales-smart-money). Convergence signal reference.
- **polymarket.tips archetypes** : [blog.polymarket.tips/polymarket-leaderboard-explained](https://blog.polymarket.tips/polymarket-leaderboard-explained). Early Mover / Contrarian / Precision / Convergence Participant.
- **Arbitrage $40M/an** : [Dev Genius](https://blog.devgenius.io/just-found-the-math-that-guarantees-profit-on-polymarket-and-why-retail-traders-are-just-providing-6163b4c431a2). Frank-Wolfe / Bregman projections evidence. **Context MB.7** (already gated).

### Questions ouvertes pertinentes à MF

- **Q6** (synthèse §11) : HHI distribution de nos 10 pilot wallets suit-elle Mitts-Ofir ? Validation MF.2 informed_score.
- **Q10** : arbitrage bots dans notre pool ? Post-MB.7 shipped, MF.1 + MF.4 viennent en complément pour catch ceux qui passent MB.7 mais sont dans wash clusters.
- **Q3** (synthèse §11) : corrélation Brier/PnL négative Convexly sur top-100 — MF.5 backtest testera si v2.2 évite ce piège.
