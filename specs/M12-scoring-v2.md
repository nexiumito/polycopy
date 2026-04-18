# M12 — Scoring v2 (formule hybride + gates durs + shadow period v1/v2)

Spec d'implémentation du **bundle "scoring v2"**. M11 vient de poser un pipeline temps réel observable (2-3 s p95, instrumentation par stage, WS CLOB) — le prérequis direct de cette spec (synthèse §4.1 + §7.2 : "on ne peut pas valider la qualité d'un wallet scoré si le bot rate les trades à cause de la latence"). M12 attaque le cœur produit : **quel capital mettre au travail, et sur lequel trader** ? La formule v1 M5 (`0.30·win_rate + 0.30·roi_norm + 0.20·diversity + 0.20·volume_norm`) a trois faiblesses documentées :

1. **ROI nominal rewarde les one-shots lucky** (Fredi9999-like) — cf. synthèse §1.1.
2. **Aucun gate anti-manipulation** (zombie positions, wash trading, age minimal) — synthèse §1.3.
3. **Pas de signal informationnel** (timing pré-event, spécialisation catégorielle, calibration probabiliste) — synthèse §5.

M12 livre trois volets couplés, additifs, **derrière feature flag coexistant avec v1** :

1. **Formule hybride `Score_v2`** (6 facteurs Sortino/Calmar + Brier + Mitts-Ofir + HHI + consistency + discipline) dans un nouveau sous-package `src/polycopy/discovery/scoring/v2/` — pure functions par facteur, winsorisation p5-p95 pool-wide, normalisation 0-1.
2. **Gates durs pré-scoring** (6 gates) : `cash_pnl_90d > 0`, `trade_count_90d ≥ 50`, `days_active ≥ 30`, `zombie_ratio < 0.40`, `not blacklisted`, `not in_wash_cluster`. Wallet qui rate un gate : jamais scoré, `trader_gate_rejected` écrit dans `trader_events`.
3. **Shadow period v1/v2 coexistence** : `SCORING_VERSION=v1|v2`, les deux formules tournent en parallèle pendant `SCORING_V2_SHADOW_DAYS` (default 14), seule v1 pilote `decision_engine`. Dashboard `/traders/scoring` rend les deux colonnes côte-à-côte + métriques agrégées (Brier du pool, Spearman v1/v2, delta_rank).

Source de vérité conception : [docs/development/M10_synthesis_reference.md](../docs/development/M10_synthesis_reference.md) §1 (formule), §5 (patterns), §6.2 (zombie), §6.7 (Mitts-Ofir pair→wallet), §7.1 (renumérotation — ex "M13" = **M12** aujourd'hui), §8 (deltas CLAUDE.md), §9 (open questions). Décisions figées : [memory/project_scoring_v2_decisions.md](../../.claude/projects/-home-nexium-code-polycopy/memory/project_scoring_v2_decisions.md). Deep searches référencées : `docs/development/gemini_deep_search_v2_and_more.md` §1-3, `perplexity_deep_search_v2_and_more.md` §1-3 — chargés uniquement si la synthèse ne répond pas. Spec M5 d'origine : [specs/M5-trader-scoring.md](./M5-trader-scoring.md). Spec de référence format : [specs/M11-realtime-pipeline-phase1.md](./M11-realtime-pipeline-phase1.md) (la plus récente).

> ⚠️ **M12 ne touche pas aux invariants M5/M10/M11.** Lifecycle `shadow/active/paused/pinned`, cap `MAX_ACTIVE_TRADERS`, `TRADER_SHADOW_DAYS`, `BLACKLISTED_WALLETS`, audit trail `trader_events`, kill switch 3 modes, badge Telegram, processor `filter_noisy_endpoints`, 4 garde-fous M3/M8, 6 stages latence, table `trade_latency_samples` — tout reste intact. **Tant que `SCORING_VERSION=v1` (default), le comportement est strictement M5 inchangé.** Le flip vers v2 est **manuel** et conditionné à un rapport de backtest validé.

---

## État actuel vs livrable (bullets de travail)

Avant de rédiger le design, point explicite sur ce qui existe déjà et ce qui manque — économise des heures d'implémentation.

**Existant (réutilisé tel quel, zéro ligne modifiée)** :
- [src/polycopy/discovery/scoring.py](../src/polycopy/discovery/scoring.py) : `SCORING_VERSIONS_REGISTRY: dict[str, Callable[[TraderMetrics], float]] = {"v1": _compute_score_v1}`. Registry extensible — M12 ajoute une entrée `"v2"`.
- [src/polycopy/discovery/dtos.py:44](../src/polycopy/discovery/dtos.py#L44) : `TraderMetrics` frozen Pydantic. Contient `win_rate, realized_roi, herfindahl_index, total_volume_usd, resolved_positions_count`. M12 étend avec un **nouveau** DTO `TraderMetricsV2` (pas de modif in-place).
- [src/polycopy/storage/models.py:262](../src/polycopy/storage/models.py#L262) : `TraderScore` table append-only, colonne `scoring_version: String(16)`. Accepte déjà `"v1"`, `"v2"`, `"v2.1"`, etc. Zéro migration DDL sur cette table.
- [src/polycopy/storage/models.py:288](../src/polycopy/storage/models.py#L288) : `TraderEvent` table avec `event_type: String(32)` + `reason: String(128)` + `event_metadata: JSON`. M12 y écrit un nouveau `event_type="gate_rejected"` — aucune DDL.
- [src/polycopy/discovery/decision_engine.py](../src/polycopy/discovery/decision_engine.py) : lifecycle M5 complet. **Pas touché par M12.** v2 calcule un score parallèle mais ne l'injecte pas dans `DecisionEngine.decide` tant que `SCORING_VERSION=v1`.
- [src/polycopy/discovery/orchestrator.py:178](../src/polycopy/discovery/orchestrator.py#L178) : point d'injection du scoring dans la boucle. M12 ajoute un appel dual `compute_both_v1_v2(...)` sous flag shadow.
- [src/polycopy/dashboard/templates/base.html:126](../src/polycopy/dashboard/templates/base.html#L126) : sidebar `/traders`. M12 ajoute un sous-onglet `/traders/scoring` — patch minimal.
- Table `trader_events` : write `gate_rejected` + `scored_v2_shadow` avec reasons explicites.

**Manquant (livré par M12)** :
- Table `trader_daily_pnl` (migration **0006**) : equity curve quotidienne par wallet → source Sortino/Calmar. Append-only, index `(wallet_address, date)` unique.
- `TraderDailyPnlWriter` scheduler (nouveau, co-lancé dans `DiscoveryOrchestrator`) : snapshot quotidien de `sum(current_value) + sum(realized_pnl cumul)` pour chaque wallet en status ∈ {shadow, active, paused, pinned}.
- Sous-package `src/polycopy/discovery/scoring/v2/` : 6 `factors/*.py` pure functions + `normalization.py` (winsorisation) + `gates.py` (6 gates) + `aggregator.py` (coordonne tout).
- DTO `TraderMetricsV2` : extension de `TraderMetrics` avec `sortino_90d, calmar_90d, brier_90d, brier_baseline_pool, timing_alpha_weighted, hhi_categories, monthly_pnl_positive_ratio, zombie_ratio, sizing_cv, cash_pnl_90d, trade_count_90d, days_active, monthly_equity_curve`.
- DTO `ScoreV2Breakdown` : les 6 sous-scores bruts + normalisés + score final (audit/drill-down dashboard).
- Nouveau `MetricsCollectorV2` (wrapper autour de `MetricsCollector` existant) : fetch additionnel `trader_daily_pnl` DB + `/activity` pre-event window + Gamma categories.
- Script `scripts/backtest_scoring_v2.py` + fixture `assets/scoring_v2_labels.csv` + rapport HTML/JSON.
- Onglet dashboard `/traders/scoring` : table v1/v2/delta_rank + chart agrégé (Brier pool, Spearman).
- 5 nouveaux feature flags Pydantic (§6).
- 3 passages CLAUDE.md (§10).

**Contradictions détectées entre la synthèse et l'état actuel du code** (à signaler en préambule, mais sans bloquer) :

- **Format `scoring_version`** : la synthèse §1.4 écrit `"1"`/`"2"`. Le code réel [discovery/scoring.py:50](../src/polycopy/discovery/scoring.py#L50) utilise `"v1"`. Le prompt utilisateur écrit `Literal["1", "2"]`. **Résolution M12** : on garde `Literal["v1", "v2"]` (match le code, backward-compat M5 stricte, extensible vers `"v2.1"` sans rewrite).
- **Numérotation synthèse §7.1** : la synthèse écrit "M13 = scoring v2". Puisque M10 + M11 sont mergés, scoring v2 est bien **M12**. Reporter à §1.3 de cette spec.
- **Table `TraderDailyPnl`** : la synthèse §1.5 annonce "migration Alembic 0005". 0005 est déjà utilisée par M11 latency (cf. [alembic/versions/0005_m11_latency_samples.py](../alembic/versions/0005_m11_latency_samples.py)). **Nouvelle migration M12 = 0006.**
- **`zombie_ratio` seuil 2¢** : la synthèse §6.2 définit `p.current_value < 0.02 * p.initial_value` (ratio relatif). Mais le prompt utilisateur dit "seuil 2¢" (2 cents absolus). **Résolution** : seuil relatif strict (`current/initial < 0.02`) — cohérent Gemini §1.1, plus robuste au sizing. Documenté §3.6.
- **`SCORING_V2_SHADOW_DAYS`** : la mémoire dit default `14`, le prompt utilisateur aussi. Même valeur → pas de conflit.

Aucune contradiction bloquante. On peut rédiger.

---

## 0. Résumé exécutif

- **Scope** : trois livrables couplés dans un seul bundle "scoring v2". (A) Nouveau sous-package `discovery/scoring/v2/` avec 6 facteurs pure + winsorisation + aggregator, exposé via extension `SCORING_VERSIONS_REGISTRY["v2"]`. (B) 6 gates durs pré-scoring, wallet rejeté loggé `gate_rejected` dans `trader_events`. (C) Shadow period 14 j : v1 et v2 calculent en parallèle, seule v1 pilote `DecisionEngine`, dashboard `/traders/scoring` rend v1|v2|delta_rank + Brier aggregate pool + Spearman rank.
- **Motivation** : synthèse §1.1 — la formule v1 est gameable (ROI farming, wash, zombie), ignorante des signaux informationnels (timing pré-event, spécialisation, calibration). Sans v2, copy-trading = Lindy mais sans edge démontré sur données labelées. Prérequis direct : M11 mergé (latence 2-3 s permet de valider les trades copiés sans confondre "scoring médiocre" et "bot trop lent").
- **Cible backtest** : Brier aggregate pool promu v2 **< 0.22 skill-level** (cible raisonnable), **< 0.15 expert-level** (cible stretch). Spearman rank(v1, v2) ∈ [0.4, 0.7] attendu (corrélé mais pas identique — sinon v2 n'apporte rien). Delta rank top-10 attendu 3-6 wallets remplacés.
- **Gain attendu par sous-feature** :
  - **Gates durs** : élimine ~30-50 % des wallets pré-scoring (zombie + wash + age < 30j + trade_count < 50). Économie compute + pool plus propre.
  - **Formule v2** : qualité sélection top-10 améliorée (à valider backtest). Anti-gaming structurel (Sortino pénalise drawdown, Brier pénalise over-confidence, HHI récompense spécialisation).
  - **Shadow period** : sécurité — un flip cutover ne peut casser capital sans 14 j d'observation + décision humaine.
- **Invariants de sécurité préservés** : M12 n'ouvre **aucune** nouvelle surface. Data sources = Data API publique + Gamma + Goldsky (déjà M5) + DB locale `trader_daily_pnl`. Aucune creds CLOB L1/L2. Aucun endpoint de POST. Aucun secret ni PII nouveau (les wallets publics restent les seules adresses loggées).
- **Hors scope strict** : pas de refactor formule v1 (intacte en fallback), pas d'auto-detection wash cluster (v1 = liste ENV, auto M17+), pas de weighting temporel exponentiel (uniform v1, half-life reportable v2.1), pas de `cold_start_mode` assoupli (gate strict v1, assoupli v2.1 si besoin), pas de rewrite rétroactif des scores historiques (versioning sacré), pas d'Apify Leaderboard integration (reportable M13), pas d'évolution du lifecycle `shadow→active→paused`.
- **Effort estimé** : ~2-3 semaines 1 dev (cohérent synthèse §7.3) + 14 j shadow period calendaire avant cutover.
- **Risque principal** : formule biaisée non détectée par le backtest (set labelé trop petit ou biaisé) → v2 sélectionne mal → cutover dégrade le pool. Mitigation = backtest obligatoire + seuil significatif Brier v2 < v1 - 0.01 + décision cutover **manuelle** (cf. §11.1).

---

## 1. Contexte

### 1.1 État M1..M11 (rappel)

M1..M11 ont livré :

- **M1-M3** : watcher → strategy → executor → storage. Polling Data API 5 s, pipeline FOK, kill switch M4.
- **M4 / M4.5 / M6** : `PnlSnapshotWriter` + dashboard FastAPI read-only.
- **M5 discovery** : scoring v1 4-métriques (`consistency + roi_norm + diversity + volume_norm`), gates = cold start uniquement (`resolved_positions_count < 10 → score=0`), lifecycle `shadow → active → paused → pinned`, cap `MAX_ACTIVE_TRADERS=10`, `BLACKLISTED_WALLETS`, audit `trader_events`.
- **M7 telegram enhanced** : templates MarkdownV2 + alertes promote/demote/cap.
- **M8 dry-run realistic** : simulation FOK via `/book`, ségrégation `realistic_fill` + `simulated`.
- **M9 silent CLI** + `RotatingFileHandler`.
- **M10 parity** : `EXECUTION_MODE: simulation|dry_run|live`, badge Telegram, `filter_noisy_endpoints`, `/logs` preset.
- **M11 real-time phase 1** : `ClobMarketWSClient` + cache Gamma adaptatif + 6 stages `trade_latency_samples` + dashboard `/latency`.

État scoring v1 (formule M5 actuelle, [discovery/scoring.py:29](../src/polycopy/discovery/scoring.py#L29)) :

```python
# Rappel v1 — intact après M12, reste accessible via SCORING_VERSION=v1.
score_v1 = 0.30 * consistency        # win_rate clipped [0, 1]
         + 0.30 * roi_norm            # (roi_clipped + 2) / 4, roi ∈ [-2, 2]
         + 0.20 * diversity           # 1 - HHI(volume par market)
         + 0.20 * volume_norm         # log10(volume/1000)/3 capped à 1
```

Gates v1 = cold start seul : `if resolved_positions_count < 10: return (0.0, low_confidence=True)`.

### 1.2 Pourquoi cette spec maintenant

Le brainstorming 2026-04-18 + les deux deep-searches + la synthèse §1-5-6 convergent sur trois faiblesses v1 structurelles :

1. **ROI nominal gameable** : un whale qui achète à 0.99 sur un marché presque résolu (edge ~1 %) affiche `roi_norm=0.51`, contribuant 0.15 points au score. Sur des wallets à 50+ trades, le cumul masque l'absence d'edge réel.
2. **Anti-gaming absent** : wash trading entre 2 wallets contrôlés produit `consistency=0.5 + roi≈0 → score≈0.4` (juste sous promotion 0.65, mais au-dessus de demote 0.40 → reste `shadow` perpétuel, gaspille budget API). Zombie positions (< 2 % initial value jamais liquidées) gonflent artificiellement le `consistency` (seulement `cash_pnl > 0` compté, positions à ~0 silencieusement ignorées).
3. **Signaux informationnels ignorés** : pas de mesure de calibration probabiliste (Brier), pas de détection timing pré-event (Mitts-Ofir), pas de pondération par spécialisation (HHI sur catégories Gamma — pas sur markets individuels). Les wallets vraiment "smart money" ne sont pas différenciés des bons spéculateurs.

M12 corrige les trois. M12 est **rapide** (2-3 semaines implémentation) et **additif-dominant** : un nouveau sous-package scoring/v2, 1 nouvelle table DB (`trader_daily_pnl`), 1 nouveau scheduler (`TraderDailyPnlWriter`), 1 nouvel onglet dashboard, 5 feature flags. **Zéro refactor sur scoring v1, decision_engine, watcher/strategy/executor, monitoring.**

### 1.3 Renumérotation synthèse → spec active

La synthèse §7.1 référençait "M13 = scoring v2". Puisque :

- M10 (parity + log hygiene) a shippé 2026-04-18.
- M11 (real-time pipeline phase 1) a shippé 2026-04-18.

→ Scoring v2 est bien **M12**. Les specs M13+ à venir (taker fees, phase 2 latence, Apify, etc.) décaleront en conséquence. Pas de changement fonctionnel.

### 1.4 Références externes

- **Synthèse §1.1-1.6** : formule finale + gates + versioning (référence unique).
- **Synthèse §5** : smart money patterns retenus (6 dans formule, 4 exclusion).
- **Synthèse §6.2** : zombie positions (seuil 2 %, gate < 0.40, inclus dans `discipline`).
- **Synthèse §6.7** : Mitts-Ofir pair-level → wallet-level via `sqrt(n_trades)` weighting.
- **Synthèse §7.2** : ordre M10 → M11 → **M12** (ce doc) → M13+.
- **Synthèse §9** : open questions tranchées par ce doc (pondération, horizon, weighting, cold start, backtest budget, Apify).
- **Gneiting-Raftery (Brier calibration)** : référence académique §1.2. Seuil skill < 0.22, expert < 0.15.
- **arxiv 2603.03136 (spécialisation catégorielle)** : HHI ≥ 0.70 sur 1-2 catégories = corrélat empirique de l'edge informationnel.
- **Mitts-Ofir 2026** : timing alpha pair-level pré-event.
- **Reichenbach-Walther** : 70 % des traders Polymarket perdent → gate `cash_pnl_90d > 0` supprime trivialement le bruit dominant.

---

## 2. Objectifs et non-objectifs

### 2.1 Objectifs

**A. Formule hybride `Score_v2`**

- Nouveau sous-package `src/polycopy/discovery/scoring/v2/` :
  - `__init__.py` exporte `compute_score_v2(metrics_v2, pool_context) -> ScoreV2Breakdown` (plus riche que v1 qui retourne juste un float).
  - `factors/risk_adjusted.py` : `compute_risk_adjusted(metrics) -> float` (0.6 Sortino + 0.4 Calmar, pre-normalisation).
  - `factors/calibration.py` : `compute_calibration(metrics, brier_baseline_pool) -> float` (Brier-skill).
  - `factors/timing_alpha.py` : `compute_timing_alpha(metrics) -> float` (mean weighted by `sqrt(n_trades_pair)`).
  - `factors/specialization.py` : `compute_specialization(metrics) -> float` (`1 - HHI(volume par catégorie Gamma)`).
  - `factors/consistency.py` : `compute_consistency(metrics) -> float` (fraction mois PnL>0 sur 90j).
  - `factors/discipline.py` : `compute_discipline(metrics) -> float` (`(1 - zombie_ratio) × sizing_stability`).
  - `normalization.py` : `winsorize_p5_p95(values) -> normalized` + `apply_pool_normalization(wallet_value, pool_values) -> float ∈ [0, 1]`. Pure functions.
  - `aggregator.py` : orchestration des 6 facteurs + pondération `0.25 + 0.20 + 0.20 + 0.15 + 0.10 + 0.10`.
  - `gates.py` : 6 gates purs + `check_all_gates(metrics) -> GateResult`.
- DTO `TraderMetricsV2` (frozen Pydantic) étend `TraderMetrics` avec :
  - `sortino_90d: float`, `calmar_90d: float`, `brier_90d: float` (raw brier score)
  - `timing_alpha_weighted: float` (∈ [0, 1])
  - `hhi_categories: float` (∈ [0, 1])
  - `monthly_pnl_positive_ratio: float` (fraction mois sur 3 mois avec PnL > 0)
  - `zombie_ratio: float`, `sizing_cv: float` (coefficient of variation des sizes)
  - `cash_pnl_90d: float`, `trade_count_90d: int`, `days_active: int`
  - `monthly_equity_curve: list[float]` (~90 points, 1 par jour)
- DTO `ScoreV2Breakdown` (frozen) : les 6 sous-scores bruts, 6 sous-scores normalisés 0-1, score final (et rappel du `brier_baseline_pool` utilisé), pour audit et drill-down dashboard.
- Registry `SCORING_VERSIONS_REGISTRY["v2"] = compute_score_v2_wrapper` — wrapper compatible signature v1 (accepte juste `metrics` + `settings` → retourne `(score, low_confidence)`). Le `pool_context` (pool values pour winsorisation + `brier_baseline_pool`) est injecté via un attribut de closure au moment de l'orchestration (cf. §3.7).
- Feature flag `SCORING_VERSION: Literal["v1", "v2"] = "v1"` (default).

**B. Gates durs pré-scoring**

- 6 gates purs dans `discovery/scoring/v2/gates.py` :

| Gate | Seuil | Fonction pure |
|---|---|---|
| `cash_pnl_90d > 0` | PnL cash positif | `check_cash_pnl(metrics) -> GateResult` |
| `trade_count_90d ≥ 50` | min 50 trades fermés ou actifs sur 90j | `check_trade_count(metrics) -> GateResult` |
| `days_active ≥ 30` | wallet actif depuis 30 j | `check_days_active(metrics) -> GateResult` |
| `zombie_ratio < 0.40` | moins de 40 % du capital immobilisé < 2 % initial | `check_zombie_ratio(metrics) -> GateResult` |
| `not blacklisted` | hors `BLACKLISTED_WALLETS` | `check_not_blacklisted(wallet, settings) -> GateResult` |
| `not in_wash_cluster` | hors `WASH_CLUSTER_WALLETS` (liste ENV v1) | `check_not_wash_cluster(wallet, settings) -> GateResult` |

- Aggregator `check_all_gates(metrics, settings) -> AggregateGateResult` : court-circuite au premier fail (fail-fast), retourne le nom du premier gate échoué + raison formatée.
- Wallet rejeté = **jamais scoré** + 1 row `trader_events` insérée avec :
  - `event_type="gate_rejected"`
  - `from_status=<current>` (shadow/active/paused/None)
  - `to_status=<current>` (pas de transition)
  - `scoring_version="v2"`
  - `reason=<gate_name>:<formatted_value>` (ex: `"zombie_ratio:0.52"`, `"trade_count_90d:23"`)
  - `event_metadata={"gate": name, "value": actual, "threshold": expected}`
- **Gates appliqués uniquement quand `scoring_version=v2`** (v1 conserve son seul gate cold start).

**C. Shadow period v1/v2 + dashboard + backtest**

- Pendant `SCORING_V2_SHADOW_DAYS` jours (default 14), les deux formules calculent :
  - v1 : pilote `DecisionEngine.decide(...)` → `promote/demote/keep` normal.
  - v2 : calcule `ScoreV2Breakdown`, écrit `TraderScore(scoring_version="v2")` append-only, **ne pilote pas** `DecisionEngine`.
- Condition de flip cutover manuel :
  1. `SCORING_V2_SHADOW_DAYS` écoulés depuis `SCORING_V2_SHADOW_STARTED_AT` (nouvelle colonne DB — non, préférer un timestamp par env var, cf. §5.4).
  2. Rapport backtest (`scripts/backtest_scoring_v2.py`) disponible et Brier v2 < Brier v1 - 0.01 (seuil de signification statistique).
  3. User flip manuellement `SCORING_VERSION=v2` dans `.env`.
- Onglet dashboard `/traders/scoring` : table wallet/v1/v2/delta_rank + métriques agrégées (Brier aggregate top-10 v1 vs top-10 v2, Spearman rank correlation). Bouton "Validate v2 & flip" visible uniquement après période shadow ET Brier v2 < v1 - 0.01.
- Script `scripts/backtest_scoring_v2.py` : lit `assets/scoring_v2_labels.csv` (50-100 wallets labelés), calcule v1 et v2 score sur fenêtre historique 6 mois, simule le pool promu, calcule Brier aggregate, Spearman, delta_rank. Output = rapport HTML + JSON.
- Fixture : `assets/scoring_v2_labels.csv` (commité, ~50-100 wallets publics) + fixtures positions/activity dans `tests/fixtures/scoring_v2/`.

**D. Table `trader_daily_pnl` + scheduler quotidien**

- Nouvelle table `trader_daily_pnl` (migration 0006 additive) :
  - `id` (pk autoincrement)
  - `wallet_address: String(42)` index
  - `date: Date` (pas `DateTime` — 1 snapshot par jour et par wallet)
  - `equity_usdc: Float` (= `sum(current_value)` des positions ouvertes + réalisé cumul)
  - `realized_pnl_day: Float` (delta réalisé vs jour précédent)
  - `unrealized_pnl_day: Float` (delta unrealized vs jour précédent)
  - `positions_count: int`
  - `snapshotted_at: DateTime(timezone=True)` default `now`
  - Index unique `(wallet_address, date)` — dédup journalier.
  - Index non-unique `date` (queries "tous wallets à date D").
- Peuplée par `TraderDailyPnlWriter` : co-lancé dans `DiscoveryOrchestrator.run_forever()` (TaskGroup), cadence = 1×/jour (sleep 24h). Scanne `target_traders WHERE status IN ('shadow', 'active', 'paused', 'pinned')` + fetch `/positions?user=<addr>` + `/value?user=<addr>` → insert 1 row par wallet par jour.
- Source de l'equity curve : lecture `trader_daily_pnl WHERE wallet=<addr> ORDER BY date` → liste d'equity → calcul Sortino/Calmar par `compute_risk_adjusted`.
- **Zéro migration sur tables M5/M11 existantes.**

**E. Feature flags**

- `SCORING_VERSION: Literal["v1", "v2"] = "v1"` (promu de `str` à literal pour safer).
- `SCORING_V2_SHADOW_DAYS: int [0, 90] = 14` (0 = pas de shadow, flip immédiat, documenté risqué).
- `SCORING_V2_WINDOW_DAYS: int [30, 365] = 90`.
- `SCORING_V2_COLD_START_MODE: bool = false` (relâche `trade_count_90d ≥ 20` au lieu de 50, warning log).
- `WASH_CLUSTER_WALLETS: list[str]` (CSV/JSON, même format `BLACKLISTED_WALLETS`).
- `SCORING_V2_BACKTEST_LABEL_FILE: Path = assets/scoring_v2_labels.csv`.
- `SCORING_V2_CUTOVER_READY: bool = false` (flag UI — n'enable le bouton dashboard "Validate v2 & flip" que si true ET backtest OK).

### 2.2 Non-objectifs

- Pas de rewrite v1 (code formule v1 intact, en fallback permanent).
- Pas d'auto-detection wash cluster (graph clustering on-chain, complexe — v1 M12 = liste ENV manuelle, auto M17+).
- Pas de weighting temporel exponentiel (uniform v1, half-life reportable v2.1).
- Pas de `cold_start_mode` relâché par défaut (gate strict v1 ; flag v1.1 si trop de candidats ratés).
- Pas de colonne nouvelle sur `target_traders`, `trader_scores`, `trader_events` (seule migration = table neuve `trader_daily_pnl`).
- Pas d'Apify integration (décision §6.3 synthèse — reportable M13 initial d'évaluation coût).
- Pas de suppression rétroactive des scores v1 historiques (versioning sacré — chaque `trader_scores` row porte son `scoring_version`).
- Pas de modif du lifecycle `shadow → active → paused → pinned` (M5 contract intact).
- Pas de modif `DecisionEngine` (la décision reste pilotée par v1 jusqu'au cutover manuel).
- Pas de parallélisation scoring (séquentiel par wallet, cohérent M5 [orchestrator.py:171](../src/polycopy/discovery/orchestrator.py#L171)).
- Pas de filtrage Goldsky additionnel (backend `data_api` par défaut reste).
- Pas de nouveau template Telegram (les gates rejected ne déclenchent PAS d'alerte — uniquement log + `trader_events`).
- Pas de rewrite `TraderMetrics` existant (extension via composition `TraderMetricsV2`).
- Pas de batch backtest sur production — script offline sur fixtures uniquement.

---

## 3. Design A : formule `Score_v2` par facteur

### 3.1 Formule finale retenue

Rappel synthèse §1.2 + mémoire `project_scoring_v2_decisions.md` :

```
Score_v2 = 0.25·risk_adjusted     # Sortino 0.6× + Calmar 0.4×
         + 0.20·calibration        # 1 - brier / brier_baseline_pool
         + 0.20·timing_alpha       # Mitts-Ofir pair→wallet weighted
         + 0.15·specialization     # 1 - HHI(volume par catégorie Gamma)
         + 0.10·consistency        # fraction mois PnL>0 sur 90j
         + 0.10·discipline         # (1 - zombie_ratio) × sizing_stability
```

**Contrat de la fonction agrégée** (`aggregator.py`) :

```python
def compute_score_v2(
    metrics: TraderMetricsV2,
    pool_context: PoolContext,
) -> ScoreV2Breakdown:
    """Pure function. metrics = valeurs brutes du wallet. pool_context = valeurs
    pool-wide utilisées pour winsorisation p5-p95 + brier_baseline_pool.

    Retourne le breakdown complet (6 sous-scores bruts + normalisés + score final)
    pour audit + dashboard drill-down.
    """
    raw = _RawSubscores(
        risk_adjusted=compute_risk_adjusted(metrics),
        calibration=compute_calibration(metrics, pool_context.brier_baseline_pool),
        timing_alpha=compute_timing_alpha(metrics),
        specialization=compute_specialization(metrics),
        consistency=compute_consistency(metrics),
        discipline=compute_discipline(metrics),
    )
    normalized = _NormalizedSubscores(
        risk_adjusted=apply_pool_normalization(raw.risk_adjusted, pool_context.risk_adjusted_pool),
        calibration=apply_pool_normalization(raw.calibration, pool_context.calibration_pool),
        timing_alpha=apply_pool_normalization(raw.timing_alpha, pool_context.timing_alpha_pool),
        specialization=apply_pool_normalization(raw.specialization, pool_context.specialization_pool),
        consistency=apply_pool_normalization(raw.consistency, pool_context.consistency_pool),
        discipline=apply_pool_normalization(raw.discipline, pool_context.discipline_pool),
    )
    final = (
        0.25 * normalized.risk_adjusted
        + 0.20 * normalized.calibration
        + 0.20 * normalized.timing_alpha
        + 0.15 * normalized.specialization
        + 0.10 * normalized.consistency
        + 0.10 * normalized.discipline
    )
    return ScoreV2Breakdown(
        wallet_address=metrics.wallet_address,
        score=max(0.0, min(1.0, final)),
        raw=raw,
        normalized=normalized,
        brier_baseline_pool=pool_context.brier_baseline_pool,
        scoring_version="v2",
    )
```

### 3.2 Facteur `risk_adjusted` (pondération 0.25)

**Sémantique** : combinaison Sortino (2/3) + Calmar (1/3) — synthèse §1.2. Sortino pénalise la volatilité **négative seulement** (cohérent distributions asymétriques binaires), Calmar pénalise le max drawdown observé.

**Source data** : `trader_daily_pnl` table — équity curve 90 j (~90 points). Reconstruction :

```python
# Pure function dans factors/risk_adjusted.py
def compute_risk_adjusted(metrics: TraderMetricsV2) -> float:
    """Valeur brute (non normalisée, peut être > 1 ou < 0)."""
    curve = metrics.monthly_equity_curve  # list[float] ~90 points
    if len(curve) < 14:
        return 0.0  # pas assez de data — pool_normalization donnera le p5 bas
    sortino = _sortino_ratio(curve, risk_free_rate=0.0)
    calmar = _calmar_ratio(curve)
    return 0.6 * sortino + 0.4 * calmar


def _sortino_ratio(curve: list[float], *, risk_free_rate: float) -> float:
    daily_returns = _daily_returns(curve)  # (e[i]/e[i-1]) - 1
    mean_ret = statistics.mean(daily_returns)
    downside_returns = [r for r in daily_returns if r < 0]
    if not downside_returns:
        return 3.0  # cap supérieur = tous gains, sentinel
    downside_dev = statistics.pstdev(downside_returns)
    if downside_dev == 0.0:
        return 3.0
    return (mean_ret - risk_free_rate) / downside_dev


def _calmar_ratio(curve: list[float]) -> float:
    daily_returns = _daily_returns(curve)
    annualized_ret = statistics.mean(daily_returns) * 365
    max_dd = _max_drawdown(curve)  # ∈ [0, 1]
    if max_dd < 1e-4:
        return 3.0  # aucun drawdown observé
    return annualized_ret / max_dd
```

**Normalisation post-facteur** : valeurs brutes peuvent être fortement bimodales (0 → 3). Winsorisation p5-p95 du pool actif + rescale [0, 1] géré par `apply_pool_normalization`.

**Dépendance DB** : table `trader_daily_pnl` doit être peuplée depuis au moins 14 j pour qu'un wallet ait un Sortino significatif. Si < 14 points → score = 0.0 pré-normalisation (pénalise cold start, cohérent avec gate `days_active ≥ 30`).

### 3.3 Facteur `calibration` (pondération 0.20)

**Sémantique** : Brier-skill score = `1 - brier_wallet / brier_baseline_pool`. Mesure calibration probabiliste — un wallet qui achète à prix 0.30 et gagne 40 % du temps a `brier = (1-0.30)² × 0.40 + (0-0.30)² × 0.60 = 0.196 + 0.054 = 0.25`.

**Baseline pool** = Brier d'un wallet qui achèterait toujours au midpoint du pool (simple reference sur l'ensemble des trades observés). Calculé une fois par cycle au niveau `PoolContext`.

**Source data** : positions résolues sur 90 j (avg_price, outcome résolu, size). Via `RawPosition` + metadata Gamma (résolution oui/non).

```python
def compute_calibration(
    metrics: TraderMetricsV2,
    brier_baseline_pool: float,
) -> float:
    """Brier-skill score. ∈ [-inf, 1]. Clippé [0, 1] post-normalisation."""
    if metrics.brier_90d is None or brier_baseline_pool <= 0:
        return 0.0
    return 1.0 - (metrics.brier_90d / brier_baseline_pool)
```

**Calcul `brier_90d`** dans `MetricsCollectorV2.collect()` :

```python
def _compute_brier(positions: list[RawPosition]) -> float | None:
    """Brier sur positions résolues. outcome ∈ {0, 1} (YES won / NO won)."""
    resolved = [p for p in positions if p.is_resolved]
    if not resolved:
        return None
    sq_errors = []
    for p in resolved:
        outcome = 1 if p.cash_pnl > 0 else 0  # approximation : YES gagnant
        pred_prob = p.avg_price  # prix payé ≈ probabilité perçue
        sq_errors.append((outcome - pred_prob) ** 2)
    return statistics.mean(sq_errors)
```

**Calcul `brier_baseline_pool`** (1×/cycle dans l'orchestrator) :

```python
# Dans DiscoveryOrchestrator._build_pool_context()
all_positions = []  # union positions résolues pool entier
for wallet in candidate_wallets:
    all_positions.extend(wallet_positions[wallet])
baseline = _compute_brier(all_positions) if all_positions else 0.25
```

`0.25` = fallback (Brier aléatoire = 0.25 sur binaire équilibré).

### 3.4 Facteur `timing_alpha` (pondération 0.20)

**Sémantique** : fraction du PnL généré par trades entrés **avant** un mouvement significatif. Synthèse §6.7 : pair-level → wallet-level via `sqrt(n_trades_pair)` weighting.

**Définition opérationnelle v1 M12** (simplifiée vs Mitts-Ofir original, documentée §13) :

- Un trade est "timing-alpha-positive" si :
  - Le prix du marché `mid_price` a bougé **de > 3 %** dans les **10 min qui ont suivi** le trade.
  - Et le trade était dans le bon sens (BUY YES si prix ↑, SELL YES si prix ↓).
- Source des prix historiques : **Data API `/activity` feed aggregate** sur le marché (pas de NLP, pas de feed news dédié). On reconstruit la courbe `mid_price(t)` approximative via les trades observés (VWAP fenêtre 1 min).
- Pair = `(wallet, conditionId)`. `timing_alpha_pair = fraction de trades positifs sur pair`.
- Wallet-level : `mean(pair_score * sqrt(n_trades_pair)) / mean(sqrt(n_trades_pair))` — weighted mean qui balance qualité vs quantité.

```python
def compute_timing_alpha(metrics: TraderMetricsV2) -> float:
    """Valeur brute ∈ [0, 1] pré-normalisation.

    metrics.timing_alpha_weighted est déjà la weighted mean pair-level calculée
    par MetricsCollectorV2. Cette fonction sert de validation/clipping.
    """
    return max(0.0, min(1.0, metrics.timing_alpha_weighted))
```

**Coût compute estimé** : pour 100 wallets × ~10 pairs distinctes × ~50 trades → 50 000 queries feed `/activity` avec filtre. Trop lourd en sync — `MetricsCollectorV2` cache les courbes `mid_price(t)` par `conditionId` (LRU 200 marchés, TTL 10 min) pour amortir.

**Fallback** : si Data API retourne insuffisamment de données historiques sur un marché (< 50 trades sur la fenêtre), `timing_alpha_pair = 0.5` (neutre) pour ce pair. Pénalisation implicite via pool normalization (les wallets avec peu de pair data auront des scores proches du p50).

### 3.5 Facteur `specialization` (pondération 0.15)

**Sémantique** : `1 - HHI(volume par catégorie Gamma)`. Concentre sur les wallets avec volume ≥ 70 % sur 1-2 catégories — corrélat empirique edge informationnel (arxiv 2603.03136).

**Différence vs v1** : v1 utilise HHI sur **markets individuels** ([metrics_collector.py:87](../src/polycopy/discovery/metrics_collector.py#L87)). v2 utilise HHI sur **catégories Gamma** (Politics, Sports, Crypto, Economics, Pop Culture, etc.).

**Source data** : Gamma `/markets?condition_ids=...` retourne un champ `tags` ou `category` — à confirmer par fixture (cf. §8 étape 4). Si le champ n'existe pas directement, on dérive la catégorie via le titre (heuristique regex simple — reportable si trop fragile).

```python
def compute_specialization(metrics: TraderMetricsV2) -> float:
    """Valeur brute ∈ [0, 1] pré-normalisation."""
    return 1.0 - metrics.hhi_categories


def _compute_hhi_categories(
    activity_trades: list[dict],
    market_to_category: dict[str, str],
) -> float:
    """HHI sur volume par catégorie. 1.0 = concentration max (1 catégorie)."""
    volume_per_cat: dict[str, float] = defaultdict(float)
    for t in activity_trades:
        cid = t.get("conditionId")
        cat = market_to_category.get(cid, "unknown")
        volume_per_cat[cat] += float(t.get("size", 0)) * float(t.get("price", 0))
    total = sum(volume_per_cat.values())
    if total == 0:
        return 1.0
    return sum((v / total) ** 2 for v in volume_per_cat.values())
```

### 3.6 Facteur `consistency` (pondération 0.10)

**Sémantique** : `fraction de mois avec PnL > 0 sur 3 mois glissants`. Filtre les one-shots. Poids faible car partiellement corrélé à `risk_adjusted`.

**Source data** : `trader_daily_pnl` table, agrégation par mois.

```python
def compute_consistency(metrics: TraderMetricsV2) -> float:
    """Valeur brute ∈ [0, 1] pré-normalisation."""
    return metrics.monthly_pnl_positive_ratio
```

Calcul dans `MetricsCollectorV2` :

```python
def _compute_monthly_ratio(curve: list[tuple[date, float]]) -> float:
    """Fraction de mois avec PnL > 0. Fenêtre 3 mois = 3 mois calendaires.

    Si < 3 mois de données → retourne ratio partiel (ex: 2 mois observés, 1 positif = 0.5).
    """
    by_month = defaultdict(list)
    for d, equity in curve:
        by_month[(d.year, d.month)].append(equity)
    if not by_month:
        return 0.0
    positive_months = 0
    for month_points in by_month.values():
        delta = month_points[-1] - month_points[0]
        if delta > 0:
            positive_months += 1
    return positive_months / len(by_month)
```

### 3.7 Facteur `discipline` (pondération 0.10)

**Sémantique** : `(1 - zombie_ratio) × sizing_stability`. Zombie = Gemini §1.1, sizing stability = inverse du coefficient of variation des tailles.

```python
def compute_discipline(metrics: TraderMetricsV2) -> float:
    """Valeur brute ∈ [0, 1] pré-normalisation."""
    anti_zombie = max(0.0, 1.0 - metrics.zombie_ratio)
    sizing_stability = max(0.0, min(1.0, 1.0 - metrics.sizing_cv))
    return anti_zombie * sizing_stability
```

**Définition `zombie_ratio`** (clarification vs prompt utilisateur) :

```python
def _compute_zombie_ratio(positions: list[RawPosition]) -> float:
    """Gemini §1.1 : positions current_value < 2% × initial_value, jamais liquidées.

    Window = 90 j glissants (aligné avec SCORING_V2_WINDOW_DAYS).
    Excluded du dénominateur : positions ouvertes depuis < 30 j (pas pénaliser les récentes).
    """
    now = datetime.now(tz=UTC)
    eligible = [
        p for p in positions
        if (now - p.opened_at).days >= 30  # exclure les jeunes
    ]
    if not eligible:
        return 0.0
    zombies = [
        p for p in eligible
        if p.current_value < 0.02 * p.initial_value
        and not p.is_resolved  # jamais liquidée
    ]
    capital_zombie = sum(p.initial_value for p in zombies)
    capital_total = sum(p.initial_value for p in eligible)
    if capital_total == 0:
        return 0.0
    return capital_zombie / capital_total
```

**Définition `sizing_cv`** :

```python
def _compute_sizing_cv(activity_trades: list[dict]) -> float:
    """Coefficient of variation des sizes. ∈ [0, +inf). Clippé [0, 1] post-calcul."""
    sizes = [float(t.get("size", 0)) * float(t.get("price", 0)) for t in activity_trades]
    sizes = [s for s in sizes if s > 0]
    if len(sizes) < 2:
        return 1.0  # pas assez de data, cas défavorable (sizing_stability = 0)
    mean = statistics.mean(sizes)
    std = statistics.pstdev(sizes)
    if mean == 0:
        return 1.0
    return min(1.0, std / mean)
```

### 3.8 Winsorisation p5-p95 + normalisation pool-wide

Module `normalization.py`, pure functions :

```python
def winsorize_p5_p95(values: list[float]) -> tuple[float, float]:
    """Retourne (p5, p95) pour clipping pool-wide. Pure, déterministe."""
    if not values:
        return (0.0, 1.0)
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    idx_p5 = int(0.05 * n)
    idx_p95 = min(n - 1, int(0.95 * n))
    return (sorted_vals[idx_p5], sorted_vals[idx_p95])


def apply_pool_normalization(wallet_value: float, pool_values: list[float]) -> float:
    """Clippe wallet_value à (p5, p95) du pool puis rescale [0, 1].

    Idempotent : 2 appels consécutifs sur la même valeur donnent le même résultat
    (la 1ère application la met déjà dans [p5, p95]).
    """
    if not pool_values:
        return max(0.0, min(1.0, wallet_value))
    p5, p95 = winsorize_p5_p95(pool_values)
    if p95 <= p5:
        return 0.5  # pool dégénéré : tous les wallets ont la même valeur
    clipped = max(p5, min(p95, wallet_value))
    return (clipped - p5) / (p95 - p5)
```

**Test idempotence critique** (§9.3.A) : `apply_pool_normalization(apply_pool_normalization(x, pool), pool) == apply_pool_normalization(x, pool)`.

### 3.9 Registry entry v2 + wrapper signature v1

Pour conserver l'API `SCORING_VERSIONS_REGISTRY: dict[str, Callable[[TraderMetrics], float]]`, on wrappe v2 :

```python
# src/polycopy/discovery/scoring.py (modif minimale M12)

from polycopy.discovery.scoring.v2 import compute_score_v2, _CURRENT_POOL_CONTEXT


def _compute_score_v2_wrapper(metrics: TraderMetrics) -> float:
    """Wrapper compatible signature v1.

    Le pool_context est injecté via un contextvar global posé par l'orchestrator
    avant chaque cycle (cf. §5.2). v2 attend un TraderMetricsV2 — upcast ici si
    metrics arrive en TraderMetrics "v1" (cas legacy tests). Si TraderMetricsV2
    absent, upcast avec valeurs par défaut (retourne 0.0 — signal explicite).
    """
    pool_ctx = _CURRENT_POOL_CONTEXT.get()
    if pool_ctx is None:
        # appel hors orchestrator (test unitaire) — retourne 0 + warn
        return 0.0
    if not isinstance(metrics, TraderMetricsV2):
        return 0.0  # besoin v2 metrics
    breakdown = compute_score_v2(metrics, pool_ctx)
    return breakdown.score


SCORING_VERSIONS_REGISTRY: dict[str, Callable[[TraderMetrics], float]] = {
    "v1": _compute_score_v1,
    "v2": _compute_score_v2_wrapper,
}
```

Le contextvar `_CURRENT_POOL_CONTEXT: ContextVar[PoolContext | None]` est posé par `DiscoveryOrchestrator._build_pool_context(...)` **avant** la boucle scoring et remis à None après — garantit que `compute_score(metrics, settings=...)` dans le registry reste une simple lookup.

---

## 4. Design B : gates durs + audit trail

### 4.1 Inventaire complet des 6 gates

| # | Gate | Fonction pure | Seuil (configurable ?) | Source data |
|---|---|---|---|---|
| 1 | `cash_pnl_positive` | `check_cash_pnl(metrics) -> GateResult` | `> 0` (fixe) | `trader_daily_pnl.sum(realized_pnl_day) over 90d` |
| 2 | `trade_count_min` | `check_trade_count(metrics) -> GateResult` | `≥ 50` (ou 20 si cold_start_mode) | `/activity?type=TRADE&limit=500 count` |
| 3 | `days_active_min` | `check_days_active(metrics) -> GateResult` | `≥ 30` (fixe) | `first_trade.timestamp` dans `/activity` |
| 4 | `zombie_ratio_max` | `check_zombie_ratio(metrics) -> GateResult` | `< 0.40` (fixe) | `/positions`, calcul §3.7 |
| 5 | `not_blacklisted` | `check_not_blacklisted(wallet, settings)` | `BLACKLISTED_WALLETS` ENV | env var |
| 6 | `not_wash_cluster` | `check_not_wash_cluster(wallet, settings)` | `WASH_CLUSTER_WALLETS` ENV | env var |

`GateResult` (Pydantic frozen) :

```python
class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    gate_name: str
    passed: bool
    observed_value: float | int | str
    threshold: float | int | str
    reason: str  # ex: "zombie_ratio:0.52 >= threshold:0.40"
```

### 4.2 Aggregator `check_all_gates`

```python
# src/polycopy/discovery/scoring/v2/gates.py

def check_all_gates(
    metrics: TraderMetricsV2,
    wallet: str,
    settings: Settings,
) -> AggregateGateResult:
    """Fail-fast : court-circuite au premier gate rejeté.

    Ordre des gates choisi pour minimiser le coût moyen d'un fail :
    1. not_blacklisted (env lookup, O(1))
    2. not_wash_cluster (env lookup, O(1))
    3. days_active_min (DTO lookup, O(1))
    4. trade_count_min (DTO lookup, O(1))
    5. cash_pnl_positive (DTO lookup, O(1))
    6. zombie_ratio_max (DTO lookup, O(1))
    """
    for check in [
        lambda: check_not_blacklisted(wallet, settings),
        lambda: check_not_wash_cluster(wallet, settings),
        lambda: check_days_active(metrics),
        lambda: check_trade_count(metrics, cold_start_mode=settings.scoring_v2_cold_start_mode),
        lambda: check_cash_pnl(metrics),
        lambda: check_zombie_ratio(metrics),
    ]:
        result = check()
        if not result.passed:
            return AggregateGateResult(
                passed=False,
                failed_gate=result,
                all_results=None,  # court-circuit : on ne continue pas
            )
    return AggregateGateResult(passed=True, failed_gate=None, all_results=None)
```

### 4.3 Persistance `trader_events.gate_rejected`

Le caller (orchestrator) :

```python
# DiscoveryOrchestrator._run_one_cycle() M12 extension (pseudocode)

gates = check_all_gates(metrics_v2, wallet, self._settings)
if not gates.passed:
    await event_repo.insert(TraderEventDTO(
        wallet_address=wallet,
        event_type="gate_rejected",
        from_status=_current_status(current),
        to_status=_current_status(current),  # no transition
        score_at_event=None,  # jamais scoré
        scoring_version="v2",
        reason=gates.failed_gate.reason,
        event_metadata={
            "gate": gates.failed_gate.gate_name,
            "value": gates.failed_gate.observed_value,
            "threshold": gates.failed_gate.threshold,
        },
    ))
    log.info("trader_gate_rejected",
             wallet=wallet,
             gate=gates.failed_gate.gate_name,
             value=gates.failed_gate.observed_value,
             threshold=gates.failed_gate.threshold)
    continue  # skip scoring, skip decision_engine
```

**Important** : pas d'alerte Telegram sur `gate_rejected` (trop bavard, `trader_events` suffit pour l'audit). Reportable M12.1 si user demande.

### 4.4 Application conditionnelle scoring_version

Les gates s'appliquent **uniquement si `scoring_version=v2`**. v1 garde son comportement M5 (cold start seul). Logique dans orchestrator :

```python
if settings.scoring_version == "v2":
    gates = check_all_gates(metrics_v2, wallet, settings)
    if not gates.passed:
        await event_repo.insert(...)
        continue
# sinon (v1) : pas de gate check, cold start seul dans compute_score()
```

Pendant la shadow period (v1 actif, v2 parallèle), **les deux comportements coexistent** : v1 score normalement (cold start seul), v2 check gates + score si gates OK. Les wallets rejetés par gates v2 reçoivent un `trader_events.gate_rejected` mais **restent** pilotés par v1 pour leur lifecycle.

---

## 5. Design C : shadow period v1/v2 + backtest + dashboard

### 5.1 Mode dual-compute (shadow active)

Pendant la shadow period, l'orchestrator calcule les deux scores en parallèle :

```python
# src/polycopy/discovery/orchestrator.py M12 extension (pseudocode)

async def _run_one_cycle(self, ...):
    cycle_at = datetime.now(tz=UTC)
    # ... candidat pool, exclusions ...

    # M12 : pre-build pool_context SI v2 enabled OU shadow mode actif
    shadow_active = self._is_v2_shadow_active()
    if settings.scoring_version == "v2" or shadow_active:
        pool_context = await self._build_pool_context_v2(candidate_wallets)
        _CURRENT_POOL_CONTEXT.set(pool_context)
    else:
        pool_context = None

    for wallet in to_score:
        metrics_v1 = await metrics_collector.collect(wallet)
        metrics_v2 = await metrics_collector_v2.collect(wallet) if pool_context else None

        # -- v2 path (gates + score, écrit en parallèle si shadow)
        if metrics_v2 is not None:
            gates = check_all_gates(metrics_v2, wallet, settings)
            if not gates.passed:
                await event_repo.insert(_gate_rejected_event(wallet, gates, current))
                # NE PAS skip le loop si on est en shadow v1 — v1 doit continuer à piloter
                if settings.scoring_version == "v2":
                    continue
                score_v2_value = None
            else:
                breakdown_v2 = compute_score_v2(metrics_v2, pool_context)
                score_v2_value = breakdown_v2.score
                # Persiste trader_scores row v2 si :
                # - v2 est la version pilote OR
                # - shadow period active (double-write)
                await score_repo.insert(TraderScoreDTO(
                    target_trader_id=current.id if current else ...,
                    wallet_address=wallet,
                    score=score_v2_value,
                    scoring_version="v2",
                    low_confidence=False,
                    metrics_snapshot={
                        "metrics_v2": metrics_v2.model_dump(mode="json"),
                        "breakdown": breakdown_v2.model_dump(mode="json"),
                    },
                ))
        else:
            score_v2_value = None

        # -- v1 path (intact, pilote)
        score_v1_value, low_conf = compute_score_v1(metrics_v1, settings=self._settings)
        if current is not None:
            await score_repo.insert(TraderScoreDTO(
                ..., score=score_v1_value, scoring_version="v1", ...
            ))

        # -- decision engine : SEULEMENT v1 (shadow) ou SEULEMENT v2 (post-cutover)
        pilot_score = score_v1_value if settings.scoring_version == "v1" else score_v2_value
        if pilot_score is None:
            continue  # gate rejected in v2 mode, pas de decision
        scoring_for_decide = ScoringResult(
            wallet_address=wallet,
            score=pilot_score,
            scoring_version=settings.scoring_version,
            low_confidence=(low_conf if settings.scoring_version == "v1" else False),
            metrics=metrics_v1,
            cycle_at=cycle_at,
        )
        decision = await decision_engine.decide(scoring_for_decide, current, active_count=active_count)
        ...
```

**Invariant critique** : `DecisionEngine.decide` est appelé **avec le score de la version pilote** (v1 par défaut). v2 n'influence **jamais** la décision tant que `SCORING_VERSION=v1`.

### 5.2 `PoolContext` et contextvar

```python
# src/polycopy/discovery/scoring/v2/__init__.py

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

class PoolContext(BaseModel):
    """Snapshot des valeurs pool-wide pour winsorisation + Brier baseline.

    Rebuilt par DiscoveryOrchestrator en début de chaque cycle.
    """
    model_config = ConfigDict(frozen=True)
    risk_adjusted_pool: list[float]
    calibration_pool: list[float]  # Brier skill raw
    timing_alpha_pool: list[float]
    specialization_pool: list[float]
    consistency_pool: list[float]
    discipline_pool: list[float]
    brier_baseline_pool: float


_CURRENT_POOL_CONTEXT: ContextVar[PoolContext | None] = ContextVar(
    "scoring_v2_pool_context", default=None,
)
```

### 5.3 Script `scripts/backtest_scoring_v2.py`

Nouveau script standalone hors `polycopy` package (comme `scripts/score_backtest.py` M5). CLI :

```bash
python scripts/backtest_scoring_v2.py \
  --labels-file assets/scoring_v2_labels.csv \
  --as-of 2026-02-15 \
  --window-days 180 \
  --output backtest_v2_report.html
```

**Input** `assets/scoring_v2_labels.csv` (commité, adresses publiques) :

```csv
wallet_address,label,notes
0xabc...,smart_money,"identified via /holders top markets 2025-Q4"
0xdef...,random,"control group, low-volume 2024 activity"
...
```

50-100 wallets, ratio ~50/50 entre `smart_money` et `random`.

**Sortie** : rapport HTML + JSON avec :

- Pour chaque wallet : `score_v1_at_T`, `score_v2_at_T`, `rank_v1`, `rank_v2`, `delta_rank`.
- Agrégés :
  - `brier_top10_v1` : Brier du pool promu (top-10 score v1) sur fenêtre `[T, T+30j]`.
  - `brier_top10_v2` : idem v2.
  - `spearman_rank(v1, v2)` : corrélation de rang.
  - Cible acceptance : `brier_top10_v2 < brier_top10_v1 - 0.01` (signification stat).
- Graph : scatter v1 vs v2, par label (smart_money = verts, random = rouges).

**Le script doit pouvoir tourner offline** sur des fixtures historiques capturées une fois. Fixtures :

- `tests/fixtures/scoring_v2/positions_<wallet>.json` (1 par wallet labelé)
- `tests/fixtures/scoring_v2/activity_<wallet>.json`
- `tests/fixtures/scoring_v2/trader_daily_pnl_<wallet>.json` (equity curve synthétique si pas de snapshot prod)
- `tests/fixtures/scoring_v2/gamma_categories.json` (mapping marché → catégorie)

### 5.4 Cutover manuel

**Séquence explicite** :

1. T0 = merge M12 sur `main`. `SCORING_VERSION=v1` default, `SCORING_V2_SHADOW_DAYS=14`.
2. T0 + 14 j = fin shadow period. Dashboard `/traders/scoring` affiche métriques agrégées + bouton "Validate v2 & flip" (désactivé si backtest absent ou Brier v2 ≥ v1 - 0.01).
3. User lance `scripts/backtest_scoring_v2.py`, inspecte rapport.
4. Si rapport OK → user set `SCORING_V2_CUTOVER_READY=true` (active le bouton dashboard) + set `SCORING_VERSION=v2` dans `.env` + redémarre le bot.
5. v2 devient pilote. v1 continue d'être calculée en parallèle pendant 14 j supplémentaires (observation inverse — détecter régression).
6. T0 + 28 j : si pas de régression, user set `SCORING_V2_SHADOW_DAYS=0` → v1 cesse d'être calculée, scoring devient v2 seul.

**Aucun auto-flip** : décision 100 % humaine, conditionnée à data empirique. Cohérent avec philosophie M5 "l'utilisateur consent explicitement à ce que le bot choisisse ses cibles".

### 5.5 Dashboard `/traders/scoring`

Nouvelle route (sous-onglet `/traders`). Design minimal :

- **Header** : titre `Scoring comparison — v1 | v2 | delta_rank`. Badge "Shadow period: 9/14 days" ou "Cutover ready" selon état.
- **Table** (HTMX polling 30 s) : colonnes `Wallet | Label | Status | Score v1 | Score v2 | Δ rank | Last scored`. Filtrable `?status=active`, sort `score_v2 DESC` default.
- **Section "Aggregate metrics"** (calculée côté serveur, cache 1 min) :
  - `Brier aggregate top-10 v1 = 0.245`
  - `Brier aggregate top-10 v2 = 0.198`
  - `Spearman rank(v1, v2) = 0.67`
  - `Delta top-10 = 4 wallets remplacés`
- **Section "Cutover status"** :
  - Days since shadow start: X / Y.
  - Backtest report last generated: date + lien.
  - Bouton "Validate v2 & flip" : désactivé si (days_since_shadow < Y) OU (brier_v2 >= brier_v1 - 0.01) OU (`SCORING_V2_CUTOVER_READY=false`).
  - Le bouton **n'exécute pas** le flip — il affiche simplement la commande .env à appliquer manuellement. Pas de write DB côté dashboard (respect invariant M4.5/M6 read-only).

Template : `src/polycopy/dashboard/templates/traders_scoring.html` (nouveau, Tailwind CDN cohérent M6).

### 5.6 Nouveau scheduler `TraderDailyPnlWriter`

Pattern cohérent `PnlSnapshotWriter` M4 + `LatencyPurgeScheduler` M11 — une coroutine co-lancée dans `DiscoveryOrchestrator` TaskGroup (ou nouveau top-level — trancher §13).

**Décision** : co-lancé dans `DiscoveryOrchestrator.run_forever()` via `async with asyncio.TaskGroup()` interne. Rationale : couplage naturel avec la boucle discovery (mêmes wallets, même period data), évite d'ajouter un top-level à `__main__.py`.

```python
# src/polycopy/discovery/trader_daily_pnl_writer.py (nouveau)

class TraderDailyPnlWriter:
    """Snapshot quotidien equity curve par wallet.

    Co-lancé dans DiscoveryOrchestrator TaskGroup. Cadence 24h (configurable
    TRADER_DAILY_PNL_INTERVAL_SECONDS). Scanne target_traders WHERE
    status IN (shadow, active, paused, pinned) + fetch /positions + /value
    → insert 1 row par wallet par date UTC.
    """

    def __init__(
        self,
        data_api: DiscoveryDataApiClient,
        target_repo: TargetTraderRepository,
        daily_pnl_repo: TraderDailyPnlRepository,
        settings: Settings,
    ) -> None: ...

    async def run_forever(self, stop_event: asyncio.Event) -> None: ...

    async def _snapshot_all(self) -> int:
        """Écrit 1 row par wallet scanné. Retourne count inséré.

        Dédup idempotent : (wallet_address, date) unique → INSERT OR IGNORE SQLite.
        Si le scheduler redémarre 2× dans la même journée, pas de duplicate.
        """
```

**Coût API** : 1 fetch `/positions` + 1 fetch `/value` par wallet (2 calls). À 50 wallets total = 100 calls/jour. Négligeable vs le budget Data API.

**Rétention** : pas de purge v1 — gardée perpétuellement (~1.5 KB / wallet / an). À 100 wallets × 5 ans = 750 KB. Pas de problème. Reportable à M14+ si taille DB devient préoccupante (table compaction v3, §14).

---

## 6. Configuration — env vars

### 6.1 Ajoutées

| Env var | Champ Settings | Type | Default | Description |
|---|---|---|---|---|
| `SCORING_VERSION` | `scoring_version` | `Literal["v1", "v2"]` | `"v1"` | **Promu de `str` à literal** (breaking-free : v1 stays default). Pilote `DecisionEngine`. |
| `SCORING_V2_SHADOW_DAYS` | `scoring_v2_shadow_days` | `int [0, 90]` | `14` | Durée coexistence v1/v2. Pendant la fenêtre, v2 calcule mais ne pilote pas. `0` = pas de shadow (pas de calcul parallèle). |
| `SCORING_V2_WINDOW_DAYS` | `scoring_v2_window_days` | `int [30, 365]` | `90` | Fenêtre temporelle métrique v2 (Sortino/Calmar/Brier/timing). |
| `SCORING_V2_COLD_START_MODE` | `scoring_v2_cold_start_mode` | `bool` | `false` | Relâche `trade_count_90d ≥ 20` au lieu de 50. Log WARNING au boot si true. |
| `WASH_CLUSTER_WALLETS` | `wash_cluster_wallets` | `list[str]` (CSV/JSON) | `[]` | Wallets exclus par gate 6. Même format `BLACKLISTED_WALLETS`. |
| `SCORING_V2_BACKTEST_LABEL_FILE` | `scoring_v2_backtest_label_file` | `Path` | `"assets/scoring_v2_labels.csv"` | Chemin fichier labels backtest. |
| `SCORING_V2_CUTOVER_READY` | `scoring_v2_cutover_ready` | `bool` | `false` | Active le bouton dashboard "Validate v2 & flip". User-controlled. |
| `TRADER_DAILY_PNL_ENABLED` | `trader_daily_pnl_enabled` | `bool` | `true` | Active le scheduler `TraderDailyPnlWriter`. Si `false`, pas d'equity curve → v2 renvoie Sortino/Calmar partiels. |
| `TRADER_DAILY_PNL_INTERVAL_SECONDS` | `trader_daily_pnl_interval_seconds` | `int [3600, 604800]` | `86400` (24h) | Cadence du snapshot. |

### 6.2 Modifiées

- `scoring_version: str` (M5 [config.py:456](../src/polycopy/config.py#L456)) → `Literal["v1", "v2"]`. **Backward-compat** : valeurs "v1"/"v2" acceptées, autres valeurs → `ValueError` au boot (meilleure sécurité). Tests M5 existants passent tels quels (tous utilisent `"v1"`).

### 6.3 Validators Pydantic

- `scoring_v2_shadow_days` : `Field(ge=0, le=90)`.
- `scoring_v2_window_days` : `Field(ge=30, le=365)`.
- `wash_cluster_wallets` : `Annotated[list[str], NoDecode]` + validator CSV/JSON (copie `_parse_blacklisted_wallets` M5 [config.py:534](../src/polycopy/config.py#L534)).
- Cross-field validator : si `scoring_version=v2` ET `trader_daily_pnl_enabled=false` → log WARNING mais ne crash pas (v2 tourne en mode dégradé, Sortino = 0 sans curve).
- Cross-field validator : si `scoring_v2_cold_start_mode=true` → log WARNING "cold start mode relâché, gate trade_count ≥ 20 au lieu de 50".

### 6.4 `.env.example` — mise à jour

Nouveau bloc (après section Discovery M5, avant M8) :

```dotenv
# --- Scoring v2 (M12, opt-in par défaut via SCORING_VERSION=v1) ---
# v2 : formule hybride (Sortino/Calmar + Brier + Mitts-Ofir + HHI catégories
# + consistency + discipline) + 6 gates durs pré-scoring. Coexistence v1/v2
# pendant la shadow period, cutover manuel après backtest OK.
# SCORING_VERSION=v1                    # "v1" (default M5) ou "v2"
# SCORING_V2_SHADOW_DAYS=14             # calcul parallèle v1+v2 pendant N jours
# SCORING_V2_WINDOW_DAYS=90             # fenêtre temporelle facteurs v2
# SCORING_V2_COLD_START_MODE=false      # relâche gate trade_count_90d ≥ 20
# WASH_CLUSTER_WALLETS=                 # CSV/JSON exclusion wash (v1 manuel)
# SCORING_V2_BACKTEST_LABEL_FILE=assets/scoring_v2_labels.csv
# SCORING_V2_CUTOVER_READY=false        # active bouton dashboard "flip v2"

# --- Equity curve quotidienne (TraderDailyPnl — prérequis v2) ---
# TRADER_DAILY_PNL_ENABLED=true
# TRADER_DAILY_PNL_INTERVAL_SECONDS=86400
```

---

## 7. Changements module par module (file:line)

### 7.1 `src/polycopy/config.py`

- **Ligne 456** : `scoring_version: str = Field("v1", ...)` → `scoring_version: Literal["v1", "v2"] = Field("v1", ...)`.
- **Après ligne 533** (après `goldsky_pnl_subgraph_url`) : insérer nouveau bloc `# --- Scoring v2 (M12) ---` avec 7 champs nouveaux + validator parsing `wash_cluster_wallets` (dup `_parse_blacklisted_wallets`).
- **Nouveau validator cross-field** `@model_validator(mode="after")` : log WARNING si v2 sans daily_pnl.

### 7.2 `src/polycopy/discovery/scoring/__init__.py` NOUVEAU (module ouvert)

Transforme `scoring.py` fichier en package :

```
src/polycopy/discovery/scoring/
├── __init__.py          # ré-exporte SCORING_VERSIONS_REGISTRY + compute_score (backward-compat M5)
├── v1.py                # ex-scoring.py déplacé — NE PAS modifier
└── v2/
    ├── __init__.py      # exporte compute_score_v2, PoolContext, _CURRENT_POOL_CONTEXT
    ├── factors/
    │   ├── __init__.py
    │   ├── risk_adjusted.py
    │   ├── calibration.py
    │   ├── timing_alpha.py
    │   ├── specialization.py
    │   ├── consistency.py
    │   └── discipline.py
    ├── normalization.py
    ├── gates.py
    ├── aggregator.py
    └── dtos.py          # TraderMetricsV2, ScoreV2Breakdown, PoolContext, GateResult, AggregateGateResult
```

**Préservation M5** : l'import `from polycopy.discovery.scoring import compute_score` continue à fonctionner. Le registry ajoute juste `"v2"` en parallèle.

### 7.3 `src/polycopy/discovery/scoring/v1.py` (ex-scoring.py)

Renommé via `git mv src/polycopy/discovery/scoring.py → src/polycopy/discovery/scoring/v1.py`. **Zéro ligne modifiée.**

### 7.4 `src/polycopy/discovery/scoring/v2/dtos.py` NOUVEAU

```python
"""DTOs Pydantic v2 M12."""
class TraderMetricsV2(TraderMetrics):  # hérite ou compose
    """Étend TraderMetrics M5 avec métriques v2.

    Composition préférée à l'héritage si incompatibilité Pydantic frozen.
    """
    model_config = ConfigDict(frozen=True)
    sortino_90d: float = 0.0
    calmar_90d: float = 0.0
    brier_90d: float | None = None
    timing_alpha_weighted: float = 0.0
    hhi_categories: float = 1.0
    monthly_pnl_positive_ratio: float = 0.0
    zombie_ratio: float = 0.0
    sizing_cv: float = 1.0
    cash_pnl_90d: float = 0.0
    trade_count_90d: int = 0
    days_active: int = 0
    monthly_equity_curve: list[float] = Field(default_factory=list)


class ScoreV2Breakdown(BaseModel):
    model_config = ConfigDict(frozen=True)
    wallet_address: str
    score: float
    raw: _RawSubscores
    normalized: _NormalizedSubscores
    brier_baseline_pool: float
    scoring_version: str = "v2"


class PoolContext(BaseModel): ...  # cf. §5.2
class GateResult(BaseModel): ...   # cf. §4.1
class AggregateGateResult(BaseModel): ...  # cf. §4.2
```

### 7.5 `src/polycopy/discovery/scoring/v2/factors/*.py` NOUVEAU

6 fichiers, ~30-80 lignes chacun. Contenu pseudocodé §3.2-3.7.

### 7.6 `src/polycopy/discovery/scoring/v2/normalization.py` NOUVEAU

~40 lignes. Contenu complet §3.8 (`winsorize_p5_p95`, `apply_pool_normalization`).

### 7.7 `src/polycopy/discovery/scoring/v2/gates.py` NOUVEAU

~150 lignes. 6 fonctions pures `check_*` + `check_all_gates` fail-fast.

### 7.8 `src/polycopy/discovery/scoring/v2/aggregator.py` NOUVEAU

~80 lignes. Fonction `compute_score_v2(metrics, pool_context) -> ScoreV2Breakdown` (§3.1 pseudocode).

### 7.9 `src/polycopy/discovery/scoring/v2/__init__.py` NOUVEAU

Exporte public API + declare `_CURRENT_POOL_CONTEXT` contextvar.

### 7.10 `src/polycopy/discovery/metrics_collector_v2.py` NOUVEAU

Wrapper autour de `MetricsCollector` existant :

```python
class MetricsCollectorV2:
    """Extension M12 pour collecter les metrics v2 (Sortino, Brier, timing, etc.).

    Réutilise MetricsCollector M5 pour win_rate/roi/hhi/volume/position_count.
    Fetch additionnel : trader_daily_pnl DB + /activity window event + Gamma
    categories + raw positions pour zombie_ratio.
    """

    def __init__(
        self,
        base_collector: MetricsCollector,
        daily_pnl_repo: TraderDailyPnlRepository,
        gamma_client: GammaApiClient,
        data_api: DiscoveryDataApiClient,
        settings: Settings,
    ) -> None: ...

    async def collect(self, wallet_address: str) -> TraderMetricsV2:
        base = await self._base.collect(wallet_address)
        # Fetch equity curve
        curve = await self._daily_pnl_repo.get_curve(wallet_address, days=settings.scoring_v2_window_days)
        # Compute Sortino/Calmar
        sortino = _sortino_ratio([c.equity_usdc for c in curve], risk_free_rate=0.0)
        calmar = _calmar_ratio([c.equity_usdc for c in curve])
        # Fetch raw positions pour zombie_ratio (réutilise base fetch)
        positions = await self._data_api.get_positions(wallet_address)
        zombie = _compute_zombie_ratio(positions)
        # Fetch /activity pour timing + sizing_cv
        activity = await self._data_api.get_activity_trades(wallet_address, since=...)
        timing_alpha_weighted = await self._compute_timing_alpha_wallet(wallet_address, activity)
        sizing_cv = _compute_sizing_cv(activity)
        # Fetch Gamma categories pour HHI catégories
        condition_ids = list({t.get("conditionId") for t in activity if t.get("conditionId")})
        market_to_category = await self._fetch_categories(condition_ids)
        hhi_cat = _compute_hhi_categories(activity, market_to_category)
        # Compute Brier
        brier = _compute_brier(positions)
        # Compute consistency (mensuelle)
        monthly_ratio = _compute_monthly_ratio([(c.date, c.equity_usdc) for c in curve])
        # trade_count_90d, days_active, cash_pnl_90d
        trade_count = len(activity)
        days_active = _compute_days_active(activity)
        cash_pnl_90d = sum(float(p.cash_pnl) for p in positions if p.is_resolved)
        return TraderMetricsV2(
            **base.model_dump(),
            sortino_90d=sortino,
            calmar_90d=calmar,
            brier_90d=brier,
            timing_alpha_weighted=timing_alpha_weighted,
            hhi_categories=hhi_cat,
            monthly_pnl_positive_ratio=monthly_ratio,
            zombie_ratio=zombie,
            sizing_cv=sizing_cv,
            cash_pnl_90d=cash_pnl_90d,
            trade_count_90d=trade_count,
            days_active=days_active,
            monthly_equity_curve=[c.equity_usdc for c in curve],
        )
```

### 7.11 `src/polycopy/discovery/trader_daily_pnl_writer.py` NOUVEAU

~150 lignes. Scheduler co-lancé dans `DiscoveryOrchestrator.run_forever()` TaskGroup. Contenu §5.6.

### 7.12 `src/polycopy/discovery/orchestrator.py` (lignes 88-131 + 172-214)

**Diff M12** :

- Ligne 88 (dans `async with httpx.AsyncClient`) : instancier `metrics_collector_v2 = MetricsCollectorV2(metrics_collector, daily_pnl_repo, gamma, data_api, cfg)` + `daily_pnl_writer = TraderDailyPnlWriter(...)`.
- TaskGroup additionnel : lancer `daily_pnl_writer.run_forever(stop_event)` en parallèle du cycle scoring principal. Lancer **uniquement si** `cfg.trader_daily_pnl_enabled`.
- Lignes 140-170 (boucle `_run_one_cycle`) : **pre-build pool_context si v2 ou shadow actif** (§5.1 pseudocode).
- Ligne 171-230 (boucle scoring) : ajouter branche dual-compute v1+v2 (§5.1 pseudocode).
- Ajouter helper `_is_v2_shadow_active(self, cycle_at) -> bool` : vérifie si la shadow period est encore ouverte (décompte depuis le 1ᵉʳ row `trader_scores WHERE scoring_version='v2'` en DB, ou depuis `.env` `SCORING_V2_SHADOW_STARTED_AT` — **trancher §13** → v1 = query DB `MIN(cycle_at)` une fois par cycle).
- Ajouter helper `_build_pool_context(candidate_wallets) -> PoolContext` : fetch metrics_v2 partiels pour calculer pool-wide values + brier_baseline.

### 7.13 `src/polycopy/storage/models.py` (+ `TraderDailyPnl`)

**Diff M12** : ajouter classe `TraderDailyPnl` après `TraderEvent` (ligne ~315) :

```python
class TraderDailyPnl(Base):
    """Snapshot quotidien equity curve par wallet (M12, append-only).

    Source de l'equity curve pour Sortino/Calmar dans scoring v2.
    Écrit par TraderDailyPnlWriter (scheduler 24h). Dédup via
    contrainte unique (wallet_address, date).
    """

    __tablename__ = "trader_daily_pnl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    equity_usdc: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl_day: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl_day: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    positions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshotted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False,
    )
    __table_args__ = (
        UniqueConstraint("wallet_address", "date", name="uq_trader_daily_pnl_wallet_date"),
        Index("ix_trader_daily_pnl_wallet_date", "wallet_address", "date"),
    )
```

### 7.14 `src/polycopy/storage/repositories.py` (+ `TraderDailyPnlRepository`)

Nouvelle classe ~80 lignes après `TradeLatencyRepository` :

```python
class TraderDailyPnlRepository:
    """CRUD trader_daily_pnl M12. Append-only + curve lookup."""

    async def insert_if_new(self, dto: TraderDailyPnlDTO) -> bool: ...
    async def get_curve(self, wallet: str, *, days: int) -> list[TraderDailyPnl]: ...
    async def get_curves_batch(self, wallets: list[str], *, days: int) -> dict[str, list[TraderDailyPnl]]: ...
```

### 7.15 `src/polycopy/storage/dtos.py` (+ `TraderDailyPnlDTO`)

Ajout DTO Pydantic frozen — pattern cohérent `TraderScoreDTO`.

### 7.16 `alembic/versions/0006_m12_trader_daily_pnl.py` NOUVEAU

Migration strictement additive (aucune modif sur tables M5/M11) :

```python
"""M12 trader daily pnl snapshots.

Ajoute la table `trader_daily_pnl` pour l'equity curve quotidienne par wallet.
Source de Sortino/Calmar/consistency dans scoring v2.
Revision ID: 0006_m12_trader_daily_pnl
Revises: 0005_m11_latency_samples
"""

def upgrade() -> None:
    op.create_table(
        "trader_daily_pnl",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet_address", sa.String(42), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("equity_usdc", sa.Float, nullable=False, server_default="0"),
        sa.Column("realized_pnl_day", sa.Float, nullable=False, server_default="0"),
        sa.Column("unrealized_pnl_day", sa.Float, nullable=False, server_default="0"),
        sa.Column("positions_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("snapshotted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("wallet_address", "date", name="uq_trader_daily_pnl_wallet_date"),
    )
    op.create_index("ix_trader_daily_pnl_wallet_address", "trader_daily_pnl", ["wallet_address"])
    op.create_index("ix_trader_daily_pnl_date", "trader_daily_pnl", ["date"])
    op.create_index("ix_trader_daily_pnl_wallet_date", "trader_daily_pnl", ["wallet_address", "date"])

def downgrade() -> None:
    op.drop_index("ix_trader_daily_pnl_wallet_date", "trader_daily_pnl")
    op.drop_index("ix_trader_daily_pnl_date", "trader_daily_pnl")
    op.drop_index("ix_trader_daily_pnl_wallet_address", "trader_daily_pnl")
    op.drop_table("trader_daily_pnl")
```

### 7.17 `src/polycopy/dashboard/routes.py` (+ `/traders/scoring`)

**Diff M12** :

- Nouvelle route `@router.get("/traders/scoring", response_class=HTMLResponse)` dans `build_pages_router()`.
- Nouvelle route partial `@router.get("/partials/traders-scoring-rows")` pour HTMX polling.
- Nouvelle query dans `dashboard/queries.py` : `compute_scoring_comparison(sf) -> dict` (v1/v2 agrégats + Spearman + Brier).

### 7.18 `src/polycopy/dashboard/templates/traders_scoring.html` NOUVEAU

~120 lignes Jinja2 + Tailwind CDN + Chart.js (scatter v1 vs v2). Cohérent pattern M6.

### 7.19 `src/polycopy/dashboard/templates/base.html` (sidebar link)

**Diff M12** : ajouter une entrée sous-niveau `/traders/scoring` (ou lien secondaire à côté `/traders`). Option A : entrée séparée dans la sidebar (alignée `/traders`). Option B : sous-lien visible uniquement depuis `/traders`.

**Décision** : option A (entrée séparée "Scoring (v1/v2)") cohérent avec autres onglets (pas de hiérarchie nav actuelle).

### 7.20 `scripts/backtest_scoring_v2.py` NOUVEAU

~400 lignes Python CLI sync (asyncio.run interne). Lit fixtures, calcule v1 et v2, produit rapport HTML + JSON.

### 7.21 `assets/scoring_v2_labels.csv` NOUVEAU

~50-100 lignes CSV wallets publics labelés. Sourcé via `/holders` top markets 2025-Q4 + contrôle random `/trades` feed.

### 7.22 `src/polycopy/cli/runner.py` (wire v2)

**Diff M12** :

- Pas de changement direct sur le boot `init_db` (migration 0006 appliquée automatiquement).
- Ajouter un log `cli_boot_info` au début du cli entry : `scoring_version=<v1|v2>` + `scoring_v2_shadow_days=<N>` pour visibilité immédiate.
- Si `settings.scoring_v2_cold_start_mode=true` : log WARNING "scoring v2 cold_start_mode actif, gate trade_count_90d relâché à ≥ 20".

### 7.23 `docs/development/m12_backtest_seed.md` NOUVEAU

Documentation du set labelé : méthodologie, sourcing, critères smart_money vs random, mise à jour périodique.

---

## 8. Plan d'implémentation séquentiel

Ordre testable isolément. Estimé ~2-3 semaines 1 dev + période shadow 14j calendaire.

### Étape 1 — Capturer fixture Gamma categories (jour 1 matin)

**Bloquant.** Si Gamma ne fournit pas de champ `category`/`tags` utilisable, le facteur `specialization` tombe sur une heuristique fragile (regex sur titres).

- Invoquer skill `/polymarket:polymarket` pour interroger le schéma `/markets` complet.
- Capturer fixture `tests/fixtures/gamma_markets_categories_sample.json` : 30-50 marchés avec champ `category` ou `tags` visible.
- **STOP si** : Gamma ne retourne ni `category` ni `tags` exploitables → fallback heuristique (regex simple) documenté §13 open question.

### Étape 2 — Refresh fixtures Data API M5 (jour 1 matin, parallèle)

- `tests/fixtures/data_api_positions_*.json` : refresh sur 3 wallets labelés (pour backtest).
- `tests/fixtures/data_api_activity_*.json` : idem.
- Capturer `tests/fixtures/data_api_positions_resolved_for_brier_sample.json` : 1 wallet avec ≥ 30 positions résolues (besoin Brier calcul).

### Étape 3 — Baseline tests M1..M11 (jour 1 après-midi)

```bash
pytest tests/unit tests/integration -v 2>&1 | tee /tmp/m12_baseline.txt
```

Capture nombre de tests passants actuels. Sera utilisé en non-régression fin M12.

### Étape 4 — Migration Alembic 0006 + model + repository (jour 1 après-midi)

- `alembic/versions/0006_m12_trader_daily_pnl.py` (§7.16).
- `TraderDailyPnl` model dans `storage/models.py` (§7.13).
- `TraderDailyPnlRepository` dans `storage/repositories.py` (§7.14).
- `TraderDailyPnlDTO` dans `storage/dtos.py` (§7.15).
- Test `test_m12_alembic_migration_applies_and_rolls_back` (§9.3.E).

### Étape 5 — `TraderDailyPnlWriter` scheduler (jour 2)

- Nouveau fichier `discovery/trader_daily_pnl_writer.py` (§7.11).
- Wire dans `DiscoveryOrchestrator.run_forever()` TaskGroup conditionnellement (`cfg.trader_daily_pnl_enabled`).
- Tests : mock `data_api.get_positions + get_value`, vérifier insert idempotent sur dédup `(wallet, date)`.

### Étape 6 — Sous-package `scoring/` ouverture + move `scoring.py` → `v1.py` (jour 2)

- `git mv src/polycopy/discovery/scoring.py src/polycopy/discovery/scoring/v1.py`.
- Nouveau `src/polycopy/discovery/scoring/__init__.py` ré-exporte `compute_score`, `SCORING_VERSIONS_REGISTRY`.
- Vérifier que tous les imports existants fonctionnent (non-régression — aucun test M5 ne doit casser).

### Étape 7 — DTOs v2 (`TraderMetricsV2`, `ScoreV2Breakdown`, `PoolContext`, gates) (jour 3)

- `src/polycopy/discovery/scoring/v2/dtos.py` (§7.4).
- Tests : validation Pydantic, frozen, immutabilité.

### Étape 8 — `normalization.py` + tests idempotence (jour 3)

- `src/polycopy/discovery/scoring/v2/normalization.py` (§3.8 + §7.6).
- Tests idempotence + edge cases (pool vide, pool dégénéré, outliers).

### Étape 9 — 6 facteurs purs + tests isolés (jour 4-5)

- 6 fichiers `factors/*.py` (§3.2-3.7 + §7.5).
- 1 test par facteur en isolation (fixtures in-memory, pas d'I/O).

### Étape 10 — `gates.py` + tests 6 gates isolés (jour 5 matin)

- `src/polycopy/discovery/scoring/v2/gates.py` (§4.1-4.2 + §7.7).
- 6 tests `test_gate_*_passes` + 6 tests `test_gate_*_fails_with_reason`.

### Étape 11 — `aggregator.py` + `compute_score_v2` (jour 5 après-midi)

- `src/polycopy/discovery/scoring/v2/aggregator.py` (§3.1 + §7.8).
- Registry entry `SCORING_VERSIONS_REGISTRY["v2"]` dans `scoring/__init__.py`.
- Tests : `test_compute_score_v2_end_to_end` sur metrics synthétiques + `ScoreV2Breakdown` complet.

### Étape 12 — `MetricsCollectorV2` wrapper (jour 6-7)

- Nouveau fichier `discovery/metrics_collector_v2.py` (§7.10).
- Tests : mock `data_api + gamma + daily_pnl_repo`, vérifier calcul complet `TraderMetricsV2`.

### Étape 13 — Feature flags config (jour 7 matin)

- Modif `src/polycopy/config.py` (§7.1) : 7 nouveaux champs + validator `wash_cluster_wallets` + cross-field warnings.
- Promotion `scoring_version` à `Literal["v1", "v2"]`.
- Tests `test_config.py` étendus pour nouveaux champs.

### Étape 14 — Orchestrator dual-compute mode (jour 7-8)

- Modif `src/polycopy/discovery/orchestrator.py` (§7.12) : pre-build pool_context + branche dual-compute.
- `_is_v2_shadow_active` query DB helper.
- `_build_pool_context` : fetch metrics v2 sur pool candidats (séquentiel, cache LRU intra-cycle).
- Tests `test_orchestrator_shadow_dual_compute`, `test_orchestrator_v2_pilot_after_cutover`, `test_orchestrator_v1_pilot_default` (§9.3).

### Étape 15 — Gate rejection audit trail (jour 8 après-midi)

- Ajouter branche `gate_rejected` dans orchestrator (§4.3).
- Helper `_build_gate_rejected_event_dto(wallet, gates, current) -> TraderEventDTO`.
- Tests `test_gate_rejected_event_written`, `test_gate_rejected_skips_scoring`, `test_gate_rejected_does_not_affect_v1_decisions`.

### Étape 16 — Dashboard `/traders/scoring` route + query + template (jour 9-10)

- Route `traders_scoring_page` + partial `/partials/traders-scoring-rows`.
- Query `compute_scoring_comparison(sf)` dans `dashboard/queries.py`.
- Template `traders_scoring.html` (§7.18).
- Sidebar link dans `base.html` (§7.19).
- Tests `test_traders_scoring_page_renders`, `test_scoring_comparison_query_agrrégats`, `test_cutover_button_disabled_during_shadow`.

### Étape 17 — Script backtest + fixture labels (jour 10-11)

- `scripts/backtest_scoring_v2.py` (§5.3 + §7.20).
- `assets/scoring_v2_labels.csv` (§7.21, 50 wallets initial).
- Fixtures `tests/fixtures/scoring_v2/*.json`.
- Test `test_backtest_script_produces_report`, `test_backtest_brier_aggregate_correctness`, `test_backtest_spearman_rank_correctness`.

### Étape 18 — Tests A/B/C/D/E complets (jour 11-12)

Cf. §9.3 sous-sections.

### Étape 19 — Smoke test shadow period (jour 12)

- Lancer bot avec `SCORING_VERSION=v1`, `SCORING_V2_SHADOW_DAYS=1`, `DISCOVERY_ENABLED=true`, `DISCOVERY_INTERVAL_SECONDS=3600`. Observer 2 cycles.
- Vérifier que `trader_scores` contient des rows `scoring_version="v1"` ET `scoring_version="v2"`.
- Vérifier que `trader_events` contient des `gate_rejected` pour wallets qui ratent les gates.
- Vérifier que `target_traders.score` (colonne overwrite) reste pilotée par v1 (comparaison avec rows v1 `trader_scores`).
- Dashboard `/traders/scoring` : table affiche les deux scores.
- **STOP et investiguer si** : aucun row v2 écrit (pool_context build échoué), ou si `decision_engine` a été influencé par v2 (bug critique).

### Étape 20 — Backtest run + rapport (jour 13)

- Lancer `scripts/backtest_scoring_v2.py --labels-file assets/scoring_v2_labels.csv --as-of <date> --output /tmp/backtest_v2_report.html`.
- Vérifier que le rapport est lisible + métriques agrégées cohérentes.
- Si Brier v2 ≥ Brier v1 - 0.01 → **investiguer la formule** avant merge (bug dans pondération ou normalisation).

### Étape 21 — README + CLAUDE.md + docs updates (jour 14)

- CLAUDE.md §Sécurité : ajouter bullet "Scoring v2 M12" (§10.1).
- CLAUDE.md §Conventions : ajouter bullet "Versioning scoring (M12+)" (§10.2).
- CLAUDE.md §APIs Polymarket : aucune modif (pas de nouvelle source API — Gamma `category` déjà couvert M2).
- CLAUDE.md §Architecture : update diagramme strategy/discovery.
- `docs/architecture.md` : section Discovery enrichie.
- `docs/setup.md` : smoke test shadow period + backtest.
- `.env.example` : bloc §6.4.

Commit final unique : `feat(discovery,storage,dashboard): M12 scoring v2 (formule hybride + gates durs + shadow period)`.

---

## 9. Tests

### 9.1 À protéger (existants à ne pas casser)

| Fichier | Test | Raison |
|---|---|---|
| `tests/unit/test_config.py:*` | tous | Non-régression config (7 champs additifs) |
| `tests/unit/test_discovery_*.py:*` | tous M5 | Scoring v1 inchangé, lifecycle M5 intact, `SCORING_VERSION=v1` default |
| `tests/unit/test_decision_engine*.py:*` | tous | **Critique** : DecisionEngine logique M5 non touchée |
| `tests/unit/test_scoring.py::*` | tous | Formule v1 intacte (code déplacé `v1.py` mais pas modifié) |
| `tests/unit/test_metrics_collector.py:*` | tous | `MetricsCollector` M5 intact ; `MetricsCollectorV2` compose, ne modifie pas |
| `tests/unit/test_candidate_pool.py:*` | tous | Pool construction inchangée |
| `tests/unit/test_alembic_migrations.py:*` | tous | 0001-0005 inchangées |
| `tests/unit/test_dashboard_routes.py:*` | tous | `/traders` existant inchangé, `/traders/scoring` ajout |
| `tests/unit/test_dashboard_security.py:*` + `test_dashboard_security_m6.py:*` | tous | Invariants dashboard préservés (M12 read-only strict) |
| `tests/unit/test_trader_*_repository.py:*` | tous | `TargetTraderRepository.transition_status` intacte (pinned raise), score/event repos intactes |
| `tests/unit/test_clob_ws_client.py:*` + `test_cache_policy.py:*` + `test_latency_*.py:*` | tous | **Invariants M11** préservés |
| `tests/unit/test_pnl_writer_*.py:*` | tous | **Invariants M10 kill switch 3 modes** inchangés |
| `tests/unit/test_executor_orchestrator.py:*` | tous | 4 garde-fous M3/M8/M10 préservés |
| `tests/unit/test_middleware_log_filter.py:*` | tous | Processor M10 intact |
| `tests/unit/test_telegram_*.py:*` | tous | Badges M10 + templates M7 intacts |

### 9.2 À adapter (signature evolutions)

| Test | Changement |
|---|---|
| `tests/unit/test_scoring.py::test_scoring_version_registered` | Assertion `"v1"` → `{"v1", "v2"}` (registry gagne une entrée) |
| `tests/unit/test_config.py::test_scoring_version_default` | `scoring_version == "v1"` (inchangé), mais **type** devient `Literal["v1", "v2"]` — validator invalide "v3" etc. |
| `tests/unit/test_orchestrator_discovery.py::test_*_scoring` | Orchestrator gagne 1 kwarg `metrics_collector_v2` dans TaskGroup setup. Tests existants qui monkey-patch `MetricsCollector` doivent vérifier que v2 n'est pas utilisé si `SCORING_VERSION=v1` ET `SCORING_V2_SHADOW_DAYS=0`. |
| `tests/unit/test_discovery_dtos.py::test_scoring_result_roundtrip` | Backward-compat : `ScoringResult.scoring_version: str` reste (v2 écrit `"v2"` dans la même colonne) |

### 9.3 À ajouter (nouveaux) — inventaire exhaustif

Tests nouveaux M12 regroupés par sous-feature.

#### 9.3.A — Winsorisation + normalisation pool (6 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_winsorize_p5_p95_returns_correct_bounds` | `tests/unit/test_scoring_v2_normalization.py` | `[0, 1, 2, ..., 99]` → p5=5, p95=95 |
| `test_apply_pool_normalization_idempotent` | idem | `apply(apply(x, pool), pool) == apply(x, pool)` (invariant critique) |
| `test_apply_pool_normalization_clips_outliers_high` | idem | x > p95 → 1.0 |
| `test_apply_pool_normalization_clips_outliers_low` | idem | x < p5 → 0.0 |
| `test_apply_pool_normalization_empty_pool` | idem | pool vide → wallet_value clippé [0, 1] |
| `test_apply_pool_normalization_degenerate_pool` | idem | pool [1.0, 1.0, 1.0] → 0.5 (sentinel) |

#### 9.3.B — Facteurs purs (6 tests + cas limites)

| Test | Fichier | Contrat |
|---|---|---|
| `test_compute_risk_adjusted_sortino_dominant` | `tests/unit/test_scoring_v2_factors.py` | metrics avec Sortino=2, Calmar=0 → 1.2 (0.6×2) |
| `test_compute_risk_adjusted_empty_curve_returns_zero` | idem | curve < 14 points → 0.0 |
| `test_compute_calibration_brier_skill_positive` | idem | brier=0.15, baseline=0.25 → 0.4 |
| `test_compute_calibration_brier_none_returns_zero` | idem | brier=None → 0.0 |
| `test_compute_timing_alpha_clipped` | idem | timing_alpha_weighted=1.5 → clippé 1.0 |
| `test_compute_specialization_hhi_inverted` | idem | hhi=0.7 → specialization=0.3 |
| `test_compute_consistency_fraction_months` | idem | 2/3 mois positifs → 0.667 |
| `test_compute_discipline_product` | idem | zombie=0.3, sizing_cv=0.2 → (1-0.3)×(1-0.2) = 0.56 |
| `test_compute_discipline_high_zombie_penalized` | idem | zombie=0.9 → discipline=0.08 × sizing_stability |

#### 9.3.C — 6 gates durs (12 tests, 2 par gate)

| Test | Fichier | Contrat |
|---|---|---|
| `test_gate_cash_pnl_positive_passes` | `tests/unit/test_scoring_v2_gates.py` | `cash_pnl_90d=100` → passed |
| `test_gate_cash_pnl_positive_fails_with_reason` | idem | `cash_pnl_90d=-50` → failed, reason="cash_pnl:-50.0" |
| `test_gate_trade_count_min_passes` | idem | `trade_count_90d=60` → passed |
| `test_gate_trade_count_min_fails` | idem | `trade_count_90d=30` → failed |
| `test_gate_trade_count_cold_start_relaxed` | idem | `cold_start_mode=true` + count=25 → passed |
| `test_gate_days_active_passes` | idem | 60 days → passed |
| `test_gate_days_active_fails` | idem | 15 days → failed |
| `test_gate_zombie_ratio_passes` | idem | 0.2 → passed |
| `test_gate_zombie_ratio_fails` | idem | 0.5 → failed |
| `test_gate_not_blacklisted_passes` | idem | wallet hors env → passed |
| `test_gate_not_blacklisted_fails` | idem | wallet dans env → failed |
| `test_gate_not_wash_cluster_passes_and_fails` | idem | idem pattern blacklist |
| `test_check_all_gates_fail_fast` | idem | première fail retourne, ne check pas les suivantes |
| `test_check_all_gates_all_pass_returns_passed` | idem | 6 passent → AggregateGateResult(passed=True) |

#### 9.3.D — `compute_score_v2` agrégé + shadow period (8 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_compute_score_v2_end_to_end` | `tests/unit/test_scoring_v2_aggregator.py` | metrics synthétiques + pool_context → score ∈ [0, 1] + breakdown complet |
| `test_compute_score_v2_weights_sum_to_one` | idem | Vérifie pondération 0.25+0.20+0.20+0.15+0.10+0.10 = 1.0 |
| `test_compute_score_v2_zero_if_all_factors_zero` | idem | Tous facteurs raw=0 → score=0 |
| `test_score_v2_normalized_bounded` | idem | Après normalisation, chaque sous-score ∈ [0, 1] |
| `test_orchestrator_shadow_dual_compute` | `tests/unit/test_orchestrator_scoring_v2.py` | v1 + v2 tous deux écrits dans `trader_scores` si shadow actif |
| `test_orchestrator_v2_shadow_does_not_affect_decisions` | idem | `DecisionEngine.decide` reçoit v1 score même si v2 disponible |
| `test_orchestrator_v2_pilot_after_cutover` | idem | `SCORING_VERSION=v2` + shadow=0 → décisions pilotées par v2 |
| `test_orchestrator_no_shadow_when_disabled` | idem | `SCORING_V2_SHADOW_DAYS=0` ET `SCORING_VERSION=v1` → v2 pas calculé, seul v1 écrit |

#### 9.3.E — Gates audit trail + orchestrator integration (4 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_gate_rejected_event_written_to_trader_events` | `tests/unit/test_orchestrator_gates.py` | wallet zombie 0.5 → row `trader_events.event_type="gate_rejected"` avec reason="zombie_ratio:0.5" |
| `test_gate_rejected_never_scores_in_v2_mode` | idem | En `SCORING_VERSION=v2`, aucun row `trader_scores` écrit pour wallet rejected |
| `test_gate_rejected_still_scores_v1_in_shadow_mode` | idem | En shadow (v1 pilote), v1 continue de scorer même si v2 gate fail |
| `test_gate_rejected_event_metadata_correct` | idem | `event_metadata={"gate": "zombie_ratio_max", "value": 0.5, "threshold": 0.4}` |

#### 9.3.F — Dashboard `/traders/scoring` (4 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_traders_scoring_page_renders` | `tests/unit/test_dashboard_scoring_route.py` | `/traders/scoring` retourne 200 + HTML contient "Scoring comparison" |
| `test_scoring_comparison_query_aggregates` | idem | Query retourne `{"brier_top10_v1": ..., "brier_top10_v2": ..., "spearman": ..., "delta_top10": ...}` |
| `test_cutover_button_disabled_during_shadow` | idem | Template affiche bouton disabled si `shadow_days_remaining > 0` |
| `test_cutover_button_enabled_when_ready` | idem | `SCORING_V2_CUTOVER_READY=true` + backtest OK + shadow complete → bouton enabled |

#### 9.3.G — Migration + TraderDailyPnl + TraderDailyPnlWriter (6 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_m12_alembic_migration_applies_and_rolls_back` | `tests/integration/test_m12_migration.py` (opt-in) | upgrade crée table + indexes, downgrade supprime |
| `test_trader_daily_pnl_unique_constraint` | `tests/unit/test_trader_daily_pnl_repo.py` | insert 2× même (wallet, date) → 2ᵉ lève IntegrityError OR silently skipped |
| `test_trader_daily_pnl_get_curve_ordered_by_date` | idem | `get_curve(wallet, days=30)` retourne liste ordonnée date ASC |
| `test_trader_daily_pnl_writer_scans_active_wallets` | `tests/unit/test_trader_daily_pnl_writer.py` | writer scanne statuses shadow/active/paused/pinned, skip "absent" |
| `test_trader_daily_pnl_writer_idempotent_same_day` | idem | 2 runs dans la même journée → 1 row par wallet (dedup via unique) |
| `test_trader_daily_pnl_writer_disabled_flag` | idem | `TRADER_DAILY_PNL_ENABLED=false` → scheduler pas lancé |

#### 9.3.H — Backtest script + fixture (3 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_backtest_script_produces_report_json` | `tests/unit/test_backtest_scoring_v2.py` | Run script sur fixtures → rapport JSON parseable avec clés attendues |
| `test_backtest_brier_aggregate_computation` | idem | 10 wallets fixtures avec outcomes connus → Brier calculé correctement |
| `test_backtest_spearman_rank_correctness` | idem | Ranks v1 [1,2,3,4] et v2 [2,1,4,3] → Spearman ≈ 0.8 |

#### 9.3.I — Config + CLI boot log (2 tests)

| Test | Fichier | Contrat |
|---|---|---|
| `test_m12_config_defaults` | `tests/unit/test_config.py` (ajout) | 7 nouveaux champs ont les défauts attendus |
| `test_m12_cli_boot_log_scoring_version` | `tests/unit/test_cli_runner.py` (ajout) | Log `cli_boot_info` contient `scoring_version="v1"` par défaut |

**Total** : **51 nouveaux tests unit + 1 intégration (migration)**. Couverture cible ≥ 80 % sur `src/polycopy/discovery/scoring/v2/` (cœur critique), `MetricsCollectorV2`, `TraderDailyPnlWriter`, `TraderDailyPnlRepository`, `dashboard/queries.py` (partie scoring_comparison).

---

## 10. Impact CLAUDE.md — texte de remplacement exact

3 passages à insérer dans `CLAUDE.md`. Pas de modif §APIs Polymarket (aucune nouvelle source data — Gamma `/markets` déjà couvert M2).

### 10.1 Section "Sécurité — RÈGLES STRICTES"

**Ajouter** (après bullet "Pipeline temps réel M11") :

```
- **Scoring v2 M12** : `SCORING_VERSION=v1` par défaut — invariant lifecycle M5 strict. Flip vers `v2` = décision humaine manuelle **uniquement après** (1) `SCORING_V2_SHADOW_DAYS` écoulés, (2) rapport `scripts/backtest_scoring_v2.py` montrant Brier v2 < Brier v1 - 0.01 sur set labelé `assets/scoring_v2_labels.csv` (≥50 wallets). Aucun auto-flip. Pendant la shadow period, v2 calcule en parallèle (double-write `trader_scores` avec `scoring_version="v2"`) mais ne pilote **jamais** `DecisionEngine`. Les 6 gates durs (`cash_pnl_90d>0`, `trade_count_90d≥50`, `days_active≥30`, `zombie_ratio<0.40`, not blacklisted, not `WASH_CLUSTER_WALLETS`) s'appliquent uniquement en v2 — un wallet rejeté écrit `trader_events.event_type="gate_rejected"` avec raison explicite, **jamais scoré**. `WASH_CLUSTER_WALLETS` env var discipline identique `BLACKLISTED_WALLETS` (exclusion absolue, vérifiée 2× pre-pool + pre-scoring). `TraderDailyPnl` table contient uniquement wallet_address publique + equity_usdc + date — **aucun secret, aucun PII**. Versioning sacré : chaque row `trader_scores` porte sa `scoring_version`, **jamais** rewrite rétroactif d'une formule. La table n'est jamais purgée.
```

### 10.2 Section "Conventions de code"

**Ajouter** (après bullet "Instrumentation latence (M11+)") :

```
- **Scoring versionné (M12+)** : formule `Score_v2` vit dans un sous-package isolé `src/polycopy/discovery/scoring/v2/` avec pure functions par facteur (6 facteurs : `risk_adjusted`, `calibration`, `timing_alpha`, `specialization`, `consistency`, `discipline`). Chaque facteur testable isolément (I/O séparé via `MetricsCollectorV2`). Winsorisation p5-p95 pool-wide + normalisation 0-1 géré par `normalization.py` (pure, idempotent). Registry `SCORING_VERSIONS_REGISTRY: dict[Literal["v1","v2"], Callable]` dans `scoring/__init__.py`. Pondération figée en code (`0.25/0.20/0.20/0.15/0.10/0.10`) — changer la pondération = bumper `SCORING_VERSION` (ex: `v2.1`) et **jamais** rewrite rétroactif des rows `trader_scores` historiques (audit trail sacré). Feature flag `SCORING_VERSION=v1` par défaut, `SCORING_V2_SHADOW_DAYS=14` pour coexistence v1/v2 avant cutover manuel.
```

### 10.3 Section "Architecture (rappel)"

**Modifier** le bloc `strategy/` → `discovery/` pour inclure v2 :

**Actuel (ligne ~30)** :

```
├── discovery/    Pool candidats + scoring + decisions (M5, opt-in, read-only)
```

**Remplacer par** :

```
├── discovery/
│   ├── scoring/v1.py             Formule v1 M5 (consistency+roi+div+vol)
│   ├── scoring/v2/factors/       6 facteurs purs M12 (Sortino+Brier+timing+HHI+consistency+discipline)
│   ├── scoring/v2/gates.py       6 gates durs M12 (pre-scoring anti-gaming)
│   ├── scoring/v2/normalization.py Winsorisation p5-p95 pool-wide
│   ├── metrics_collector_v2.py   Wrapper autour de MetricsCollector M5 + TraderDailyPnl
│   ├── trader_daily_pnl_writer.py Scheduler 24h equity curve (prérequis v2 Sortino)
│   └── ... (M5 candidate_pool, decision_engine, orchestrator inchangés)
```

### 10.4 Section "APIs Polymarket utilisées" — aucune modif

M12 ne consomme aucun endpoint API nouveau. Gamma `/markets` couvre déjà les catégories (à confirmer par fixture §8 étape 1). Data API `/positions`, `/activity`, `/value` déjà documentés M5. Aucun delta.

---

## 11. Risques et mitigations

### 11.1 Risque critique — formule v2 biaisée non détectée par le backtest

**Scénario** : le set `assets/scoring_v2_labels.csv` (~50-100 wallets) est trop petit ou biaisé (ex: majorité Politics → specialization gonflé sur cette catégorie, sous-représente Sports/Crypto). Backtest valide v2, cutover, mais en prod v2 dégrade le pool → drawdown accru.

**Impact** : capital copié pire qu'avec v1. Signal tardif (detecté via dashboard `/pnl` après 2-4 semaines).

**Mitigations** :

1. Backtest methodology documentée (§5.3) : ratio smart_money/random ~50/50, sourcing diversifié (`/holders` top 5 catégories × 10 wallets/catégorie + `/trades` feed global top 50 volume).
2. Seuil de signification strict : Brier v2 < v1 - 0.01 (pas juste < v1).
3. Double shadow après cutover : v1 continue de calculer 14 j supplémentaires, permet rollback si régression.
4. Kill switch M4 préservé (indépendant du scoring) : si drawdown > `KILL_SWITCH_DRAWDOWN_PCT`, bot s'arrête, user réagit.
5. README + CLAUDE.md §Sécurité : warning explicite "flip v2 = décision user éclairée après audit backtest".

**Condition STOP** : si backtest v2 ≥ v1 − 0.005 (presque indistinguable) → ne pas merger le cutover, seulement la shadow.

### 11.2 Risque moyen — shadow period trop courte pour converger

**Scénario** : 14 jours = ~2-3 cycles discovery (cadence 6h → 56 cycles, mais certains wallets scorés 1× seulement). Set trop petit → Brier aggregate peu fiable.

**Impact** : décision cutover prématurée.

**Mitigations** :

1. `SCORING_V2_SHADOW_DAYS` configurable [0, 90] — user peut étendre si besoin.
2. Dashboard `/traders/scoring` affiche "Samples collected: X scores v1 / Y scores v2" — user voit si convergé.
3. Documentation : recommander 30 j si pool < 50 wallets.
4. Gate dashboard : bouton cutover désactivé si samples_v2 < 100 (à valider en implémentation).

### 11.3 Risque moyen — migration 0006 sur DB user préexistante

**Scénario** : user avec DB M11 pull `main` post-M12 → `init_db` applique `alembic upgrade head` → 0006 crée `trader_daily_pnl`.

**Impact** : aucun, migration additive. Mais la table est vide jusqu'à la 1ère exec `TraderDailyPnlWriter` (J+1) → v2 en shadow retourne `sortino=0` pour tous les wallets la 1ère semaine.

**Mitigations** :

1. Documenter : shadow period **effective** = à partir du 14ᵉ jour après le 1er snapshot daily_pnl (donc ~15 j post-upgrade). Pas d'urgence.
2. Alternative backfill synthetic : pas v1 (complexité). Reportable §14.
3. Test d'intégration `test_m12_alembic_migration_applies_and_rolls_back`.

### 11.4 Risque moyen — compute cost calcul journalier

**Scénario** : `_build_pool_context` + `MetricsCollectorV2.collect` sur 100 wallets → ~4-5 calls API additionnels par wallet × 100 = ~500 calls/cycle. À cadence 6h = 2000 calls/jour. `asyncio.Semaphore(5)` déjà en place mais l'overhead peut excéder 10 min/cycle.

**Impact** : cycle discovery qui déborde sur le sleep interval (si > 6h, cycles s'enchaînent sans pause).

**Mitigations** :

1. **Cache intra-cycle** : `PoolContext` construit 1× par cycle, réutilisé pour tous les wallets.
2. `MetricsCollectorV2` fetch réutilise `TraderDailyPnlRepository` (lecture DB locale, ~0.1 ms / wallet) au lieu de re-fetch `/positions` complet.
3. Gamma categories cache LRU 200 marchés × TTL 10 min (couvre 1 cycle entier).
4. Test de stress : exécuter cycle v2 sur 200 wallets mockés, mesurer durée. Cible < 5 min.
5. Si observé trop lent post-merge : batch API calls via `asyncio.gather(...)` — reportable v1.1 de M12.

### 11.5 Risque faible — `brier_baseline_pool` dégénéré (peu de positions résolues)

**Scénario** : pool candidat avec < 100 positions résolues cumulées → baseline Brier = 0.05 (anormalement bas car set trop petit). Brier-skill du wallet = `1 - wallet_brier / 0.05` devient énorme ou négatif irréaliste.

**Impact** : sous-score `calibration` bruyant.

**Mitigations** :

1. Floor `brier_baseline_pool` : si < 0.15 → forcer 0.25 (brier random binaire).
2. Pool normalization absorbe les outliers (p5-p95 clipping).
3. Test `test_calibration_with_tiny_pool` (§9.3.B).

### 11.6 Risque faible — feature flag off régression M5

**Scénario** : `SCORING_VERSION=v1` + `SCORING_V2_SHADOW_DAYS=0` + `TRADER_DAILY_PNL_ENABLED=false` → doit reproduire exactement le comportement M5.

**Impact** : si le refactor `scoring.py → scoring/v1.py` a introduit un bug subtil (ex: import circulaire, registry exposé mal), la non-régression M5 casse.

**Mitigations** :

1. Test `test_orchestrator_no_shadow_when_disabled` (§9.3.D).
2. Baseline tests M1..M11 captée étape 3, comparaison pre/post merge.
3. Documenter le contrat "flag off = M5 strict" dans les docstrings.

### 11.7 Risque faible — Gamma `category` absent → fallback regex titres

**Scénario** : fixture §8 étape 1 révèle que Gamma `/markets` ne retourne ni `category` ni `tags` exploitables.

**Impact** : facteur `specialization` tombe sur regex heuristique (`re.match(r"(election|president|congress|...)", title)`) — fragile, bruité.

**Mitigations** :

1. **STOP spec** : si fixture montre que c'est le cas, ajouter §13 open question + revoir `specialization` (pondération réduite ou désactivée v1).
2. Alternative : agréger par `tag` (même si noisy) — meilleur que rien.
3. v2.1 : intégrer un mapping marché → catégorie custom maintenu dans `assets/gamma_categories_override.csv`.

### 11.8 Risque faible — timing_alpha dépend de Data API `/activity` fiabilité

**Scénario** : `/activity?market=<cid>` ne retourne pas assez de trades historiques (< 50 sur la fenêtre) pour reconstruire courbe `mid_price(t)` → timing_alpha_pair=0.5 (neutre) pour beaucoup de pairs.

**Impact** : facteur `timing_alpha` tire vers 0.5 pool-wide, discriminance réduite.

**Mitigations** :

1. Pool normalization absorbe : si tous les wallets ont timing_alpha≈0.5, la normalisation compresse vers le milieu.
2. Fallback fenêtre élargie : si < 50 trades sur 10 min, étendre à 30 min (heuristique).
3. v2.1 : intégrer RTDS Polymarket (§6.6 synthèse) pour prix temps réel.

---

## 12. Rollout / migration

### 12.1 Séquence

Cohérent avec synthèse §7.2 (ordre M10 → M11 → **M12** → M13+).

1. **T0** — Merge spec M12 sur `main` (sans code).
2. **T0 + 2-3 semaines** — PR code M12 mergée derrière flag `SCORING_VERSION=v1` default. Kill switch rollback dispo : `SCORING_V2_SHADOW_DAYS=0` + `TRADER_DAILY_PNL_ENABLED=false` → comportement M5 strict.
3. **T0 + 2-3 semaines** — README + CLAUDE.md + docs/architecture.md + docs/setup.md dans le **même** commit.
4. **T0 + 3-4 semaines** — Shadow period user-observable : dashboard `/traders/scoring` rend v1|v2 side-by-side. User surveille quotidiennement.
5. **T0 + 4-6 semaines** — User lance `scripts/backtest_scoring_v2.py` sur set labelé, valide rapport.
6. **T0 + 5-6 semaines** — Si backtest OK, user flip manuel `SCORING_VERSION=v2` + set `SCORING_V2_CUTOVER_READY=true` + redémarre.
7. **T0 + 7-8 semaines** — Double shadow inverse (v1 continue 14 j). Si pas de régression détectée via dashboard `/pnl` → user set `SCORING_V2_SHADOW_DAYS=0` (arrêt calcul v1 parallèle).
8. **Version+2 (T0 + 3 mois)** — Si stable, v1 reste accessible en fallback (registry entry conservée). v1 supprimée seulement après M14+ et validation longue (6+ mois v2 en prod).

### 12.2 Rollback

Si régression critique post-flip cutover v2 :

- **Option A (runtime)** : flip `SCORING_VERSION=v1` via `.env` → restaure M5 strict. Aucun redéploiement code.
- **Option B (git)** : revert du commit M12. Migration 0006 reste appliquée (table vide inutilisée, pas de problème).

**Décision** : privilégier option A pour T0+4sem à T0+3mois, passer à option B au-delà si bug structurel v2 détecté.

### 12.3 Communication

- CHANGELOG entry détaillé (les 3 volets).
- README section "Breaking changes M11 → M12" : **aucune breaking change** — ajouts additifs uniquement, `SCORING_VERSION=v1` default.
- Warning CLI au 1er boot post-M12 : "**M12** : scoring v2 available (shadow mode if `SCORING_V2_SHADOW_DAYS>0`). Dashboard `/traders/scoring` pour comparaison v1|v2. Cutover manual — voir docs/setup.md §19."
- Document nouveau `docs/development/m12_cutover_playbook.md` : checklist cutover (backtest, seuils, rollback).

---

## 13. Open questions

Questions dont la réponse n'est pas critique pour démarrer l'implémentation mais à trancher avant cutover final. Les questions **déjà tranchées** dans le prompt utilisateur ("à trancher dans la spec") ne figurent **pas** ici.

1. **Half-life temporel (weighting exponentiel)** : v1 M12 = uniform weight sur 90 j. Synthèse §1.2 + Gemini/Perplexity suggèrent `exp(-age_days/30)`. **À trancher post-backtest** : si backtest montre que les wallets jeunes (< 30 j) sont trop pénalisés ou sur-pondérés, flip à exponentiel v2.1 avec env var `SCORING_V2_HALFLIFE_DAYS=30`.
2. **Apify Leaderboard Scraper** : synthèse §6.3 suggère évaluation coût $5-20/mois vs path `/holders + /trades` actuel. Décision post-M12 : si bootstrap M5 pool semble insuffisant (< 50 candidats qualifiés) après 1 mois M12 actif, évaluer Apify.
3. **Auto-detection wash cluster** : v1 M12 = liste manuelle `WASH_CLUSTER_WALLETS`. Auto-detection par graph clustering on-chain = M17+. Déclencheur : observation manuelle >5 wash clusters majeurs via `/trades` feed.
4. **Compaction table `trader_daily_pnl`** : grows linéairement (100 wallets × 365 j = 36500 rows/an). À 1.5 KB/row = 55 MB/an. OK à 5 ans. Post-M14 : évaluer compaction en quarterly rollups (`trader_quarterly_pnl`) si DB devient lourde. Déclencheur : DB SQLite > 500 MB.
5. **Timing alpha via RTDS** : v1 M12 utilise `/activity` feed aggregate (approximation). RTDS Polymarket `prices` channel donne le mid en temps réel. Post-M14 (phase 2 latence) : si RTDS intégré, migrer `timing_alpha` vers RTDS prix historiques (plus fiable).
6. **Alertes Telegram sur `gate_rejected`** : v1 = log + `trader_events` seul, pas d'alerte Telegram (trop bavard). Reportable M12.1 si user demande (ex: cluster de 10+ rejections sur 1 cycle = signe de wallet poisoning en cours).
7. **Scheduler TraderDailyPnlWriter top-level vs intra-discovery** : v1 = intra-discovery TaskGroup (couplage naturel). Si M14+ ajoute d'autres consumers de daily_pnl (ex: dashboard PnL extended), envisager promotion à top-level `__main__`. Reportable.
8. **Shadow period tracking : DB query vs env var** : v1 = `SELECT MIN(cycle_at) FROM trader_scores WHERE scoring_version='v2'` par cycle (coût négligeable). Alternative env var `SCORING_V2_SHADOW_STARTED_AT` — moins précis (user peut oublier de mettre à jour). Décision v1 = DB query.
9. **Normalisation pool avec outliers extrêmes** : winsorisation p5-p95 mais si 1 wallet a Sortino=50 (outlier Fredi9999-like), il impacte p95. Évaluer `p10-p90` si trop sensible. Reportable post-backtest.
10. **Export fixture synthetic pour tests latents** : `tests/fixtures/scoring_v2/` contient ~10 wallets fixtures. Si tests agrégatifs (ex: `brier_aggregate`) deviennent fragiles, générer fixtures larges synthétiques. Reportable.
11. **Intégration `scripts/backtest_scoring_v2.py` dans CI** : v1 = script manuel hors CI (coût CI). Post-M12 : évaluer job GitHub Actions nightly sur set labelé pour détecter drift formule. Reportable.

---

## 14. ROADMAP des features reportées

À **ne pas oublier**. Ces hors scopes sont validés par la synthèse mais non inclus dans M12 par scope strict.

### 14.1 M12.1 (optionnel, ~1 semaine)

Déclencheur : backtest M12 montre que la formule uniforme pénalise trop les wallets jeunes.

- **Half-life temporel** : env var `SCORING_V2_HALFLIFE_DAYS=30` + weighting exponentiel dans `MetricsCollectorV2._compute_weighted_curve`. Gain : pool plus réactif aux wallets récemment devenus skilled.

### 14.2 M13 — Taker fees dynamiques (~2-3 jours, cf. synthèse §6.1 + §7.1)

- Nouveau `FeeRateClient` dans `executor/` + cache TTL 60 s.
- `Sizer.calculate()` soustrait `fee_rate × notional` de l'EV.

### 14.3 M14 — Phase 2 latence (parallélisation + WS user channel, ~1 mois, cf. M11 §14.1)

Déclencheur : post-M12 cutover, p95 latence > 2 s durable.

### 14.4 M15 — Apify Leaderboard Scraper integration (~3 jours, cf. synthèse §6.3)

Déclencheur : pool discovery M5 insuffisant (< 50 candidats qualifiés / cycle).

### 14.5 M16 — RTDS timing alpha refactor (~1 semaine, cf. §6.6 synthèse)

Prérequis : M14 mergé (WS user channel). Remplace approximation `/activity` feed par prix temps réel.

### 14.6 M17 — Auto-detection wash cluster (~2-4 semaines)

Graph clustering on-chain via Goldsky subgraph `positions`. Remplace liste ENV `WASH_CLUSTER_WALLETS` par détection automatique.

### 14.7 M18+ — MEV defense + Avellaneda-Stoikov market making (Gemini §6.1 + §6.3)

Très long terme, dépend validation M12-M16.

### 14.8 Features évaluées mais pas dans la roadmap active

- **Closing Line Value (CLV)** : nécessite orderbook snapshots time-series. Storage cost élevé, gain incertain (§5.3 synthèse).
- **Maker/taker ratio (liquidity provider)** : signal de sophistication, pas d'edge directionnel — à exclure du copy plutôt qu'à copier (§5.3 synthèse).
- **Fractional Kelly sizing** : feature engineering lourde, proxy d'edge bruité (§5.3).
- **Iceberg slicing detection** : algo complexe (pattern matching sur séquences d'ordres), gain non démontré (§5.3).

---

## 15. Commandes de vérification finale

```bash
# Qualité code
ruff check .
ruff format --check .
mypy src --strict
pytest --cov=src/polycopy/discovery --cov=src/polycopy/storage --cov=src/polycopy/dashboard \
  --cov-report=term-missing   # ≥ 80% sur modules touchés

# Non-régression M1..M11
pytest tests/unit tests/integration -v 2>&1 | diff - /tmp/m12_baseline.txt
# → delta attendu : +51 tests unit + 1 integration M12, 0 régression

# Migration
alembic upgrade head  # 0006 créé trader_daily_pnl
alembic downgrade 0005  # rollback propre
alembic upgrade head

# Smoke test shadow period (sur 2 cycles)
SCORING_VERSION=v1 \
SCORING_V2_SHADOW_DAYS=1 \
DISCOVERY_ENABLED=true \
DISCOVERY_INTERVAL_SECONDS=3600 \
TRADER_DAILY_PNL_ENABLED=true \
DASHBOARD_ENABLED=true \
python -m polycopy --verbose &
sleep 7200   # 2 cycles
# Vérifier :
# - trader_scores contient rows v1 ET v2
# - trader_events contient gate_rejected si applicable
# - target_traders.score (colonne overwrite) = score v1
# - Dashboard http://127.0.0.1:8787/traders/scoring rend table v1|v2|delta
kill %1 && wait

# Backtest (obligatoire avant cutover)
python scripts/backtest_scoring_v2.py \
  --labels-file assets/scoring_v2_labels.csv \
  --as-of 2026-02-15 \
  --window-days 180 \
  --output /tmp/backtest_v2_report.html
# Vérifier : brier_v2 < brier_v1 - 0.01 ; spearman ∈ [0.4, 0.7]

# Smoke rollback (flag v1 strict)
SCORING_VERSION=v1 \
SCORING_V2_SHADOW_DAYS=0 \
TRADER_DAILY_PNL_ENABLED=false \
python -m polycopy --verbose
# Vérifier : comportement M5 strict (pas de MetricsCollectorV2 instancié,
# pas d'insert trader_scores avec scoring_version=v2, dashboard /traders/scoring
# rend section disabled/empty)
```

---

## 16. Critères d'acceptation

- [ ] 6 facteurs purs implémentés dans `discovery/scoring/v2/factors/*.py`, chacun testable isolément, 0 I/O.
- [ ] Winsorisation p5-p95 + normalisation 0-1 dans `normalization.py`, idempotence testée.
- [ ] 6 gates durs implémentés dans `gates.py`, fail-fast, `AggregateGateResult` avec `failed_gate` nommé.
- [ ] `compute_score_v2(metrics, pool_context) -> ScoreV2Breakdown` agrège les 6 facteurs avec pondération `0.25/0.20/0.20/0.15/0.10/0.10`, somme = 1.0.
- [ ] Registry `SCORING_VERSIONS_REGISTRY["v2"]` exposé, v1 intact.
- [ ] `TraderMetricsV2` DTO étend `TraderMetrics` (composition ou héritage) avec 12 nouveaux champs. `ScoreV2Breakdown` expose 6 sous-scores bruts + 6 normalisés + final.
- [ ] `MetricsCollectorV2` fetch Sortino/Calmar depuis `trader_daily_pnl`, Brier depuis `/positions` résolues, timing_alpha depuis `/activity` (approximé), HHI catégories depuis Gamma.
- [ ] `TraderDailyPnlWriter` scheduler co-lancé dans `DiscoveryOrchestrator` TaskGroup si `TRADER_DAILY_PNL_ENABLED=true`, cadence 24h, dédup `(wallet_address, date)`.
- [ ] Migration Alembic 0006 strictement additive (crée `trader_daily_pnl`, zéro modif sur tables M5/M11). Downgrade propre.
- [ ] Shadow period dual-compute : `SCORING_VERSION=v1` + `SCORING_V2_SHADOW_DAYS>0` → v1 et v2 écrits en parallèle dans `trader_scores`, `DecisionEngine` reçoit score v1 seul.
- [ ] `gate_rejected` écrit dans `trader_events` avec reason explicite + event_metadata. Pas d'alerte Telegram.
- [ ] Dashboard `/traders/scoring` rend table v1|v2|delta_rank + métriques agrégées (Brier top-10, Spearman, delta_top10).
- [ ] Bouton "Validate v2 & flip" désactivé tant que (days < SCORING_V2_SHADOW_DAYS) OU (brier_v2 ≥ brier_v1 - 0.01) OU (`SCORING_V2_CUTOVER_READY=false`).
- [ ] Script `scripts/backtest_scoring_v2.py` produit rapport HTML + JSON avec `brier_top10_v1/v2`, `spearman`, `delta_top10`.
- [ ] `assets/scoring_v2_labels.csv` commité avec ≥ 50 wallets labelés smart_money/random.
- [ ] 7 feature flags (`SCORING_VERSION`, `SCORING_V2_SHADOW_DAYS`, `SCORING_V2_WINDOW_DAYS`, `SCORING_V2_COLD_START_MODE`, `WASH_CLUSTER_WALLETS`, `SCORING_V2_CUTOVER_READY`, `TRADER_DAILY_PNL_ENABLED`) tous avec défauts safe (v1 mode). Si flags off → comportement M5 strict (non-régression absolue).
- [ ] **Invariants M5 préservés** : pinned jamais demote-able, `MAX_ACTIVE_TRADERS` cap dur, `BLACKLISTED_WALLETS` vérifié 2×, lifecycle `shadow → active → paused → pinned` intact. Tests `test_decision_engine*.py` passent inchangés.
- [ ] **Invariants M10 préservés** : `EXECUTION_MODE` 3 modes, badge Telegram, processor `filter_noisy_endpoints`, exclusion `/logs` default, 4 garde-fous M3/M8. Tests `test_pnl_writer_m10_parity.py`, `test_telegram_badge.py`, `test_middleware_log_filter.py` passent.
- [ ] **Invariants M11 préservés** : 3 feature flags latency, `trade_latency_samples`, 6 stages, `/latency` dashboard. Tests M11 passent.
- [ ] **Versioning sacré** : aucun row `trader_scores` rewrite (append-only strict). Chaque row porte sa `scoring_version`.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src --strict` : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `src/polycopy/discovery/scoring/v2/`, `metrics_collector_v2.py`, `trader_daily_pnl_writer.py`, `TraderDailyPnlRepository`, `dashboard/queries.py` (partie scoring_comparison). Non-régression M1..M11 ≥ 80 %.
- [ ] Smoke test shadow period 2 cycles : dashboard affiche table v1|v2, `trader_events` contient `gate_rejected`, `target_traders.score` piloté par v1.
- [ ] Backtest exécutable + rapport JSON/HTML lisible. Brier v2 < v1 - 0.01 **recommandé** avant cutover (pas exigé pour merge spec).
- [ ] Doc updates §10 (CLAUDE.md) + README section scoring + `docs/architecture.md` M12 + `docs/setup.md` §19 shadow period + `docs/development/m12_cutover_playbook.md` + `.env.example` bloc §6.4 dans le **même** commit.
- [ ] Commit final unique : `feat(discovery,storage,dashboard): M12 scoring v2 (formule hybride + gates durs + shadow period)`.

---

## 17. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M12

Suis specs/M12-scoring-v2.md à la lettre. Invocation skill
/polymarket:polymarket REQUISE pour vérifier que Gamma /markets expose un
champ `category` ou `tags` exploitable (§8 étape 1).

Avant tout code, actions obligatoires :

1. Vérifier schéma Gamma categories :
   - Option A : skill /polymarket:polymarket interroge /markets
   - Option B : curl direct + capture fixture
     tests/fixtures/gamma_markets_categories_sample.json (30-50 marchés)
   STOP si Gamma ne retourne ni category ni tags exploitables → revoir
   facteur `specialization` avant de continuer (pondération réduite OU
   fallback regex heuristique documenté §13.7).

2. Refresh fixtures Data API M5 pour tests backtest (§8 étape 2) :
   - tests/fixtures/data_api_positions_<wallet>.json × 3 wallets
   - tests/fixtures/data_api_activity_<wallet>.json × 3 wallets
   - tests/fixtures/data_api_positions_resolved_for_brier_sample.json
     (≥ 30 positions résolues, requis Brier calcul)

3. Vérifier que M11 latency instrumentation tourne + tests M1..M11 passent :
   pytest tests/unit tests/integration -v 2>&1 | tee /tmp/m12_baseline.txt
   grep -E "passed|failed" /tmp/m12_baseline.txt | tail -5
   → delta attendu M12 : +51 tests unit + 1 integration, 0 régression

4. Vérifier invariants M5/M10/M11 présents :
   grep -E "SCORING_VERSION|pinned|MAX_ACTIVE_TRADERS|EXECUTION_MODE|filter_noisy_endpoints|trade_latency_samples" src/polycopy/ -r | wc -l
   → devrait retourner > 30 matches

Ensuite suis l'ordre §8 (21 étapes séquentielles sur ~2-3 semaines).

Contraintes non négociables :

- SCORING_VERSION=v1 par défaut. Aucun flip auto vers v2. Le cutover est
  100% manuel, conditionné à rapport backtest avec brier_v2 < brier_v1 - 0.01.
- Shadow period dual-compute : v1 et v2 écrivent tous deux dans trader_scores
  append-only, mais DecisionEngine ne reçoit QUE la version pilote (SCORING_VERSION).
- Formule v2 pondération fixe 0.25/0.20/0.20/0.15/0.10/0.10 — changer = bump
  vers v2.1 (jamais rewrite trader_scores historiques).
- 6 gates durs en v2, wallet rejeté = jamais scoré + row trader_events gate_rejected.
- Versioning sacré : trader_scores.scoring_version préservé append-only.
- MetricsCollectorV2 compose (pas modifier) MetricsCollector M5. Scoring v1
  fichier déplacé `scoring.py → scoring/v1.py` git mv, ZÉRO ligne modifiée.
- Invariants M5 (pinned, MAX_ACTIVE_TRADERS, BLACKLISTED_WALLETS, lifecycle
  shadow→active→paused→pinned) strictement préservés.
- Invariants M10 (EXECUTION_MODE 3 modes, badge Telegram, filter_noisy_endpoints,
  /logs exclusion default, 4 garde-fous M3/M8) strictement préservés.
- Invariants M11 (3 feature flags latency, trade_latency_samples, 6 stages,
  /latency dashboard) strictement préservés.
- Migration 0006 strictement additive (nouvelle table trader_daily_pnl seule).
- trader_daily_pnl ne contient aucun secret ni PII (wallet_address publique +
  equity_usdc). WASH_CLUSTER_WALLETS discipline identique BLACKLISTED_WALLETS.
- Aucune creds CLOB touchée — M12 read-only strict (Data API + Gamma + DB locale).
- Conventions CLAUDE.md (async, Pydantic v2 frozen, SQLAlchemy 2.0, structlog,
  docstrings FR / code EN, pas de print).
- mypy --strict propre, ruff propre, pytest ≥ 80% coverage sur modules
  nouveaux. Non-régression M1..M11 ≥ 80%.

Demande-moi confirmation AVANT tout patch sensible :
- config.py : promotion `scoring_version` à Literal["v1","v2"] + 7 nouveaux champs.
- git mv scoring.py → scoring/v1.py (refactor structurel).
- Migration Alembic 0006 (nouvelle table).
- storage/models.py : ajout TraderDailyPnl après TraderEvent.
- dashboard/routes.py + base.html : nouvelle route /traders/scoring + sidebar link.
- CLAUDE.md : 3 passages §10 (Sécurité, Conventions, Architecture).
- Nouveau script scripts/backtest_scoring_v2.py + assets/scoring_v2_labels.csv.

STOP et signale si :
- Gamma /markets ne fournit pas de category/tags exploitable (§8 étape 1).
- Fixture /activity révèle que le feed ne permet PAS de reconstruire
  mid_price(t) sur 10 min (timing_alpha indisponible) — besoin revoir §3.4.
- Overhead cycle > 10 min sur 100 wallets (risk §11.4) — besoin batching API
  avant merge.
- Backtest sur set labelé montre brier_v2 >= brier_v1 - 0.005 (presque
  indistinguable) — NE PAS merger le cutover, seulement la shadow.

Smoke test final obligatoire avant merge :
- 2 cycles discovery shadow : trader_scores contient rows v1 ET v2,
  trader_events contient gate_rejected, target_traders.score piloté par v1.
- Dashboard /traders/scoring rend table v1|v2|delta + agrégats.
- Backtest script exécutable (rapport HTML/JSON produit).
- Migration 0006 apply + downgrade OK.
- Rollback : SCORING_V2_SHADOW_DAYS=0 + TRADER_DAILY_PNL_ENABLED=false →
  comportement M5 strict confirmé.

Commit unique : feat(discovery,storage,dashboard): M12 scoring v2 (formule
hybride + gates durs + shadow period)
```
