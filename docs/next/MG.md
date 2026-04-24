# MG — Additional scoring factors (CLV + Kelly proxy + liquidity-adjusted ROI + latency tolerance)

**Priorité** : 🟠 P2 (bonus scoring, enrichit v2.2 capstone)
**Charge estimée** : M (3-4 jours)
**Branche suggérée** : `feat/scoring-factors-clv-kelly`
**Prérequis** : **MA shipped** (v2.1 rank transform + factor infrastructure)
**Bloque** : — (optionnel pour MF, MF peut ship avec versions baseline de ces facteurs)

---

## 1. Objectif business

Implémenter 4 facteurs **académiquement validés mais manquants** de notre formule :

1. **CLV (Closing Line Value)** — métrique primaire de vrai skill en prediction markets (Gemini prioritaire §"Alternative Empirical Factors") ; domine WR et ROI.
2. **Kelly proxy `conviction_sigma`** — détecte les wallets avec sizing Kelly-like (sophistication signal).
3. **Liquidity-adjusted ROI (Kyle's λ framework)** — pénalise les wallets dont le PnL vient de mover des books illiquides, pas d'info.
4. **Latency tolerance** (`avg_holding_time`) — identifie wallets macro-swing (insensibles à notre 300ms lag) vs micro-scalpers (incompatibles avec notre latency floor).

Ces facteurs enrichissent **v2.2-DISCRIMINATING** (MF) — les sous-facteurs `conviction_sigma` et `liquidity_adjusted_roi` de `informed_score` sont ici en **version full** (MF ship avec baseline simple si MG arrive après).

## 2. Contexte & problème observé

### Findings référencés

- **[F08] 🟡 2/3** (synthèse §2.3) : Ajouter CLV. **Gemini §"Alternative Empirical Factors"** : "CLV measures the mathematical difference between the price at which a trader executed their position and the final price immediately prior to market resolution. Positive CLV is the **ultimate, variance-free indicator of informed trading**. If a trader buys 'Yes' at $0.20 and the market closes at $0.80, that trader possessed extreme edge. **Even if the 20% 'No' outcome ultimately occurs**, the CLV metric recognizes the brilliant execution". **Claude §9 item 1** : "information leadership (entry time vs market midpoint time-series)" — formulation proche, compute from /trades + Gamma price history.
- **[F09] 🟡 2/3** (synthèse §2.3) : Kelly fraction alignment (`conviction_sigma`). **Gemini §"Fallacy of Full Kelly"** : "empirical backtesting on binary prediction markets proves that betting above a quarter-Kelly (0.25x) fraction induces catastrophic drawdowns exceeding 80% of capital" + §"Alternative Factors" : "sophisticated models measure how closely a trader's capital allocation correlates to a quarter-Kelly (0.25x) distribution relative to the market edge". **Claude §9 item 2** : "std-dev of position size normalized by wallet bankroll. High-variance sizers are more likely using Kelly-like reasoning. Kelly 1956, Thorp 1969".
- **[F10] 🔵 Claude unique** (synthèse §2.4) : Liquidity-adjusted ROI via Kyle's λ. **Claude §9 item 3** : "PnL / sum of (bid-ask × filled size). Penalizes wallets whose returns come from moving illiquid books rather than information. Closest academic anchor: Kyle's λ framework applied to Polymarket (arXiv Anatomy of Polymarket 2603.03136)".
- **[F38] 🔵 Claude §5.1** : Latency tolerance factor. **Synthèse §5.1** : "Pour identifier un wallet micro-scalper est trompeur — son alpha dépend de capturer 1¢ spreads que notre 300ms latency floor rend impossible. Préférer wallets macro-swing : horizon trade-to-trade >30s. Feature `latency_tolerance_score(w) = mean(holding_time_until_exit)` — wallets qui tiennent longtemps sont insensibles à nos 300ms de lag."

### Sessions originales mappées

Aucune session A-E. **Naît directement du deep-search** — items #17 (CLV), #18 (Kelly), #19 (liquidity-adjusted), #38 (latency tolerance) de la roadmap §9 synthèse.

### Pourquoi P2 et pas P1

Ces facteurs **enrichissent** mais **ne bloquent pas** le test business :
- v2.1 (MA) + v2.1.1 (MB) sont déjà shippables/viables sans MG.
- v2.2 (MF) peut ship avec les versions **baseline** de conviction_sigma et liquidity_adjusted_roi intégrées dans `informed_score` composite.
- MG est le **polish académique** : une v2.2 avec MG shipped est **plus discriminante** qu'une v2.2 sans.

**Timing optimal** : MG ship **entre MA et MF** (après v2.1 stable, avant v2.2 capstone). Les facteurs MG enrichissent alors directement la formule v2.2 composite sans baseline compromis.

## 3. Scope (items détaillés)

### MG.1 — CLV (Closing Line Value) computation

- **Location** : nouveau fichier `src/polycopy/discovery/scoring/v2/factors/clv.py` + extend `src/polycopy/discovery/metrics_collector_v2.py`
- **Ce qu'il faut faire** :
  - Pour chaque position résolue du wallet sur les 90 derniers jours :
    ```
    entry_price = price at position open (from my_orders / my_positions.avg_price for polycopy; from /trades Data API for source wallets)
    closing_price = price at resolution time (fetch via Gamma /markets historical prices or WSS last_trade_price)
    clv = closing_price - entry_price  (positive = informed entry)
    ```
  - **Attention** : pour un BUY YES à $0.20 qui close à $0.80, CLV = +$0.60 (excellent).
  - Pour un BUY NO à $0.60 qui close à $0.70 (prob YES), CLV_NO = 0.60 - 0.70 = -$0.10 (perte sur side acheté).
  - **Normalisation side-aware** : reframe en CLV_on_side_bought. Pour BUY NO à $0.60, le side acheté est NO donc `prob_NO_close = 1 - 0.70 = 0.30`, CLV_NO = 0.30 - 0.60 = -$0.30 (perte).
  - **CLV moyen wallet** : `avg_clv(wallet) = mean(CLV per position) over last 90d`. **Pondération par size** : `clv_weighted = sum(clv_i * size_i) / sum(size_i)`.
  - Rank normalize dans aggregator (cohérent MA rank transform).
  - **Décision D1** : CLV weight **0.10** dans v2.2, **redistribution depuis `calibration_proper` qui passe de 0.15 à 0.10**. Justification : CLV capture "informed entry timing" qui chevauche partiellement calibration. La réduction calibration équilibre.
  - Alternative (à valider shadow) : CLV weight **0.15**, redistribution plus large. Spec M19 tranche.
  - **Data dependency** : nécessite `closing_price` par position. Pour source wallets : via Data API `/markets/<condition_id>` qui renvoie `resolvedOutcome` et prices historiques. Pour polycopy trades : via `MyPosition` + Gamma fetch. **Coût API** : 1 Gamma call par résolution → OK avec cache (M11 adaptive TTL).
  - Cache `closing_price` par `condition_id` résolu : TTL infini (markets résolus immuables — déjà géré par Gamma adaptive cache M11).
- **Tests requis** :
  - `test_clv_positive_on_informed_buy_before_resolution`
  - `test_clv_negative_on_late_buy_after_news`
  - `test_clv_side_aware_buy_no`
  - `test_clv_weighted_by_size`
  - `test_clv_uses_cache_for_resolved_markets`
- **Sources deep-search** : F08, Gemini §"Alternative Empirical Factors" + §5 réponse "replace timing_alpha with rolling 90-day CLV", Claude §9 item 1.
- **Charge item** : 1.5 jour

### MG.2 — Kelly proxy (`conviction_sigma`) — version full pour v2.2

- **Location** : nouveau fichier `src/polycopy/discovery/scoring/v2/factors/conviction_sigma.py` + extend `src/polycopy/discovery/metrics_collector_v2.py`
- **Ce qu'il faut faire** :
  - Pour un wallet, compute :
    ```
    trade_sizes = [t.usdc_size for t in last_90d_trades]
    wallet_bankroll = estimated via Data API /value?user=<addr>  # total USDC valuation
    normalized_sizes = [s / wallet_bankroll for s in trade_sizes]
    conviction_sigma = stdev(normalized_sizes) / mean(normalized_sizes)  # coefficient of variation
    ```
  - **High conviction_sigma** = sizing varie avec edge → Kelly-like (signal sophistication).
  - **Low conviction_sigma (~0)** = sizing plat → retail-type (ignoring edge probability).
  - **Normalisation** : `conviction_sigma ∈ [0, ~2]` typiquement ; rank normalize sur pool.
  - **Note MF.2 baseline** : MF implémente une version **simplifiée** `std(trade_size) / mean(trade_size)` sans normalisation bankroll. MG.2 est la **version full** qui normalise par `wallet_bankroll`. Si MG ship **avant** MF, MF.2 utilise directement MG.2. Si MG ship **après** MF, MG.2 remplace l'implémentation baseline.
  - **Décision D2** : si Data API `/value` échoue (rate limit, wallet introuvable), **fallback** sur `mean(trade_sizes)` comme proxy bankroll. Pas idéal mais robuste.
- **Tests requis** :
  - `test_conviction_sigma_high_for_kelly_trader`
  - `test_conviction_sigma_low_for_flat_sizer`
  - `test_conviction_sigma_fallback_on_value_api_error`
  - `test_conviction_sigma_normalized_by_bankroll`
- **Sources deep-search** : F09, Gemini §"Fallacy of Full Kelly" + §"Alternative Factors", Claude §9 item 2.
- **Charge item** : 1 jour

### MG.3 — Liquidity-adjusted ROI (Kyle's λ framework) — version full

- **Location** : nouveau fichier `src/polycopy/discovery/scoring/v2/factors/liquidity_adjusted_roi.py` + extend `src/polycopy/discovery/metrics_collector_v2.py`
- **Ce qu'il faut faire** :
  - Pour chaque trade du wallet sur 90j :
    ```
    bid_ask_spread_at_entry = (best_ask - best_bid) at entry time
    depth_consumed = my_size (shares filled at that level)
    liquidity_cost = bid_ask_spread * depth_consumed  # dollar cost of moving the book
    ```
  - Agrégation wallet :
    ```
    total_pnl_90d = sum(realized_pnl per closed position)
    total_liquidity_consumed = sum(liquidity_cost per trade)
    liquidity_adjusted_roi(wallet) = total_pnl_90d / total_liquidity_consumed
    ```
  - **High ratio** = wallet génère du PnL **sans** mover beaucoup les books → vraie info (arg Kyle's λ).
  - **Low ratio** = PnL vient de l'impact sur book illiquide → pas de vrai signal.
  - **Data dependency** : `bid_ask_spread_at_entry` historique. Difficile à reconstruire pour les trades anciens. **Décision D3** : utiliser une **approximation** — `bid_ask_spread_at_entry ≈ current_spread` (stable pour markets mûrs). Peut être recalibré post-ME (WSS market channel capture les spreads temps réel, on peut indexer).
  - **Version simplifiée baseline (déjà dans MF.2 informed composite)** : `liquidity_adjusted_roi = total_pnl / sum(size * price)`. Moins précise mais shippable.
  - **Version full MG.3** : utilise vrai `bid_ask_spread` via historique WSS + Gamma snapshot.
  - **Décision D4** : commencer par version simplifiée, évaluer gain full version post-shadow.
- **Tests requis** :
  - `test_liquidity_adjusted_roi_high_for_informed_trader`
  - `test_liquidity_adjusted_roi_low_for_book_mover`
  - `test_liquidity_adjusted_roi_simplified_fallback`
- **Sources deep-search** : F10, Claude §9 item 3, arXiv Anatomy of Polymarket 2603.03136 (Kyle's λ reference).
- **Charge item** : 1 jour

### MG.4 — Latency tolerance factor (`avg_holding_time`)

- **Location** : nouveau fichier `src/polycopy/discovery/scoring/v2/factors/latency_tolerance.py` + extend `src/polycopy/discovery/metrics_collector_v2.py`
- **Ce qu'il faut faire** :
  - Pour chaque position closed du wallet :
    ```
    holding_time = closed_at - opened_at  (timedelta)
    ```
  - Wallet aggregation : `avg_holding_time(wallet) = median(holding_times) over last 90d closed positions`.
  - **Log-normalize** : `latency_tolerance_score = log(1 + avg_holding_time_minutes)` puis rank normalize.
  - **High score** = wallet macro-swing (hold hours/days) → **compatible polycopy latency floor**.
  - **Low score** = wallet micro-scalper (hold minutes/seconds) → **incompatible polycopy** (on ne peut pas reproduire leur edge à 300ms floor).
  - **Weight v2.2** : **0.05** (modeste, défensif). Justification : évite de copier du scalping inefficace mais ne domine pas.
  - **Usage** : intégré directement dans `score_v2.2` comme 6ᵉ terme, ou comme **multiplicateur sur internal_pnl_score** (wallet scalper dont internal_pnl est mauvais chez nous = down-weighted).
  - **Décision D5** : approche multiplicateur sur internal_pnl_score :
    ```
    polycopy_tolerance_multiplier = 0.5 + 0.5 * rank(latency_tolerance_score(wallet))
    effective_internal_pnl = internal_pnl_score(wallet) * polycopy_tolerance_multiplier
    ```
    Range [0.5, 1.0] : un wallet scalper voit son internal_pnl contribution réduite de moitié, un macro-swing conserve 100%.
- **Tests requis** :
  - `test_latency_tolerance_high_for_macro_swing`
  - `test_latency_tolerance_low_for_scalper`
  - `test_polycopy_multiplier_reduces_scalper_internal_pnl_contribution`
- **Sources** : F38 Claude §5.1 + synthèse §5.1.
- **Charge item** : 0.5 jour

### MG.5 — Intégration dans v2.2 aggregator (optionnel selon timing)

- **Location** : [src/polycopy/discovery/scoring/v2/aggregator.py](../../src/polycopy/discovery/scoring/v2/aggregator.py)
- **Ce qu'il faut faire** :
  - Si MG ship **avant** MF :
    - Ajouter CLV comme facteur dédié dans v2.2 (weight 0.10, ajuster calibration à 0.10).
    - `conviction_sigma` et `liquidity_adjusted_roi` utilisés par `informed_score` composite MF.2 avec versions full MG.2 + MG.3.
    - `latency_tolerance` applique multiplicateur sur `internal_pnl_score` dans l'aggregator.
  - Si MG ship **après** MF :
    - Bump version à `"v2.2.1"` (incrément mineur).
    - Remplace les versions baseline dans `informed_score` par les full MG.2 + MG.3.
    - Ajoute CLV comme facteur.
    - Applique `latency_tolerance` multiplier.
  - **Décision D6** : équivalent en résultat final, timing affecte juste la nomenclature.
  - Docstring clarification : quelle version composite (baseline vs full) pour traçabilité.
- **Tests requis** :
  - `test_v2_2_1_uses_full_conviction_sigma_and_liquidity_adjusted`
  - `test_v2_2_1_applies_latency_tolerance_multiplier`
  - `test_v2_2_with_mg_factors_shadow_parallel_with_baseline`
- **Charge item** : 0.5 jour

## 4. Architecture / décisions clefs

- **D1** : CLV weight 0.10, réduction calibration à 0.10. Justification : CLV et calibration partagent du signal (both measure "informed entry"). Réduction calibration équilibre le total.
- **D2** : fallback `conviction_sigma` sur `mean(trade_sizes)` si `/value` API échoue. Justification : robuste à rate limits, approximation acceptable.
- **D3** : `bid_ask_spread_at_entry ≈ current_spread` pour version simplifiée liquidity-adjusted. Justification : reconstruire spreads historiques est coûteux. Approximation OK pour markets mûrs.
- **D4** : shipper version simplifiée liquidity-adjusted d'abord, évaluer gain version full post-shadow. Justification : évite over-engineering avant validation empirique.
- **D5** : `latency_tolerance` comme multiplicateur sur `internal_pnl_score`, pas facteur standalone. Justification : l'effet est conditionnel (seul `internal_pnl` est sensible à notre latence), applique directement là où ça compte.
- **D6** : v2.2.1 si MG après MF, sinon MG intégré directement dans v2.2 spec. Timing affecte juste la version string.

## 5. Invariants sécurité

- **Triple garde-fou M3 + 4ᵉ M8** : intact. MG est pur scoring discovery, zéro touch executor.
- **Append-only scoring versions** : v2.2.1 nouvelle row dans `trader_scores`, pas de rewrite v2.2.
- **Zéro secret loggé** : les events structlog (`clv_computed`, `conviction_sigma_computed`, `latency_tolerance_applied`) n'incluent que numeric + wallet publics.
- **Data API `/value` read-only** : pas de creds CLOB L2 consommées.
- **Gamma `/markets` historical prices** : public, cohérent M2+M11.

## 6. Hypothèses empiriques à valider AVANT ship

- **H-EMP-5** (synthèse §8 Q3) : corrélation Brier/PnL négative Convexly tient-elle sur notre pool ? **Méthode** : post-MA+MB shipped, calcul Spearman `calibration_proper` vs `internal_pnl_score` sur nos ACTIVE avec ≥30 positions. **Seuil informatif** : si ρ < 0.2, calibration proche inutile → réduire davantage poids calibration dans v2.2. MG.1 CLV devient plus important relative.
- **Q12** (implicite) : CLV ajoute-t-il de l'info orthogonale à `calibration_proper` + `internal_pnl_score` ? Post-MG shadow, Spearman CLV vs autres facteurs. Si ρ < 0.5 avec tous, CLV distinct signal (good). Si ρ > 0.8, redondant.
- **Q-MG-2** (nouvelle) : `avg_holding_time` distribution sur notre pool — combien de wallets sont scalpers (hold < 30 min) vs macro-swingers (hold > 2h) ? Informe le multiplicateur D5.

## 7. Out of scope

- **News-alpha factor** (entry vs public info timestamp) : Gemini §"Alternative Factors", nécessite news API externe. Hors scope MG, spec future.
- **Cross-market correlation / pair-trade patterns** (Claude §9 item 5) : détection hedge patterns. Hors scope MG, spec future.
- **Resolution-path awareness** (Claude §9 item 6) : wallets évitant oracle-dispute markets. Hors scope MG, spec future.
- **Maker/taker ratio as factor** : Claude §9 item 4 recommande comme **gate** (exclure makers pures) plutôt que factor scoring. Intégré dans MF.7 (gate) plutôt qu'ici.
- **Full Kyle's λ framework** (historical order book reconstruction) : version simplifiée D3 suffit pour v2.2. Full version = spec future si besoin.
- **Kelly sizing recommandation pour polycopy** (quelle fraction utiliser pour nos FOK orders) : MG mesure Kelly-ness des sources wallets, **pas** notre propre sizing. Notre sizing reste `COPY_RATIO` + `MAX_POSITION_USD` configs. Kelly sizing de polycopy = spec future si volonté d'adopter.

## 8. Success criteria

1. **Tests ciblés verts** : ~15 nouveaux tests unit + 2 integration.
2. **CLV coverage** : post-ship, ≥80% des wallets ACTIVE ont `clv_avg != None` (assez de positions résolues).
3. **Kelly proxy opérationnel** : sur 50 wallets shadow+active, au moins 5 avec `conviction_sigma > 0.5` (identifiés comme Kelly-like).
4. **Latency tolerance filter effective** : au moins 1 wallet ACTIVE est down-weighted (multiplier < 0.7) pour scalping.
5. **Shadow v2.2.1 vs v2.2** : Spearman rank correlation > 0.6 (pas totalement décorrélés) et < 0.95 (apporte info nouvelle).
6. **Delta top-10 v2.2 vs v2.2.1** : changement de ≥2 wallets dans le top-10 post-MG intégration.

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MG.1 | — | — (new) | F08 (2/3), Gemini §"Alternative Factors" + §5 réponse, Claude §9 item 1 | #17 |
| MG.2 | — | — (new, full version) | F09 (2/3), Gemini §"Kelly", Claude §9 item 2 | #18 |
| MG.3 | — | — (new) | F10 (1/3 Claude unique), Kyle's λ arXiv | #19 |
| MG.4 | — | — (new) | F38 Claude §5.1, synthèse §5.1 | #38 |
| MG.5 | — | — (assembly) | Claude §4.2 v2.2 | #27 extension |

## 10. Prompt de génération de spec

````markdown
# Contexte

Lis `docs/next/MG.md` en entier. C'est le brief actionnable du module MG
(CLV + Kelly proxy + liquidity-adjusted ROI + latency tolerance factors). Ces
4 facteurs enrichissent v2.2-DISCRIMINATING (MF) ; MG peut ship avant ou après
MF — le spec doit couvrir les deux cas.

# Tâche

Produire `docs/specs/M19-scoring-factors-clv-kelly-liquidity.md` suivant
strictement le format des specs M1..M18 existantes.

Numéro : M19 (après MA=M14, MB=M15, MC=M16, MD=M17, ME=M18).

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Scoring v2 + §Conventions (rank normalize partout post-MA)
- `docs/specs/M14-scoring-v2.1-robust.md` (MA shipped — rank transform infra)
- `docs/specs/M15-anti-toxic-lifecycle.md` (MB shipped — internal_pnl_score)
- `docs/specs/M12-scoring-v2.md` comme référence DTO + metrics_collector_v2
- `docs/specs/M13_dry_run_observability_spec.md` comme template de forme
- Synthèse §2.3 F08 CLV + F09 Kelly + §2.4 F10 liquidity + §5.1 F38 latency
- Gemini §"Alternative Empirical Factors" + §"Fallacy of Full Kelly" + §5 réponses
- Claude §9 items 1-4 missing factors
- Papers Kelly 1956, Thorp 1969, Kyle's λ arXiv 2603.03136

# Contraintes

- Lecture seule src/ + docs
- Écriture uniquement `docs/specs/M19-scoring-factors-clv-kelly-liquidity.md`
- Longueur cible : 1000-1300 lignes
- Migration Alembic : **aucune** (pur code + nouveaux facteurs)
- Clarifier §Architecture le cas timing MG avant/après MF (v2.2 vs v2.2.1)
- Documenter §Algorithmes en pseudocode détaillé pour les 4 facteurs

# Livrable

- Le fichier `docs/specs/M19-scoring-factors-clv-kelly-liquidity.md` complet
- Un ping final ≤ 10 lignes : tests estimés, charge cumulée, ordre commits
  (recommandé : MG.1 CLV → MG.2 Kelly → MG.3 liquidity → MG.4 tolerance → MG.5
  aggregator integration)
````

## 11. Notes d'implémentation

### Piège : CLV data dependency — `closing_price` fetching

Pour chaque position résolue, on a besoin du `closing_price` au moment de la résolution. Options :
1. **Gamma `/markets?condition_ids=<id>&closed=true`** renvoie `outcomePrices` finaux — **mais** pas le `midpoint` à `T-1 minute` avant résolution. On a `[1.0, 0.0]` ou `[0.0, 1.0]` au moment de la résolution.
2. **Midpoint juste avant résolution** : via Gamma historical endpoint (si exposé) ou WSS `last_trade_price` capturé pré-résolution.

**Décision** : utiliser `outcomePrices` au moment de résolution comme proxy. Pour une position BUY YES à $0.20 qui close à `outcomePrices=[1.0, 0.0]` (YES wins) → `CLV = 1.0 - 0.20 = +$0.80`. Pour BUY YES à $0.20 qui close NO → `CLV = 0.0 - 0.20 = -$0.20`. **Simplification** : CLV = realized_outcome (0 ou 1) - entry_price. Sur side acheté.

**Version plus sophistiquée** : capture le midpoint à `T-5min` avant résolution via snapshot WSS régulier. Spec future si gain mesuré.

### Piège : conviction_sigma et arbitrage bots

Un arbitrageur YES+NO qui split son capital sur deux sides d'un même market peut avoir `conviction_sigma` élevé (trades varient entre buy/sell). Mais il n'est pas "Kelly-like", il est arb. Le **gate MB.7** (`arbitrage_bot` exclusion via `net_exposure_ratio`) doit filtrer ces wallets **avant** conviction_sigma compute. Sinon faux signal.

### Piège : `avg_holding_time` sur positions encore ouvertes

Pour un wallet ACTIVE avec plein de positions ouvertes (pas encore resolved), on ne peut pas calculer le full `holding_time`. Options :
1. Calculer uniquement sur closed positions (sous-échantillon).
2. Utiliser `age_of_open_positions` comme proxy (position ouverte depuis X jours = holding time implied ≥ X).

**Décision** : option 1, exclure ouvertes. Option 2 biaise vers macro-swing (en attendant résolution, holding time croît artificiellement). Si count(closed) < 10, `latency_tolerance_score = None` (cold-start).

### Piège : normalisation bankroll `/value` rate limit

Data API `/value?user=<addr>` est soumis aux rate limits (Perplexity B1 : ~hundreds/10s global). Sur 50 wallets shadow + 8 active, 58 calls par cycle discovery. Cache TTL 24h recommandé (bankroll évolue lentement).

### Piège : CLV weight interference

Si CLV weight = 0.10 et on réduit calibration de 0.15 à 0.10, le total est maintenu à 1.0. **Mais** : cela suppose calibration était sur-pondéré. **H-EMP-5** (Convexly Spearman Brier/PnL = +0.608) suggère oui. Si H-EMP-5 réfute Convexly sur notre pool, peut-être ne pas réduire calibration autant. Spec M19 tranche post-validation.

### Références externes

- **Gemini §"Alternative Empirical Factors"** : CLV as primary metric. Citations [XCLSV Media CLV 2026](https://xclsvmedia.com/closing-line-value-clv-explained-the-complete-guide-for-sports-bettors-in-2026/), [Webopedia CLV](https://www.webopedia.com/crypto-gambling/sportsbooks/how-to/closing-line-value-clv-explained/), [VSiN CLV](https://vsin.com/how-to-bet/the-importance-of-closing-line-value/), [Betstamp](https://betstamp.com/education/what-is-closing-line-value-clv), [Reddit r/algobetting CLV vs WR](https://www.reddit.com/r/algobetting/comments/1rp54ks/clv_vs_win_rate_what_actually_matters_when/).
- **Kelly 1956 / Thorp 1969** : [Wikipedia](https://en.wikipedia.org/wiki/Kelly_criterion), [Yoder 2023](https://nickyoder.com/kelly-criterion/).
- **Frigo 2024 Kalshi AI bot** : [GitHub](https://github.com/ryanfrigo/kalshi-ai-trading-bot). Empirical 0.25x Kelly catastrophic drawdowns above.
- **Kyle's λ Anatomy of Polymarket** : [arXiv 2603.03136](https://arxiv.org/html/2603.03136v1). Liquidity framework for prediction markets.
- **Claude §5.1 latency tolerance argument** : "If scoring picks a 1% better wallet than the prior week's scoring, that dominates 1¢ of latency slippage".

### Questions ouvertes pertinentes à MG

- **Q3** (synthèse §11) : Convexly Brier/PnL négatif — validable post-MG via Spearman calibration vs internal_pnl vs CLV. Si CLV prédit mieux que calibration, conforte down-weight calibration.
- **Q-MG-1** : CLV weight optimal — 0.10 (D1) vs 0.15. À backtest empiriquement post-ship.
- **Q-MG-2** : distribution holding_time sur notre pool — informe D5 multiplicateur latency_tolerance.
