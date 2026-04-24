# MC — Fees dynamic + EV adjustment

**Priorité** : 🔥 P1 (critique post-March 2026, blocker EV correctness)
**Charge estimée** : M (2-3 jours)
**Branche suggérée** : `feat/dynamic-fees-sizer`
**Prérequis** : aucun
**Bloque** : — (indépendant, parallélisable)

---

## 1. Objectif business

Depuis le **30 mars 2026**, Polymarket applique des **fees taker dynamiques** jusqu'à **1.80%** (Crypto), 1.00-1.50% (Politics/Finance/Tech/Economics), 0.75% (Sports). Notre `PositionSizer` calcule l'EV **sans** soustraire les fees — résultat : **chaque trade ≥ quelques % EV est en réalité EV-négatif post-fee** sur markets crypto. C'est un blocker silencieux de la performance dès que la shadow period se met à valider des wallets. Intégration du `FeeRateClient` + adjustment `PositionSizer.calculate()` EV-aware.

## 2. Contexte & problème observé

### Observations 2026-04-24

- Dashboard `/exécution` montre de nombreux trades SIMULATED sur markets crypto-style (`will-bitcoin-reach-85k-in-april-2026` à $188, etc.) dont l'EV calculée par le Sizer ne tient pas compte des fees 1.80%.
- Sur positions ~$50-100 avec fees 1.80% Crypto : **−$0.90 à −$1.80 par trade de fee-drag non comptabilisé**.
- Notre `STRATEGY_MAX_ENTRY_PRICE = 0.97` et `MAX_SLIPPAGE_PCT = 2.0` ont été calibrés pour zero-fee era → **tous les seuils EV sont décalés**.

### Findings référencés

- **[F60] 🔵 Perplexity unique détaillé** (synthèse §2.4, §4.7, §5.4) : Dynamic fees March 30 2026 rollout complet. Perplexity C4+D1 documente la table complète :

    | Category | Peak taker fee | Détails |
    |---|---|---|
    | Crypto | **1.80%** | High-velocity 15-min markets |
    | Economics | 1.50% | |
    | Culture / Weather / Other / Mentions | 1.25% | |
    | Finance / Politics / Tech | 1.00% | |
    | Sports | 0.75% | |

  Sources : [KuCoin fees guide](https://www.kucoin.com/blog/polymarket-fees-trading-guide-2026), [FinanceFeeds](https://financefeeds.com/polymarket-fees-revenue-surge-new-pricing-model/) ($560k → $1M+ fee revenue April 1), [MEXC taker fees expansion](https://www.mexc.com/news/976171) (8 new categories March 30), [Polymarket Help Trading Fees](https://help.polymarket.com/en/articles/13364478-trading-fees) (max effective 1.80% at 50% prob).

- **Synthèse §5.4** : "Current v1 EV = (P(YES) × 1.0) - cost (pas de fee) ; Post-fee EV = EV − fee_rate × notional. Fee impact ≈ 0.6% du notional (Politics/Finance), 1.5-1.8% (Economics/Crypto). Nos trades < 1% EV deviennent structurellement négatifs post-fees. Recalibrage nécessaire."

- **Endpoint documenté** : `GET /fee-rate?tokenID=<id>` (Polymarket API). Cache TTL 60s recommandé par synthèse §5.4.

### Session originale mappée

**Pas de session A-E dédiée** — MC naît directement du deep-search Perplexity C4+D1. Item #11 de la roadmap §9 synthèse. Initialement planifié M16 dans `docs/development/M10_synthesis_reference.md` roadmap mais **désormais critique**.

### Pourquoi c'est P1 critique

Sans MC, chaque jour de trading en mode **live** (et même en dry-run post-Bug 1 fix) nous fait sur-scorer les wallets sur des stratégies dont l'EV est faussée. Le pire : **en dry-run**, on **ne voit pas** l'impact (nos orders sont SIMULATED, pas de vraie perte cash) donc on valide une stratégie qui serait perdante en live. **C'est un blocker invisible du passage live**.

## 3. Scope (items détaillés)

### MC.1 — Nouveau `FeeRateClient`

- **Location** : nouveau fichier `src/polycopy/executor/fee_rate_client.py`
- **Ce qu'il faut faire** :
  - Nouveau client async `FeeRateClient` avec méthode `async def get_fee_rate(token_id: str) -> Decimal`.
  - Endpoint : `GET https://clob.polymarket.com/fee-rate?tokenID=<id>` (read-only public, **aucune auth**).
  - Response schema : probablement `{"makerFee": "0.0", "takerFee": "0.0125"}` ou équivalent — à capturer en fixture réelle au début du dev (voir §11 Notes).
  - Cache in-memory `dict[str, tuple[Decimal, datetime]]` avec **TTL 60s** (recommandé synthèse §5.4).
  - LRU cap : 500 entrées (similaire au cache M8 orderbook).
  - Pattern single-flight (TOCTOU fix préventif — voir audit M-007) : lock par token_id pour éviter N fetches redondants.
  - Fallback si erreur réseau : retourner **`Decimal("0.018")`** (= 1.80%, conservative) + log WARNING `fee_rate_fetch_failed_using_conservative_fallback`. Mieux vaut sur-estimer fee et rejeter un bon trade que sous-estimer et trader à perte.
  - Reconnect logic via tenacity (pattern cohérent `ClobMarketWSClient`).
- **Tests requis** :
  - `test_fee_rate_client_returns_decimal`
  - `test_fee_rate_client_caches_60s`
  - `test_fee_rate_client_fallback_on_network_error`
  - `test_fee_rate_client_single_flight_prevents_redundant_fetches`
  - `test_fee_rate_client_lru_cap_eviction`
- **Sources deep-search** : Perplexity C4+D1 endpoint + synthèse §5.4 spec.
- **Charge item** : 1 jour

### MC.2 — `PositionSizer.calculate()` EV-aware post-fees

- **Location** : [src/polycopy/strategy/pipeline.py:177-210](../../src/polycopy/strategy/pipeline.py#L177-L210) (PositionSizer)
- **Ce qu'il faut faire** :
  - Injecter `FeeRateClient` via constructor `PositionSizer(..., fee_rate_client: FeeRateClient)`. **Décision D1** : optional avec default `None` pour les tests (si None, fee=0 utilisé, préserve tests existants).
  - Dans `_check_buy()` après calcul `raw_size` + `cap_size`, avant final `ctx.my_size = min(...)` :
    ```python
    fee_rate = Decimal("0")
    if self._fee_rate_client is not None:
        fee_rate = await self._fee_rate_client.get_fee_rate(ctx.trade.asset_id)
    notional = Decimal(str(ctx.my_size)) * Decimal(str(ctx.trade.price))
    fee_cost = notional * fee_rate

    # Post-fee EV check : si EV - fee_cost < MIN_EV_USD_AFTER_FEE : rejet
    expected_payout = Decimal(str(ctx.my_size)) * Decimal("1.0")  # si YES wins
    ev_before_fee = expected_payout * Decimal(str(ctx.trade.price)) - notional
    # ... formule complète selon side, voir Notes
    ev_after_fee = ev_before_fee - fee_cost
    if ev_after_fee < settings.strategy_min_ev_usd:
        return FilterResult(passed=False, reason="ev_negative_after_fees")
    ```
  - Nouveau reason code `ev_negative_after_fees` dans `strategy_decisions`.
  - **Attention** : calcul EV dépend du side BUY YES vs BUY NO. Le prix entre est la probabilité implicite du side pris. Détails en §11.
  - Le fee_rate s'applique uniquement aux **takers** (nos FOK orders sont toujours takers en dry-run M8 et M3 live). Skip si maker (pas applicable à polycopy).
- **Tests requis** :
  - `test_position_sizer_subtracts_fee_from_ev`
  - `test_position_sizer_rejects_negative_ev_after_fee`
  - `test_position_sizer_no_fee_client_preserves_behavior` (regression)
  - `test_position_sizer_reason_code_ev_negative_after_fees`
  - `test_position_sizer_buy_yes_vs_buy_no_ev_calculation` (side-awareness)
- **Sources deep-search** : Perplexity C4+D1 + synthèse §5.4.
- **Charge item** : 1 jour

### MC.3 — Nouveau setting `STRATEGY_MIN_EV_USD_AFTER_FEE` + mise à jour `.env.example`

- **Location** : [src/polycopy/config.py](../../src/polycopy/config.py) + [.env.example](../../.env.example)
- **Ce qu'il faut faire** :
  - Nouveau setting Pydantic `strategy_min_ev_usd_after_fee: Decimal = Decimal("0.05")` (5 cents minimum EV post-fee pour approve).
  - Validator `ge=Decimal("0.01"), le=Decimal("10.0")`.
  - `.env.example` commenté :
    ```bash
    # MC — Fees dynamiques (ship March 2026)
    # Seuil minimal d'EV post-fee pour approuver un trade.
    # Trop bas : on accepte des trades EV-négatifs post-fee volatility.
    # Trop haut : on rejette trop de trades, discovery échantillon trop faible.
    STRATEGY_MIN_EV_USD_AFTER_FEE=0.05
    ```
  - CLAUDE.md §Conventions : ajouter note sur le nouveau seuil + impact March 2026.
  - Mise à jour du preset A/B/C dans `docs/night_test_runbook.md` avec le nouveau setting.
- **Tests requis** :
  - `test_settings_strategy_min_ev_validator_bounds`
  - `test_cli_boot_logs_current_min_ev_at_startup`
- **Sources** : synthèse §5.4 recommandation seuil conservative.
- **Charge item** : 0.5 jour

### MC.4 — Dashboard `/stratégie` : afficher `ev_negative_after_fees` count + fee_drag totals

- **Location** : [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) + [src/polycopy/dashboard/templates/strategy.html](../../src/polycopy/dashboard/templates/strategy.html)
- **Ce qu'il faut faire** :
  - **Minimal viable** : ajouter compteur `rejected_by_reason["ev_negative_after_fees"]` dans les stats /stratégie (cohérent avec `liquidity_too_low`, `slippage_exceeded` existants).
  - **Bonus UX** (déferrable MH) : panel "Fee impact" sur `/stratégie` qui montre :
    - Total fees attendues sur les 24h (sum `fee_rate × notional` sur trades approved)
    - Fee-drag % du volume total
    - Breakdown par catégorie Gamma (Crypto vs Sports vs Politics)
  - Le bonus UX est **OUT OF SCOPE MC**, appartient à **MH**. MC ship juste le count.
- **Tests requis** :
  - `test_strategy_dashboard_shows_ev_negative_rejection_count`
- **Sources** : synthèse §5.4 + UX cohérence.
- **Charge item** : 0.5 jour

### MC.5 — Co-lancement `FeeRateClient` dans `ExecutorOrchestrator`

- **Location** : [src/polycopy/executor/orchestrator.py](../../src/polycopy/executor/orchestrator.py) + [src/polycopy/cli/boot.py](../../src/polycopy/cli/boot.py)
- **Ce qu'il faut faire** :
  - Instancier `FeeRateClient` au boot (ou dans `StrategyOrchestrator` si plus logique — c'est le consommateur principal via PositionSizer).
  - **Décision D2** : l'instancier dans `StrategyOrchestrator` pas `ExecutorOrchestrator`. Raison : le fee check arrive **avant** l'envoi d'order (dans le pipeline strategy), pas après. Cohérent avec où vit `SlippageChecker`.
  - `await fee_rate_client.close()` sur shutdown (cohérent avec autres clients httpx).
  - Injection dans `PositionSizer` constructor via `StrategyOrchestrator.__init__`.
- **Tests requis** :
  - `test_strategy_orchestrator_instantiates_fee_rate_client`
  - `test_strategy_orchestrator_closes_fee_rate_client_on_shutdown`
- **Sources** : cohérence architecturale M2 + M11.
- **Charge item** : 0.5 jour

## 4. Architecture / décisions clefs

- **D1** : `FeeRateClient` optional dans PositionSizer constructor (default None). Justification : préserve 200+ tests existants qui instancient PositionSizer sans fee client. Permet tests incrémentaux sans cascade.
- **D2** : Client vit dans `StrategyOrchestrator` (consommateur) pas `ExecutorOrchestrator`. Justification : fee check s'applique au **rejet pre-POST**, cohérent avec SlippageChecker.
- **D3** : Fallback conservateur 1.80% si network error. Justification : rater un bon trade (false negative fee-rejection) est moins coûteux que passer un trade fee-négatif (false positive). **Asymétrie d'impact**.
- **D4** : Cache TTL 60s (vs 300s Gamma adaptive). Justification : fees peuvent bouger rapidement (Polymarket ajuste dynamiquement), 60s est conservative.
- **D5** : Single-flight pattern dans FeeRateClient. Justification : évite TOCTOU redondance (audit M-007) préventivement.
- **D6** : Seuil default `STRATEGY_MIN_EV_USD_AFTER_FEE = 0.05` (5¢). Justification : ordre de grandeur cohérent avec nos capital sizes $50-200 (5¢ = 0.025-0.1% notional minimum). Ajustable empiriquement post-ship.
- **D7** : Skip si `fee_rate_client is None` (pas d'erreur). Justification : permet configuration qui désactive fee-awareness (backward compat + testing + SIMULATION mode).

## 5. Invariants sécurité

- **Triple garde-fou M3** : intact. `FeeRateClient` est read-only public endpoint, ne touche à rien des creds CLOB L2.
- **4ᵉ garde-fou M8** : intact. Le path `_persist_realistic_simulated` est après `PositionSizer`, donc le fee check arrive en amont. Les SELL orders traversent aussi le check (pas de bypass).
- **Zéro secret loggé** : FeeRateClient log uniquement `token_id`, `fee_rate`, `cache_hit/miss` — aucun secret. Test `test_fee_rate_client_no_secret_leak_in_logs`.
- **Lecture seule API publique** : endpoint `GET /fee-rate` est public no-auth, pas de surface creds ajoutée. Cohérent invariant M2.
- **Decimal strict** : toute la logique fee utilise `Decimal` (cf. CLAUDE.md convention M8 "Decimal interne, float à la persistance"). Pas de float mixing qui créerait drift d'arrondi.

## 6. Hypothèses empiriques à valider AVANT ship

- **H-EMP-10** (synthèse §8) : Fees dynamiques réduisent notre EV moyen de ≥1% post-implementation. **Méthode** : post-MC.2, sur 100 trades synthétiques (fixture réaliste), calculer `ev_before_fee - ev_after_fee` distribution. **Seuil informatif** : si moyenne < 0.5%, fees ne sont pas impactantes sur notre portfolio mix. Si > 1.5%, recalibrer `STRATEGY_MIN_EV_USD_AFTER_FEE`.
- **Q5** (synthèse §11) : quel % de trades seraient rejetés par `ev_negative_after_fees` post-MC ? Mesure empirique sur 100 décisions historiques rejouées. Si > 30% de rejet addition, seuil trop strict OU notre pool actif prend structurellement des trades sub-marginaux.

## 7. Out of scope

- **Maker fees / rebates** : polycopy est taker-only (FOK orders M3 + M8). Pas de consideration maker dans MC. Si un jour maker strategy (v∞), spec dédiée.
- **Fee rate historique / backtest** : MC gère le live. Backtest de fees historiques = spec future si besoin.
- **Dashboard `/stratégie` panel complet fee-drag** : migre en **MH** (UX détaillée).
- **Alertes Telegram fee spike** : hors scope MC. Si fee rate > 2% inhabituel, pas d'alerte v1. Ajout futur si observé.
- **FeeRateClient WebSocket** : endpoint REST suffit (TTL 60s acceptable), pas de WS needed. Polymarket ne publie pas de WSS dédié fees.
- **Gestion tier utilisateur** (Perplexity B1 mentionne Builder Tiers) : on assume Standard tier, pas de logique tier-specific.
- **Retour au modèle zero-fee hypothétique** : non anticipé. Si Polymarket retire les fees (improbable), `FeeRateClient` retourne 0 naturellement et le flow passe.

## 8. Success criteria

1. **Tests ciblés verts** : ~12 nouveaux tests unit + 2 integration.
2. **Fee endpoint capturé** : fixture réelle `tests/fixtures/fee_rate_crypto_sample.json` + `tests/fixtures/fee_rate_sports_sample.json` depuis live API (via skill polymarket ou curl).
3. **EV calculation post-fee correct** : spot-check sur 5 trades types (BUY YES $0.40 Crypto, BUY NO $0.60 Sports, etc.) — le calcul manuel match le résultat du code.
4. **Rejet stratégie visible** : post-ship 24h, sur `/stratégie`, le reason `ev_negative_after_fees` apparaît avec ≥0 rejections (count non-nul attendu sur notre portfolio mix).
5. **Pas de régression perf** : PositionSizer p95 latence augmente de ≤50ms (cache hit rate attendu > 90% sur tokens actifs).
6. **Fallback conservateur testé** : simuler network outage FeeRateClient → verify les trades Crypto ne passent pas (fee 1.80% défaut bloquant), trades Sports passent (0.75% toléré).

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MC.1 | — | — | F60 (Perplexity unique C4+D1) | #11 |
| MC.2 | — | — | F60 + synthèse §5.4 | #11 |
| MC.3 | — | — | synthèse §5.4 | #11 |
| MC.4 | — | — | synthèse §5.4 + UX cohérence | — (bonus UX) |
| MC.5 | — | — | cohérence arch M2+M11 | — |

## 10. Prompt de génération de spec

````markdown
# Contexte

Lis `docs/next/MC.md` en entier. C'est le brief actionnable du module MC
(Dynamic taker fees + EV adjustment). Il référence deep-search Perplexity C4+D1
et synthèse §5.4.

# Tâche

Produire `docs/specs/M16-dynamic-fees-ev.md` suivant strictement le format
des specs M1..M15 existantes (§ numérotées : TL;DR, Motivation, Scope, User
stories, Architecture, Algorithmes, DTOs, Settings, Invariants sécurité, Test
plan, Impact existant, Migration, Commandes vérif, Hors scope, Notes
implémentation, Prompt implémentation, Commit message proposé).

Numéro : M16 (après M14=MA et M15=MB).

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Conventions (Decimal interne) + §Sécurité (triple garde-fou M3+M8)
- `docs/specs/M13_dry_run_observability_spec.md` comme template de forme
- `docs/specs/M2-strategy-engine.md` comme référence PositionSizer actuel
- Perplexity §C4 (fees table complète) + §D1 (rollout March 2026)
- Polymarket [Help Trading Fees](https://help.polymarket.com/en/articles/13364478-trading-fees)
- Skill `/polymarket:polymarket` pour le schema endpoint `/fee-rate`

# Contraintes

- Lecture seule src/ + docs sources
- Écriture uniquement `docs/specs/M16-dynamic-fees-ev.md`
- Longueur cible : 800-1200 lignes
- **Capturer fixture réelle** `tests/fixtures/fee_rate_*.json` via skill polymarket
  OU documenter dans spec §Commandes vérif comment la capturer manuellement
- Migration Alembic : **aucune** (MC est pur client + logique, pas de DB schema)

# Livrable

- Le fichier `docs/specs/M16-dynamic-fees-ev.md` complet
- Un ping final ≤ 10 lignes : tests estimés, charge cumulée, décisions ouvertes
  restantes (ex: confirmer schema response endpoint)
````

## 11. Notes d'implémentation

### Piège : calcul EV side-aware

**BUY YES à $0.40** :
- Cost = size × 0.40
- Payoff si YES wins = size × 1.00 → net = size × 0.60 (gain)
- Payoff si YES loses = 0 → net = -size × 0.40 (loss)
- EV = P(YES) × (size × 0.60) + (1 - P(YES)) × (-size × 0.40)
- Si P(YES) = 0.40 (prix = probabilité implicite) → EV = 0 (fair bet)
- **EV réel** = size × (P(YES)_true - 0.40) (gain si on croit YES plus probable que 40%)
- **Approximation polycopy** : on copie la probabilité du source wallet, `EV = my_size × edge_vs_mid`. Simplification mais suffisante.
- Post-fee : `ev_after_fee = ev - fee_rate × notional` où `notional = size × price`.

**BUY NO à $0.60** (équivalent BUY YES à $0.40 miroir) :
- Même logique avec NO token, prob implicite = price.
- `ev_after_fee = my_size × edge_vs_mid - fee_rate × my_size × 0.60`.

### Piège : fee_rate unit

Polymarket documente **1.80% comme "max effective"** ([Help Polymarket](https://help.polymarket.com/en/articles/13364478-trading-fees)). Le taux réel varie selon prix (`fee drops at extremes`, cf. Help). L'endpoint `/fee-rate?tokenID=` retourne probablement le taux effectif courant pour ce token.

**À confirmer** via capture fixture : format `"0.018"` (0.018 = 1.8%) ou `"1.8"` (1.8 = 1.8%) ? Le premier est plus standard Polymarket (pattern M11 WS `outcomePrices=["0.4", "0.6"]` unitaire). Confirmer dans le spec M16.

### Piège : TTL 60s vs rate limit Polymarket

Rate limit Polymarket CLOB : 1500/10s sur /book et /price endpoints (Perplexity B1). Le `/fee-rate` endpoint n'est pas documenté séparément, assume ~CLOB pattern. Avec TTL 60s + ~50 tokens actifs dans polycopy, ~50 requêtes/minute = largement sous le cap.

### Piège : Decimal vs float dans formule EV

Le pipeline strategy actuel utilise `float` partout. Introduction de `Decimal` dans le fee calculation uniquement. **Attention** au mixing `Decimal(str(float_value))` pour éviter artefacts d'arrondi. Final `my_size` reste `float` (M8 convention persistence). EV interne calculé en Decimal, converti à float pour persistence.

### Références externes

- **Polymarket Help Fees** : [help.polymarket.com/en/articles/13364478-trading-fees](https://help.polymarket.com/en/articles/13364478-trading-fees) — référence officielle.
- **KuCoin fees guide** : [kucoin.com/blog/polymarket-fees-trading-guide-2026](https://www.kucoin.com/blog/polymarket-fees-trading-guide-2026) — explainer.
- **MEXC 8 new categories** : [mexc.com/news/976171](https://www.mexc.com/news/976171) — rollout March 30.
- **FinanceFeeds revenue** : [financefeeds.com/polymarket-fees-revenue-surge-new-pricing-model](https://financefeeds.com/polymarket-fees-revenue-surge-new-pricing-model/) — preuve impact.
- **Polymarket US DCM fee schedule** : [polymarketexchange.com/fees-hours.html](https://www.polymarketexchange.com/fees-hours.html) — régulé US différent (0.05 coefficient max $1.25/100 contracts). **Pas applicable à polycopy** (on trade le venue offshore, pas US DCM).

### Questions ouvertes pertinentes à MC

- **Q5** (synthèse §11) : impact fees sur notre EV actuelle ? Mesurable post-MC.
- **Q10** (implicite) : arbitrageurs YES+NO paient fees sur les deux sides → leur EV net est **encore plus faible** post-fees. Arg supplémentaire pour `arbitrage_bot_gate` (MB.7).
