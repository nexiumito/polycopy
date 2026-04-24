# MB — Anti-toxic lifecycle + internal PnL feedback

**Priorité** : 🔥 P1 (ferme la boucle anti-toxic, débloque MF)
**Charge estimée** : M (4-5 jours)
**Branche suggérée** : `feat/anti-toxic-lifecycle`
**Prérequis** : **MA shipped** (scoring stable v2.1)
**Bloque** : **MF** (v2.2-DISCRIMINATING nécessite 30j d'internal_pnl_score collectée)

---

## 1. Objectif business

Empêcher qu'un trader perdant reste ACTIVE indéfiniment (cas concret : `0x21ffd2b7…0d71`, 19% WR, -$0.55 PnL, score 0.66, user a dû le blacklister manuellement le 2026-04-24). Ajouter **un signal de performance interne** (realized_pnl observé depuis qu'on copie le wallet) dans le scoring, **passer le lifecycle en ranking-based** (plus de threshold absolu), et introduire des **gates durs** anti-arbitrage-bot qui ferment les grosses lacunes de conception actuelles. Démarre la collecte des 30 jours d'internal_pnl data requise pour MF.

## 2. Contexte & problème observé

### Observations runtime 2026-04-24

- Trader `0x21ffd2b7…0d71` (/performance dashboard) : **ACTIVE depuis 5 jours**, 19% WR (10W/42L), cumulative PnL observé -$0.55. Score v1 = 0.66 (au-dessus du `SCORING_DEMOTION_THRESHOLD`). Aucun mécanisme automatique ne l'a demote. User l'a blacklisté manuellement via `.env` + restart.
- Traders ACTIVE dormants : `0x63d4…a2f1` (score 0.83) **dernier trade il y a 15h**. Pompe un slot `MAX_ACTIVE_TRADERS` pour rien.
- Discovery panel /home : **0 promotions / 0 demotions sur 24h** avec 43 shadow + 7 active. Zéro rotation alors que les scores devraient bouger.

### Findings référencés

- **[F02] 🟢 3/3** (synthèse §2.2) : Internal PnL feedback = gap #1. **Claude §3.2 + item 8** : "single largest epistemic gap in v2. A Sortino-weighted multi-factor composite is a noisy proxy for 'posterior probability this wallet will be net-positive on next 10 trades'. The weights were chosen by introspection rather than calibration against realized polycopy PnL." **Gemini §commercial** : aucun bot commercial surveyé n'intègre realized PnL. **Perplexity E2 via Convexly** : "Edge Score V3b is a composite fitted on 8,656 wallets using calibration, sizing discipline, concentration risk **fitted against signed log PnL**". Convexly = seul exemple académique-grade avec realized PnL feedback.
- **[F06] 🟢 3/3** (synthèse §2.2) : rank-based > threshold-based. **Claude C11** : "M5_bis uses competitive promotion (rank) but the static demote threshold (mediocre 0.60 stays active) is an absolute threshold. These live on different metric spaces: rank is ordinal, 0.60 is cardinal. When pool size shifts, absolute scores drift without rank change, so a wallet can become rank-worst but stay above 0.60 (your observed pathology)".
- **[F14] 🔵 Claude + $40M évidence** (synthèse §2.4 unique Claude) : Arbitrage bots filter. **Claude §9 item 5 + A10** : "$40M/an extraits par arbitrageurs YES+NO via Bregman projections ([Dev Genius](https://blog.devgenius.io/just-found-the-math-that-guarantees-profit-on-polymarket-and-why-retail-traders-are-just-providing-6163b4c431a2)). Ces wallets passent tous les gates actuels (positive PnL, high trade count, active, non-zombie, probably non-wash) but have **no signal for a copy-trader**". Détection : `|YES_net - NO_net| / gross < 0.10` sur une fenêtre 90j.
- **[F11] ⚠️ 2/3** (synthèse §2.3 ⚠️ divergence) : Gemini "supprimer gates 30j/50 trades", Claude "relaxer avec probation fractional-Kelly". **Arbitrage polycopy** : probation fractional-Kelly 0.25× pour wallets avec 10-50 trades (Gemini §"Cold-Start Policies").
- **[C11] Claude + C10** : Sortino sentinel zombies (déjà partiellement fixé dans MA.3) mais ici on ajoute le **ranking fix** qui est orthogonal.
- **Audit [H-007]** : "State machine eviction utilise les scores **stale** (DB) au lieu des scores refreshed". Bug connu M5_bis, fix en parallèle dans MB.
- **Session A brouillon** (`docs/bug/session_A_anti_toxic_trader_lifecycle.md`) : items A1-A4 intégrés ici, A5-A6 migrent en MH (alerts UX) ou restent.

### Convexly Edge Score V3b — référence académique

**Perplexity A5** documente : "Convexly... composite fitted on 8,656 wallets using calibration (Brier score), sizing discipline, and concentration risk against signed log PnL, and the team claims to publish methodology, coefficients, and underlying data in a 10k-wallet calibration study". Référence [convexly.app/truth-leaderboard](https://www.convexly.app/truth-leaderboard) + [HN 47765107](https://news.ycombinator.com/item?id=47765107).

**C'est notre modèle direct** pour la formule polycopy-specific : fitter une partie du scoring contre le PnL **réalisé par polycopy** (pas le PnL historique du wallet source).

## 3. Scope (items détaillés)

### MB.1 — Collecteur `internal_pnl_score(w)` dans MetricsCollectorV2

- **Location** : [src/polycopy/discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py) — ajouter méthode `_compute_internal_pnl_score(wallet_address: str) -> float | None`
- **Ce qu'il faut faire** :
  - Nouvelle query SQL : `SELECT realized_pnl FROM my_positions WHERE closed_at IS NOT NULL AND source_wallet_address = :wallet AND (simulated = :mode OR closed_at > :since_30d)` — filtre selon `execution_mode` (cf. MD pour le fix simulated flag).
  - Calcul : `signed_pnl_30d = SUM(realized_pnl)`, `count = COUNT(*)`.
  - Formule : `internal_pnl_score = sigmoid(signed_pnl_30d / 10.0)` où `10.0` = scaling factor (équivalent à "+$10 PnL sur 30j = score 0.73"). Le sigmoid cappe à [0, 1] smoothly.
  - **Cold-start** : si `count < SCORING_INTERNAL_MIN_POSITIONS=10` → retourner `None` (wallet pas encore scoré sur ce facteur, traité par `aggregator.py` comme facteur inobservable).
  - **Attention** : utiliser `my_positions.source_wallet_address` qui doit tracer le wallet source copié (pas juste le wallet de polycopy). Si absent du schéma existant, ajout migration dans MD ou ici (préférer dans MD qui porte déjà la migration 0008).
- **Tests requis** :
  - `test_internal_pnl_score_sigmoid_bounds`
  - `test_internal_pnl_score_returns_none_under_min_positions`
  - `test_internal_pnl_score_dry_run_vs_live_mode_isolation` (simulated flag filter)
  - `test_internal_pnl_score_30d_window_correct`
- **Sources deep-search** : Claude §4.2 v2.2-DISCRIMINATING `internal_pnl_score(w) = sigmoid(realized_copy_pnl_30d(w) / $10)` + Perplexity E2 Convexly methodology.
- **Charge item** : 1 jour

### MB.2 — Nouveau facteur `internal_pnl` dans scoring v2.1.1

- **Location** : [src/polycopy/discovery/scoring/v2/aggregator.py](../../src/polycopy/discovery/scoring/v2/aggregator.py) + [src/polycopy/discovery/scoring/v2/factors/internal_pnl.py](../../src/polycopy/discovery/scoring/v2/factors/internal_pnl.py) (à créer)
- **Ce qu'il faut faire** :
  - Créer nouveau facteur `internal_pnl` qui utilise `MB.1` collector.
  - **Pondération** : ajouter 0.20 de poids sur `internal_pnl`, ré-équilibrer les 5 autres (renormaliser à 0.80/5 × old_weight). Formule v2.1.1 :
    ```
    score_v2.1.1 = 0.25·rank(internal_pnl)
                 + 0.25·rank(risk_adjusted)        [était 0.3125 post-MA.1]
                 + 0.20·rank(calibration)          [était 0.25]
                 + 0.15·rank(specialization)       [était 0.1875, HHI direct post-MA.5]
                 + 0.075·rank(consistency)         [était 0.125]
                 + 0.075·rank(discipline)          [était 0.125]
    ```
    **Alternative (à trancher en spec M15)** : internal_pnl weight 0.30, reste ramené à 0.70 equal. Plus agressif mais aligne Claude §4.2.
  - Cold-start : si `internal_pnl_score is None` (wallet <10 copied positions), traiter comme facteur "absent" → score du wallet calculé **sans ce facteur** et les 5 autres renormalisent à 1.0 localement. Évite biais 0.5 neutre.
  - Bump `SCORING_VERSION` default → `"v2.1.1"` (incrément mineur de v2.1). **v2.1 reste dispo** dans le registry pour comparaison shadow.
- **Tests requis** :
  - `test_aggregator_v2.1.1_weights_sum_to_one`
  - `test_aggregator_cold_start_renormalizes_without_internal_pnl`
  - `test_aggregator_v2.1.1_responds_to_internal_pnl_change`
- **Sources deep-search** : Claude §4.2 v2.2-DISCRIMINATING + synthèse §2.5 formules candidates.
- **Charge item** : 1 jour

### MB.3 — Ranking-based activation (remplace threshold absolu)

- **Location** : [src/polycopy/discovery/decision_engine.py:290-339](../../src/polycopy/discovery/decision_engine.py#L290-L339) `_decide_active`
- **Ce qu'il faut faire** :
  - Remplacer la logique `if score < SCORING_DEMOTION_THRESHOLD: increment_low_score(); if new_count >= HYSTERESIS_CYCLES: demote` par :
    ```python
    # Rank-based : si wallet sort du top-N par score v2.1, demote après hystérésis
    all_active_scores = await self._target_repo.list_scores_for_status("active")
    sorted_scores = sorted(all_active_scores, key=lambda s: -s.score)
    wallet_rank = next(i for i, s in enumerate(sorted_scores) if s.wallet == wallet)
    max_active = settings.max_active_traders
    if wallet_rank >= max_active:  # Hors top-N
        new_count = increment_low_score()
        if new_count >= HYSTERESIS_CYCLES:
            transition_status("shadow")
    ```
  - **Alternative** (simpler) : vérifier si le wallet est dans les `MAX_ACTIVE_TRADERS` meilleurs scores du pool `{active ∪ shadow}` combinés. Si non → demote.
  - **Préserver** `SCORING_DEMOTION_THRESHOLD` comme **garde-fou de sécurité** : si `score < 0.30` absolu, force demote même si dans le top-N (wallet clairement cassé).
  - **Préserver** `SCORING_DEMOTION_HYSTERESIS_CYCLES=3` (pas de flip-flop rapide).
  - **Attention pinned** : `pinned` wallets n'entrent pas dans le ranking (déjà safeguard M5, conserver strict).
- **Tests requis** :
  - `test_decide_active_ranking_based_demotes_out_of_top_n`
  - `test_decide_active_hysteresis_preserved`
  - `test_decide_active_absolute_threshold_safeguard_still_fires` (score<0.30)
  - `test_decide_active_pinned_never_demoted`
  - `test_decide_active_ranking_stable_with_pool_change` (régression test H-007 partiel)
- **Sources deep-search** : Claude C11 + §9 item 4 + F06.
- **Charge item** : 1 jour

### MB.4 — Fix EvictionScheduler scores stale (audit H-007)

- **Location** : [src/polycopy/discovery/eviction/state_machine.py:79-166](../../src/polycopy/discovery/eviction/state_machine.py#L79-L166) vs [src/polycopy/discovery/eviction/scheduler.py:206-216](../../src/polycopy/discovery/eviction/scheduler.py#L206-L216)
- **Ce qu'il faut faire** :
  - Audit H-007 : `classify_sell_only_transitions` évalue T6 abort sur `self_score = scores.get(wallet, sell_only.score or 0.0)` (fresh) mais `_delta_vs_worst` consomme `active_non_pinned` avec `t.score` de la DB (stale — cycle précédent).
  - Refresher les scores des snapshots active **avant** `classify_sell_only_transitions`. Passer un dict `fresh_scores: dict[wallet, float]` qui couvre active + sell_only + shadow, et consommer fresh partout dans la state machine.
  - **Attention** : la signature de `classify_sell_only_transitions(inputs, tracker, blacklist)` doit accepter les fresh scores. Mettre à jour `scheduler.py` caller pour passer les fresh.
- **Tests requis** :
  - `test_classify_sell_only_uses_fresh_scores_for_worst_active`
  - `test_eviction_no_stale_score_dependency_regression` (fixture : pool avec scores changés entre cycles)
- **Sources** : Audit H-007 + cross-ref MA.7 (margin recalibration dépend de scores frais).
- **Charge item** : 0.5 jour

### MB.5 — Recalibrer `EVICTION_SCORE_MARGIN` basé sur std observée

- **Location** : [src/polycopy/config.py:800-828](../../src/polycopy/config.py#L800-L828) (cross-ref MA.7)
- **Ce qu'il faut faire** :
  - **Cette action complète MA.7** : MA.7 a baissé le default à 0.10 (= 1σ estimé). Ici, on ajoute un helper qui **observe la std empirique** sur les 7 derniers jours de scores post-v2.1/v2.1.1, et log une recommandation au boot.
  - `_log_empirical_margin_recommendation(settings, session_factory)` : calcule `std(active_scores) over last 7d`, log `structlog.info("eviction_margin_empirical_recommendation", current=settings.eviction_score_margin, empirical_1_sigma=std, recommended_range=[std*0.8, std*1.2])`.
  - Ne **pas auto-ajuster** — décision humaine (print to CLI + log, user tweak `.env`).
  - Hypothèse H-EMP-2 : variance réduite post-rank-transform → std attendue ~0.06. Margin recommandé = 0.06-0.08.
- **Tests requis** :
  - `test_log_empirical_margin_recommendation_with_fixture_pool`
  - `test_eviction_score_margin_validator_bounds_preserved` (déjà MA.7)
- **Sources** : Claude §3.1 + Q2 synthèse.
- **Charge item** : 0.5 jour

### MB.6 — Probation fractional-Kelly pour wallets 10-50 trades

- **Location** : [src/polycopy/discovery/decision_engine.py](../../src/polycopy/discovery/decision_engine.py) + [src/polycopy/strategy/pipeline.py PositionSizer](../../src/polycopy/strategy/pipeline.py)
- **Ce qu'il faut faire** :
  - Nouveau status DB ou flag `trader.is_probation: bool` (ou inférable à la volée depuis `trade_count_90d`).
  - **DecisionEngine** : un wallet avec `10 ≤ trade_count_90d < 50` qui passe les **autres** gates (cash_pnl positive, days_active≥7 relaxé temporairement, not blacklisted, not in wash cluster) est **PROMOTABLE** en ACTIVE avec flag `is_probation=True`.
  - **PositionSizer** : si `source_trader.is_probation == True`, multiplier `my_size` par **0.25** (quarter-Kelly). Ce wallet est copié à 1/4 du sizing normal jusqu'à passer le gate full (`trade_count_90d >= 50`).
  - Auto-release probation : quand `trade_count_90d >= 50` sur un wallet, flag flip → sizing normal (ou wallet repassa en shadow si scoring v2.1.1 ne le place plus dans le top-N).
  - **Attention** : probation ne bypasse PAS les gates critiques (`cash_pnl_90d > 0`, `not blacklisted`, `not in wash cluster`). Juste relaxe `trade_count ≥ 50` et `days_active ≥ 30` (baisse à `trade_count ≥ 10` et `days_active ≥ 7`).
- **Tests requis** :
  - `test_probation_wallet_sized_quarter_kelly`
  - `test_probation_released_at_50_trades`
  - `test_probation_does_not_bypass_blacklist_or_wash_gates`
- **Sources deep-search** : Gemini §"Cold-Start Policies" "fractional Kelly probation 0.1x-0.25x" + Claude §9 item 11.
- **Charge item** : 1 jour

### MB.7 — Arbitrage bot filter gate (nouveau gate dur)

- **Location** : [src/polycopy/discovery/scoring/v2/gates.py](../../src/polycopy/discovery/scoring/v2/gates.py) (ajout) + [src/polycopy/discovery/metrics_collector_v2.py](../../src/polycopy/discovery/metrics_collector_v2.py) (nouveau computer)
- **Ce qu'il faut faire** :
  - Nouveau gate `not_arbitrage_bot` calculé par `_compute_net_exposure_ratio(wallet)` :
    ```python
    # Pour chaque (condition_id) où le wallet a pris position :
    yes_net = sum(size for pos in positions where asset_id = yes_token)
    no_net = sum(size for pos in positions where asset_id = no_token)
    gross = abs(yes_net) + abs(no_net)
    net_exposure_ratio = abs(yes_net - no_net) / gross if gross > 0 else 1.0

    # Moyenner sur tous les condition_ids du wallet :
    avg_net_exposure = mean(net_exposure_ratio for each condition_id)

    # Gate : rejeter si avg < 0.10
    return avg_net_exposure >= 0.10
    ```
  - Ajouter dans `GATES_V2` tuple avant scoring dispatcher. Rejet écrit `trader_events.event_type="gate_rejected"` avec `reason_code="arbitrage_bot_pattern"`.
  - **Fenêtre** : 90 jours glissants (cohérent avec autres gates).
  - **Dépend de Data API `/positions`** — si positions non disponibles, fallback sur inférence depuis `detected_trades`.
- **Tests requis** :
  - `test_compute_net_exposure_ratio_arbitrage_bot_under_threshold`
  - `test_compute_net_exposure_ratio_directional_trader_above_threshold`
  - `test_arbitrage_gate_writes_trader_event`
- **Sources deep-search** : Claude §9 item 5 + A10 + $40M/an arbitrage évidence ([Dev Genius](https://blog.devgenius.io/just-found-the-math-that-guarantees-profit-on-polymarket-and-why-retail-traders-are-just-providing-6163b4c431a2)).
- **Charge item** : 1 jour

### MB.8 — Auto-blacklist seuil PnL cumulé + alertes Telegram

- **Location** : [src/polycopy/discovery/decision_engine.py](../../src/polycopy/discovery/decision_engine.py) + nouveau `src/polycopy/monitoring/templates/trader_auto_blacklisted.md.j2`
- **Ce qu'il faut faire** :
  - Nouveau garde-fou dans `DecisionEngine._decide_active` **après** les autres checks :
    ```python
    if observed_cumulative_pnl < AUTO_BLACKLIST_PNL_THRESHOLD_USD:  # default -$5
        or (observed_win_rate < 0.25 and observed_position_count >= 30):
        # Auto-blacklist
        self._target_repo.transition_status(wallet, "blacklisted")
        push_alert("trader_auto_blacklisted_toxic", {...})
    ```
  - Nouveau setting `AUTO_BLACKLIST_PNL_THRESHOLD_USD: float = -5.0` (strict), ajustable via env.
  - Nouveau template Telegram `trader_auto_blacklisted.md.j2` avec : wallet + raison (PnL<seuil OU WR<25%+30 trades) + stats observées + lien dashboard.
  - **Seuil conservative** : −$5 sur un capital virtuel $1000 = −0.5% — suffisant pour signaler mais pas trop bas (attention nos fluctuations normales sont $0.50-$2 par wallet).
  - **Attention idempotence** : un wallet déjà `blacklisted` ne déclenche pas 2× l'alerte (cooldown_key = `f"auto_blacklist_{wallet}"`).
- **Tests requis** :
  - `test_auto_blacklist_fires_on_pnl_threshold`
  - `test_auto_blacklist_fires_on_win_rate_floor_with_min_positions`
  - `test_auto_blacklist_idempotent_no_duplicate_alert`
  - `test_telegram_template_auto_blacklisted_renders_safely` (MarkdownV2 escape)
- **Sources** : Session A item A4 (brouillon) + finding converge avec Convexly "hard caps aggressive risk".
- **Charge item** : 1 jour

## 4. Architecture / décisions clefs

- **D1** : `internal_pnl_score` utilise sigmoid (smooth cap) plutôt que linéaire bornée. Justification : évite saturation à ±∞ et préserve gradient au voisinage du seuil.
- **D2** : scaling factor sigmoid = `/10` (soit $10 de PnL ≈ score 0.73). Justification : sur capital virtuel $1000, $10 PnL sur 30j = 1% monthly = signal raisonnable. Ajustable via setting `SCORING_INTERNAL_PNL_SCALE_USD`.
- **D3** : cold-start `internal_pnl_score = None` plutôt que 0.5 neutre. Justification : 0.5 biaiserait tous les nouveaux wallets vers la médiane. `None` → facteur ignoré localement, renormalize les 5 autres. Plus honnête statistiquement.
- **D4** : ranking + garde-fou threshold absolu à 0.30. Justification : ranking-based capture la dynamique pool, threshold capte les cas absolument cassés. Ceinture + bretelle.
- **D5** : probation 0.25× quarter-Kelly, pas 0.1×. Justification : Gemini §"Kelly" cite catastrophic drawdowns above 0.25×, donc 0.25× est la limite supérieure sûre. 0.1× serait trop conservateur (trades trop petits pour mesurer internal_pnl_score rapidement).
- **D6** : arbitrage bot gate est **dur** (rejet total), pas un penalty factor. Justification : ces wallets n'ont pas de directional signal **by design** — les copier est toujours négatif, pas juste sub-optimal.
- **D7** : bump version `"v2.1.1"` (mineur) plutôt que `"v2.2"`. Justification : on ajoute **1 facteur** (internal_pnl), on ne fait pas le gros upgrade Sirolly+Mitts-Ofir qui est MF. Preserve la sémantique de versioning.

## 5. Invariants sécurité

- **Triple garde-fou M3 + 4ᵉ M8** : MB n'active rien côté live. Le nouveau facteur `internal_pnl` lit depuis `my_positions.realized_pnl` qui est écrit par M8 DryRunResolutionWatcher en dry-run et par M3 fills en live. Pas de nouvelle surface de signature.
- **Zéro secret loggé** : le nouveau template `trader_auto_blacklisted.md.j2` ne contient que wallet public + stats numériques. Grep automatisé `test_no_secret_leak_in_auto_blacklist_template`.
- **Blacklist double-check préservé** : `DecisionEngine.decide` vérifie déjà `wallet in BLACKLISTED_WALLETS`. MB.8 ajoute `auto_blacklist` qui écrit en DB → `reconcile_blacklist` au cycle suivant traite normalement.
- **Append-only scoring versions** : v2.1.1 est un nouveau row type dans `trader_scores`, jamais UPDATE des rows v2 ou v2.1 historiques.
- **Auto-blacklist réversible** : user peut retirer un wallet auto-blacklisté via `.env` modif + `reconcile_blacklist`. Trace dans `trader_events` pour audit.

## 6. Hypothèses empiriques à valider AVANT ship

- **H-EMP-3** (synthèse §8) : corrélation `internal_pnl_score` vs score v2.1 actuel — ces deux métriques mesurent-elles la même chose ou sont-elles complémentaires ? **Méthode** : calculer Spearman entre `internal_pnl_score` et `score v2.1` pour tous wallets avec ≥10 closed. **Seuil go** : `0.1 < ρ < 0.7` (trop bas = un des deux est bruit, trop haut = redondance). Si hors bornes, réviser D2 scaling.
- **H-EMP-11** (synthèse §8) : combien de wallets dans notre pool passeraient le `arbitrage_bot` gate ? **Méthode** : compute `net_exposure_ratio` sur les 50 shadows + 8 actives. **Seuil go** : ≥90% passent (sinon gate trop strict et rejette des directional traders légitimes).
- **H-EMP-13** : sur notre pool de 50, quel % a `cumulative_pnl_90d > 0` à l'instant T ? **Méthode** : query Data API `/value?user=<addr>` pour chaque wallet. **Seuil informatif** (pas go/no-go) : si <15%, notre gate hard `cash_pnl_90d > 0` élimine 85% du pool — vérifier que probation MB.6 capture les edges.

**Script de validation** : étendre `scripts/validate_ma_hypotheses.py` (MA) avec les checks H-EMP-3, H-EMP-11, H-EMP-13.

## 7. Out of scope

- **Sirolly wash cluster continuous score** : migre en **MF**.
- **Mitts-Ofir composite informed_score** : migre en **MF**.
- **CLV (Closing Line Value)** : migre en **MG**.
- **Kelly proxy conviction_sigma** comme factor : migre en **MG** (ici on utilise 0.25 Kelly uniquement pour le sizing probation, pas comme signal scoring).
- **Thompson Sampling Trend-Aware** : hors scope MB, rank-based suffit pour v2.1.1 (Claude §7.3 "simple approximation").
- **Dashboard /performance colonne `internal_pnl_score`** : migre en **MH** (UX sur la data que MB produit).
- **Convergence signal (cross-wallet agreement)** : migre en **MF** (fait sens avec informed_score composite).
- **Fenêtre rolling 180j + exp decay** : hors scope, v3 future.
- **Latency tolerance factor** (avg_holding_time) : migre en **MG**.
- **Anti-copy bait detection** (Claude §9 item 9) : hors scope v2.1.1, spec future qui consomme l'`internal_pnl_score` que MB produit.

## 8. Success criteria

1. **Tests ciblés verts** : ~20 nouveaux tests unit + 3 integration.
2. **Hypothèses empiriques validées** : H-EMP-3 et H-EMP-11 dans les seuils go avant ship.
3. **Rotation effective** : post-ship MB sur 14j, observer **≥5 promotions** et **≥5 demotions** (vs 0/0 observé 2026-04-24).
4. **Auto-blacklist fonctionnel** : un wallet test injecté avec realized_pnl < −$5 sur 7j déclenche l'alerte Telegram + bascule status `blacklisted` (test integration E2E).
5. **Probation actif** : ≥2 wallets passent probation → full status sur 30j d'observation post-ship, avec sizing effectif 0.25× mesurable dans `my_orders.size` vs `raw_size`.
6. **Arbitrage bot gate** : rejette **≥0** wallets dans notre pool actuel (= aucun détecté, gate silencieux OK) ; ≥1 wallet dans pool synthétique test (fixture arbitrage bot).
7. **Internal PnL factor** : 30j post-ship, ≥50% des wallets ACTIVE ont `internal_pnl_score != None` (collecte suffisante).
8. **Pas de flip-flop** : aucun wallet ne fait >3 transitions `active ↔ shadow` sur 14j (hystérésis fonctionnelle).

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MB.1 | — | A (A1 core) | F02 (3/3), Claude §4.2, Perplexity E2 Convexly | #7 |
| MB.2 | — | A (new) | F02, Claude §4.2 v2.2 weights 0.30 | #7 |
| MB.3 | — | A (A2 core) | F06 (3/3), Claude C11 + §9 item 4 | #9 |
| MB.4 | [H-007] | — | — (audit seul) | audit mapping |
| MB.5 | — | A (A3 extend) | F06 partial, Claude §3.1 σ_score ≈ 0.092 | #4 |
| MB.6 | — | A (new) | F11 ⚠️, Gemini §Cold-Start + Claude §9 item 11 | #10 |
| MB.7 | — | A (new) | F14, Claude §9 item 5 + A10 | #5 |
| MB.8 | — | A (A4 + A5) | F02 extension, Claude §9 item 9 implicite | session A only |

## 10. Prompt de génération de spec

````markdown
# Contexte

Lis `docs/next/MB.md` en entier. C'est le brief actionnable du module MB
(Anti-toxic trader lifecycle + internal PnL feedback). Il référence audit,
session A, deep-search synthèse §2.2 F02/F06 + §2.3 F11 + §2.4 F14, et
Convexly Edge Score V3b methodology.

# Tâche

Produire `docs/specs/M15-anti-toxic-lifecycle.md` suivant strictement le format
des specs M1..M14 existantes (§ numérotées : TL;DR, Motivation, Scope, User
stories, Architecture, Algorithmes, DTOs, Settings, Invariants sécurité, Test
plan, Impact existant, Migration, Commandes vérif, Hors scope, Notes
implémentation, Prompt implémentation, Commit message proposé).

Numéro : M15 (après M14 shippé en MA).

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Conventions + §Sécurité (Discovery M5 invariants, auto-blacklist
  discipline réversible, append-only trader_scores)
- `docs/specs/M13_dry_run_observability_spec.md` comme **template de forme**
- `docs/specs/M5-trader-scoring.md` + `docs/specs/M5_bis_competitive_eviction_spec.md`
  comme **référence de contenu** lifecycle existant (DecisionEngine, EvictionScheduler)
- `docs/next/MA.md` déjà shippé = scoring v2.1 stable (MB construit dessus)
- Sections deep-search citées dans MB.md §2 : synthèse §2.2 F02+F06, §2.3 F11,
  §2.4 F14 ; Claude §3.2, §4.2 v2.2, §9 items 4-5-11 ; Gemini §"Cold-Start
  Policies" + §"Promotion/Demotion"

# Contraintes

- **Lecture seule** sur `src/`, `tests/`, docs sources
- **Écriture uniquement** `docs/specs/M15-anti-toxic-lifecycle.md`
- **Ne pas inventer** : chaque item spec doit tracer vers un item MB.§3 (MB.1-MB.8)
- **Longueur cible** : 1200-1500 lignes (cohérent avec M13 + M14)
- **Migration Alembic** : à évaluer. Si `my_positions.source_wallet_address` ou
  `target_traders.is_probation` absent du schéma, migration 0009. Sinon non.
- **Hypothèses empiriques H-EMP-3 + H-EMP-11 + H-EMP-13** : inclure dans §Notes
  implémentation + §Commandes de vérification (`scripts/validate_mb_hypotheses.py`).

# Livrable

- Le fichier `docs/specs/M15-anti-toxic-lifecycle.md` complet
- Un ping final ≤ 10 lignes : tests estimés, charge cumulée, ordre commits
  (recommandé : MB.1 → MB.7 → MB.2 → MB.3 → MB.4 → MB.5 → MB.6 → MB.8).
````

## 11. Notes d'implémentation

### Piège : `source_wallet_address` dans `my_positions`

Le schéma actuel de `my_positions` ([storage/models.py](../../src/polycopy/storage/models.py)) peut ne **pas** avoir directement la colonne `source_wallet_address` (wallet qui a déclenché la copie). Vérifier :
- Si absent : ajouter dans migration 0009 (ou 0008 si MD précède MB).
- Si présent : confirmer qu'il est bien peuplé par le path M8 `_persist_realistic_simulated` et par le path live `_persist_sent_order`.
- Inférable via `detected_trades.target_wallet_address` + `my_orders.source_trade_id` en dernier recours, mais query coûteuse.

### Piège : ordering des cycles discovery + eviction + internal_pnl collector

1. Cycle discovery écrit `trader_scores` avec scores v2.1.1 (incluant internal_pnl_score OU None).
2. `DecisionEngine._decide_active` utilise les fresh scores pour ranking.
3. `EvictionScheduler` est hook post-decision dans `DiscoveryOrchestrator` — doit utiliser **les mêmes fresh scores** (MB.4 fix H-007).
4. Internal PnL collector tourne **avant** le scoring (peuple `internal_pnl_score` dans le breakdown), pas en parallèle.

### Piège : `observed_win_rate < 0.25` gate auto-blacklist

Calcul : `wins = count(realized_pnl > 0)`, `losses = count(realized_pnl < 0)`. Les break-even (`realized_pnl == 0`) sont **exclus du dénominateur** (synthèse F-L010). Si un wallet a 30 positions toutes break-even, `observed_win_rate = None` — ne pas déclencher auto-blacklist (neutre pas mauvais).

### Piège : auto-blacklist flood protection

Au boot, si plusieurs wallets ACTIVE sont déjà sous le seuil PnL (fix d'un bug pre-existant), **on ne veut pas spammer 10 alertes Telegram**. Idempotence via cooldown + déjà-blacklisté-check. Voir MB.8 tests.

### Piège : probation vs sized 0.25 dans PositionSizer

Le `PositionSizer` ([strategy/pipeline.py:177-210](../../src/polycopy/strategy/pipeline.py#L177-L210)) ne connaît pas directement le source_trader. Besoin d'une query ou d'enrichir le `DetectedTrade` DTO avec `is_source_probation: bool`. Préférer enrichir le DTO dans le watcher `WalletPoller` pour éviter N+1 query dans le pipeline.

### Références literature

- **Convexly Edge Score V3b** : [truth-leaderboard](https://www.convexly.app/truth-leaderboard) + [HN discussion 47765107](https://news.ycombinator.com/item?id=47765107). **Modèle direct** pour fit internal_pnl contre composite multi-factor.
- **Kelly 1956 / Thorp 1969** : [Wikipedia](https://en.wikipedia.org/wiki/Kelly_criterion) + [Yoder 2023](https://nickyoder.com/kelly-criterion/). Base théorique quarter-Kelly.
- **Frigo 2024 Kalshi AI bot** : [GitHub](https://github.com/ryanfrigo/kalshi-ai-trading-bot). "betting above a quarter-Kelly (0.25x) fraction induces catastrophic drawdowns exceeding 80% of capital during standard variance cycles".
- **$40M/an arbitrageurs** : [Dev Genius summary](https://blog.devgenius.io/just-found-the-math-that-guarantees-profit-on-polymarket-and-why-retail-traders-are-just-providing-6163b4c431a2). Arg net_exposure_ratio gate.

### Questions ouvertes pertinentes à MB

- **Q3** (synthèse §11) : corrélation Brier/PnL négative Convexly sur top-100 — validable post-MB.1 + 30j data. Calcul Spearman `brier_skill_v2.1.1` vs `internal_pnl_score` sur nos ACTIVE wallets.
- **Q10** : combien d'arbitrageurs dans notre pool ? MB.7 répond directement (log des rejets).
