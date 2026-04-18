# M12 — Carnet de bord implémentation

Living doc tenu pendant l'implémentation du module M12 (scoring v2). Zone d'idées, problèmes rencontrés, décisions fines, reportables v2.1+.

Référence unique de conception : [M10_synthesis_reference.md](./M10_synthesis_reference.md).
Spec active : [../../specs/M12-scoring-v2.md](../../specs/M12-scoring-v2.md).

## Pré-code — données capturées

- **Gamma `/markets?include_tag=true`** ✅ — retourne `tags: [{id, label, slug, forceShow, ...}]` (~5.6 tags/market, 0 markets sans tag). Fixture : [../../tests/fixtures/gamma_markets_categories_sample.json](../../tests/fixtures/gamma_markets_categories_sample.json) (50 markets, 2026-04-18).
- **Baseline tests M1..M11** : 822 passed, 10 deselected, 0 failed (114 s). Cible M12 : +51 tests unit + 1 integration, 0 régression → **≥ 873 passed**.
- **Invariants grep** : 90 matches total (SCORING_VERSION=6, pinned=60, MAX_ACTIVE_TRADERS=4, EXECUTION_MODE=9, filter_noisy_endpoints=5, trade_latency_samples=6).

## Décisions validées par le user 2026-04-18

- **D1 main_category** (§3.5) : option A — set hardcodé `_TOP_LEVEL_POLYMARKET_CATEGORIES` + override env var `SCORING_V2_CATEGORY_OVERRIDES`.
- **D2 shadow tracking** (§13.8) : option A — query DB `MIN(cycle_at)` par cycle avec cache 1 min.
- **D3 timing_alpha fallback** (§3.4) : option A — `0.5` (neutre) si < 50 trades sur fenêtre 10 min. Reportable v2.1.

## Constantes top-level Polymarket (D1)

Source : observation fixture + polymarket.com/markets nav.

```python
_TOP_LEVEL_POLYMARKET_CATEGORIES: frozenset[str] = frozenset({
    "Politics", "Sports", "Crypto", "Economics", "Economy",
    "Geopolitics", "Tech", "Culture", "Pop Culture", "Climate",
    "Health", "Science", "Business", "Entertainment",
})
```

Pattern resolution : premier `tag.label` qui match `_TOP_LEVEL_POLYMARKET_CATEGORIES` (case-sensitive) = catégorie principale. `"other"` fallback. `"Economy"` et `"Economics"` mappés à la même catégorie "Economy" (normalisation).

## Idées à ne pas oublier (reportable v2.1+)

### Formule / facteurs

- [ ] **Half-life exponentiel temporel** (§14.1) — env `SCORING_V2_HALFLIFE_DAYS=30`. Déclencheur : backtest montre pénalisation excessive wallets jeunes.
- [ ] **Auto-detection wash cluster** (§14.6 M17+) — graph clustering Goldsky subgraph. Actuellement : liste ENV manuelle.
- [ ] **Timing_alpha via RTDS** (§14.5 M16+) — prix historiques officiels Polymarket RTDS remplacent approximation `/activity` VWAP. Prérequis M14 (WS user channel).
- [ ] **Brier calibration sur résolution réelle** — notre approximation `outcome = 1 if cash_pnl > 0` est fragile sur neg_risk multi-outcome. Migrer vers Gamma `/markets` `resolvedOutcome` quand fixture confirme le champ.
- [ ] **Ponderation catégorie HHI weighted by total_market_volume** — actuellement égalité entre toutes les catégories. Catégorie avec volume global élevé (Politics pendant une élection) pourrait peser différemment.

### Infra

- [ ] **Compaction `trader_daily_pnl`** (§14 note) — rollup quarterly post-5 ans de data pour réduire taille SQLite. Déclencheur : DB > 500 MB.
- [ ] **Batching inserts scoring v2** — actuellement 1 insert par wallet × 2 (v1 + v2) en shadow = pic DB. Si cycle > 5 min sur 100 wallets, batcher via queue.
- [ ] **CI nightly backtest** — job GitHub Actions hebdomadaire qui rejoue `scripts/backtest_scoring_v2.py` sur set labelé pour détecter drift formule.
- [ ] **Alertes Telegram sur cluster `gate_rejected`** — si > 10 rejections sur 1 cycle, alerte INFO (signal de poisoning/wash cluster à investiguer).

### Dashboard

- [ ] **Drill-down breakdown v2** — cliquer un wallet dans `/traders/scoring` affiche `ScoreV2Breakdown` complet (6 sous-scores bruts + normalisés + Brier baseline). Actuellement résumé seul.
- [ ] **Export CSV** `/traders/scoring?format=csv` pour analyse externe.
- [ ] **Histogramme distribution sous-scores pool** — voir si pool dégénéré (tous au p50).

## Problèmes rencontrés + résolutions

_(ajouter au fil de l'implémentation)_

### 2026-04-18 — Exploration Gamma tags

- **Problème** : fixture initiale `gamma_top_markets_sample.json` ne contient pas `tags` (requête sans `include_tag=true`).
- **Résolution** : ajouter `?include_tag=true` au client `GammaApiClient.list_top_markets`. **Diff** minimal — constante `_GAMMA_LIST_MARKETS_PARAMS` ou kwarg sur la méthode. À appliquer Tranche 3 dans `MetricsCollectorV2`.
- **Impact tests** : les tests `test_gamma_client_top_markets.py` existants doivent continuer à passer (tags optionnels). Vérifier.

### 2026-04-18 — Format `scoring_version` string

- **Contradiction synthèse §1.4 `"1"/"2"` vs code existant `"v1"`**. Résolu dans spec §"État actuel vs livrable" → on utilise `Literal["v1", "v2"]`.

### 2026-04-18 — Normalisation "idempotente" imprécise

- **Problème** : la spec §3.8 annonçait "idempotence" (`apply(apply(x)) == apply(x)`). Faux au sens strict quand le pool original est hors `[0, 1]` — la 2ᵉ passe prend la valeur normalisée et la clippe à 0.0 ou 0.5 selon p5.
- **Résolution** : reformuler contrat réel = déterminisme + bornes `[0, 1]` + monotonicité. Le test `test_apply_pool_normalization_idempotent` §9.3.A est remplacé par `test_apply_pool_normalization_monotonic` + `test_apply_pool_normalization_bounded_in_unit_interval`.

### 2026-04-18 — Tranche 1 livrée

- 17 tests neufs (3 alembic + 9 repo + 5 writer), baseline 822 → 839 passed.
- Patches sensibles appliqués : migration 0006, storage/models.py, storage/dtos.py, storage/repositories.py, git mv scoring.py → scoring/v1.py, config.py 2 env vars minima.
- Patches sensibles restants : config.py 5+ env vars scoring v2 (Tranche 3), dashboard/routes.py + base.html (Tranche 4), CLAUDE.md (Tranche 5), scripts/backtest_scoring_v2.py + assets/scoring_v2_labels.csv (Tranche 4).

### 2026-04-18 — Hook déplacement m12_notes.md

- Un hook project a relocalisé `docs/development/m12_notes.md` → `docs/logbook_module/m12_notes.md`. Chemin mis à jour dans références futures.

### 2026-04-19 — Bug M5 historique : `BLACKLISTED_WALLETS` JSON pas lowercase

- **Découvert en implémentant** `_parse_wash_cluster_wallets` en copiant `_parse_blacklisted_wallets` M5 — le validator JSON path (`if stripped.startswith("[")`) retourne `json.loads(stripped)` **sans** lowercase. CSV path et list path font bien lowercase.
- **Impact M5** : si user écrit `BLACKLISTED_WALLETS='["0xABC"]'` le wallet reste en majuscules ; la comparaison dans `DecisionEngine` fait `.lower()` des deux côtés donc match OK, mais la comparaison `blacklist_hits` dans `candidate_pool` aussi. En pratique, M5 fonctionne sans problème.
- **Décision** : laisser M5 intact (risque trop grand de casser des tests historiques). `_parse_wash_cluster_wallets` M12 fait bien le lowercase JSON — cohérent avec le rôle "sécurité absolue" du gate. Documenté explicitement dans le validator.
- **Reportable v2.1** : aligner `_parse_blacklisted_wallets` avec le fix M12 (trivial : 3 lignes). À faire dans un PR séparé si bug remonte.

### 2026-04-18 — Décision pragmatique `timing_alpha` v1

- **Problème** : implémenter timing_alpha "vrai" (D3 option A) nécessite de reconstruire `mid_price(t)` via `/activity?market=<cid>` pour chaque pair (wallet, market). ~100 wallets × ~10 markets distincts = **1000 fetchs API supplémentaires par cycle** — prohibitif, risque de blocage Data API rate limit (~100 req/min).
- **Options étudiées** :
  - A ⭐ (retenue) : `timing_alpha_weighted = 0.5` (neutre) pour tous les wallets v1 M12. Pool normalization compressera tout autour de 0.5 → facteur effectivement désactivé avec pondération 0.20 "gaspillée" mais safe. Cohérent avec décision D3 fallback.
  - B : vrai calcul avec cache LRU intra-cycle + limit max 200 markets fetched. Risque toujours le rate limit.
  - C : intégrer RTDS Polymarket (§14.5) comme source prix — grosse refonte, reportable.
- **Retenue A** : timing_alpha_weighted=0.5 en v1 M12. Documenté explicitement dans MetricsCollectorV2 + §14.5 reportable. Impact pondération effective = **0.80 discriminante** (0.25 risk + 0.20 cal + 0.15 spec + 0.10 cons + 0.10 disc) au lieu de 1.0 nominale.
- **Re-évaluation v2.1** : backtest révèle si timing_alpha aurait changé le top-10 significativement. Si oui + RTDS disponible, M12.1.

## TODOs techniques locaux

- [ ] Ne pas oublier d'ajouter `Date` à l'import SQLAlchemy dans `storage/models.py` (nouveau type pour `TraderDailyPnl.date`).
- [ ] `TraderDailyPnlRepository.insert_if_new` doit retourner `bool` pour signaler dédup (pattern cohérent `DetectedTradeRepository.insert_if_new` M1).
- [ ] Scheduler `TraderDailyPnlWriter` : aligner UTC date (éviter drift timezone — utiliser `datetime.now(tz=UTC).date()`).
- [ ] `compute_score_v2` wrapper dans `registry` : si `_CURRENT_POOL_CONTEXT.get()` est None → retourner 0.0 + log warn (cas test unitaire).
- [ ] Pour `specialization` : dédup tags dans `_compute_hhi_categories` (un market peut avoir plusieurs tags mappant la même top-level, compter 1×).
