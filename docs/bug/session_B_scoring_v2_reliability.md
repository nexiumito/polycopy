# Session B — Scoring v2 reliability

**Priorité** : 🔥 #2 (débloque le cutover v2 qui est le critère business principal)
**Charge estimée** : L (2-3 jours, fort axe investigation)
**Branche suggérée** : `fix/scoring-v2-stability`

---

## Objectif business

Le scoring v2 est aujourd'hui **inutilisable pour piloter des décisions** parce que :

1. **Variance cycle-to-cycle énorme** (±30 %) avec spikes discontinus : ex sur
   `0x08c95f70…a2ef`, passage de 0.265 à 0.480 puis 0.358 entre 3 cycles consécutifs.
2. **Valeur fallback suspecte** : `0x63d43bbb…a2f1` affiche `0.45` exactement
   sur 80+ cycles, probablement quand certains facteurs retournent défaut/None
   (timing_alpha=0.5 placeholder + 2-3 autres = moyenne pondérée ≈ 0.45).
3. **Asymétrie couverture v1/v2** : pool v1 ~50 wallets, pool v2 ~13 au jour 5 de
   shadow period. Les gates durs (days_active ≥ 30, trade_count ≥ 50) bloquent
   beaucoup de wallets légitimes en début de shadow.
4. **Spearman rank faussé sur /scoring** : malgré le commit `1ba8ae3` qui devait
   calculer sur l'intersection v1∩v2, le dashboard affiche encore des ranks
   v1 du pool entier (33, 45, 52…) — le fix n'est pas complet.

**Blocker business** : on ne peut pas faire le cutover `SCORING_VERSION=v2` tant
que le score n'est pas stable ET interprétable. La shadow period 14 j devait
servir à valider — elle révèle surtout des bugs dans l'implémentation.

## Items

### B1 — Investigation variance + spikes v2

**Tâche d'investigation d'abord, pas d'implémentation immédiate.**

Pour chaque facteur (`risk_adjusted`, `calibration`, `timing_alpha`,
`specialization`, `consistency`, `discipline`), logger/exporter la valeur brute
+ normalisée **cycle par cycle** pour 3-5 wallets. Identifier lequel des 6
provoque les spikes.

Pistes :
- `risk_adjusted` (Sortino/Calmar) : sur 2-5 jours d'equity curve, le Sortino
  est du bruit. Hypothèse probable.
- `calibration` : Brier-skill relative au baseline pool — si le pool bouge,
  le baseline change et donc le score change sans que le wallet ait rien fait.
- `specialization` HHI : stable sauf si un wallet change sa distribution de
  catégories (rare).
- `timing_alpha=0.5` placeholder (décision D3 M12) : stable, donc pas suspect.

**Livrable** : un script `scripts/debug_scoring_v2_variance.py` qui dump
breakdown par cycle et un rapport `docs/development/scoring_v2_variance_report.md`
pointant la cause racine.

### B2 — Fix valeur verrouillée `0.45` (`0x63d43bbb…a2f1`)

Quand un wallet a certains facteurs à valeur défaut (timing_alpha=0.5 placeholder
+ peut-être 2 autres neutres à 0.5), la moyenne pondérée tombe pile sur 0.45
parce que `0.20 × 0.5 + 0.20 × 0.5 + 0.25 × X + …`. Si `X` est aussi stable
(Sortino calculé sur 0 jours d'equity → valeur défaut), on obtient une constante.

**Symptôme** : un wallet "réel mais pas encore mesurable" affiche un score
artificiellement correct (0.45) alors qu'il devrait afficher "score_unavailable"
ou ne pas être scoré du tout.

**Fix** : quand un facteur ne peut pas être calculé (N insuffisant), **exclure
le wallet du pool scoré** au lieu de lui donner une valeur neutre. Équivalent à
un gate dur implicite "facteur inobservable → skip".

### B3 — Stabiliser winsorisation p5-p95 sur petit N

Aujourd'hui la winsorisation recalcule p5/p95 **à chaque cycle** sur le pool
courant. Si le pool passe de 10 à 11 wallets (nouveau shadow découvert), les
p5/p95 se décalent et **tous les scores normalisés bougent**.

**Options** :
- **Option A (simple)** : figer p5/p95 sur un pool de référence "stable" (ex : top
  50 Polymarket) recalibré 1×/jour. Scores plus stables cycle-to-cycle.
- **Option B (complexe)** : moving average exponentielle des bornes p5/p95.
- **Option C (retenue probable)** : p5/p95 recalculé uniquement sur pool ≥ 30
  wallets (sinon utiliser un fallback hardcodé). Contraint l'usage v2 au moment
  où on a assez de data.

### B4 — Débloquer couverture v2 (13 → ~50 wallets)

Hypothèses sur pourquoi v2 ne couvre que 13/50 :
- Gate `days_active ≥ 30` trop strict sur shadow frais (2 j actuels). À vérifier
  dans la DB : combien de wallets sont rejetés `gate_rejected` avec raison
  `days_active_insufficient` ?
- Gate `trade_count_90d ≥ 50` trop strict sur wallets récents.
- `TraderDailyPnl` scheduler pas suffisamment ancien → pas assez de points equity
  curve → facteurs non calculables → wallet skipped.

**Action** : requête SQL sur `trader_events` filtrée sur `event_type=gate_rejected`
pour quantifier les raisons. Puis décider si on **assouplit les gates** v2 (risqué)
ou si on **accepte la couverture restreinte** (la shadow period de 14 j finit par
couvrir tous les wallets éligibles).

### B5 — Fix Spearman rank `/scoring` (compléter commit 1ba8ae3)

Dashboard affiche encore les ranks v1 du pool entier (33, 45, 52) — donc le
Δ rank montré n'est pas sur l'intersection. Vérifier dans
[src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py)
si le commit 1ba8ae3 a bien re-classé les deux listes sur l'intersection avant
de soustraire.

Ajouter un test de non-régression
`test_spearman_rank_computed_on_intersection_only`.

### B6 — Dashboard `/scoring` : view top-10 côte-à-côte + stabilité

Au-delà du tableau existant, ajouter :
- **Top-10 v1** vs **Top-10 v2** en deux colonnes visuelles (tags "newcomer",
  "fell off", "stable").
- **Stability score** par wallet : `std(score v2 sur N derniers cycles)`. Un
  score stable (std < 0.02) = signal fiable. Un score volatile (std > 0.10) =
  rouge (flagged).

Permet à l'user de **voir d'un coup d'œil** si v2 converge ou non.

### B7 — Documenter la variance acceptable

Après B1-B3, définir un **seuil de std acceptable** (ex 0.05) pour flagger les
wallets dont v2 n'est pas fiable. Si > X % des wallets dépassent le seuil,
**ne pas autoriser le cutover** (bloquer via garde-fou dans le DecisionEngine :
si `SCORING_VERSION=v2` ET `unstable_ratio > 0.3` → crash boot avec erreur
explicite).

## Hypothèses à valider

- **H1** : la variance vient majoritairement de `risk_adjusted` (Sortino sur petit N).
- **H2** : la stabilité viendra d'un pool de référence fixe (option A) plutôt
  qu'une logique adaptative complexe.
- **H3** : assouplir les gates durs v2 risque de réintroduire les faux-positifs
  que M12 gates étaient censés éliminer. À peser.

## Livrables

- Script `scripts/debug_scoring_v2_variance.py`
- Rapport `docs/development/scoring_v2_variance_report.md` (cause racine + fix
  retenu)
- Spec additionnelle si besoin `docs/specs/M14bis_scoring_v2_stability_spec.md`
- ~8-12 tests unit + 2-3 tests integration
- Mise à jour CLAUDE.md §Scoring v2 sur les nouveaux garde-fous
- Mise à jour dashboard `/scoring` (top-10 side-by-side + stability indicator)

## Out of scope

- Pas de changement de la formule v2 ni des pondérations (`0.25/0.20/0.20/0.15/0.10/0.10`
  restent figées).
- Pas d'implémentation du "vrai" `timing_alpha` (D3 report v2.1 décision M12, pas
  ce milestone).
- Pas de refacto de `TraderDailyPnl` scheduler (déjà stable).

## Success criteria

1. Variance cycle-to-cycle v2 < 5 % (std relatif sur N derniers cycles) pour au
   moins 80 % des wallets scorés.
2. Couverture v2 ≥ 50 % des wallets ACTIVE + SHADOW (vs ~25 % aujourd'hui).
3. Aucun wallet ne doit afficher une valeur exactement `0.45` (sauf coïncidence
   numérique vraie).
4. Dashboard `/scoring` Spearman affiche des ranks cohérents (tous dans 1..N où
   N = taille intersection).
