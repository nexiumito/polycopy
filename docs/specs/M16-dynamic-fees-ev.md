# M16 — Dynamic taker fees + EV adjustment

**Status** : Draft — 2026-04-25
**Depends on** : M2 (Strategy pipeline + `PositionSizer`), M3 (Executor — triple
garde-fou intact), M8 (Dry-run realistic fill — 4ᵉ garde-fou intact), M11
(`ClobMarketWSClient` pattern de référence pour caching + tenacity), M13
(`PositionSizer` side-aware déjà mergé)
**Bloque** : — (indépendant, parallélisable avec MA / MB / MD-MJ)
**Workflow git** : commits directement sur `main` (pas de branche, pas de PR — règle projet)
**Charge estimée** : M (2-3 jours dev, 0 jour shadow — comportement déterministe)
**Numéro** : M16 (après M14 = MA `scoring v2.1`, M15 = MB `lifecycle internal_pnl`)

---

## 0. TL;DR

M16 livre un **`FeeRateClient`** read-only public + un upgrade EV-aware du
`PositionSizer` qui soustrait le coût des **fees taker dynamiques Polymarket**
(rollout étendu **30 mars 2026** sur Crypto + NCAAB + Serie A) avant
d'approuver un trade.

**5 items couplés** mappés MC.1 → MC.5 du brief
[docs/next/MC.md](../next/MC.md) :

- **MC.1** — Nouveau client async `FeeRateClient` (endpoint public
  `GET /fee-rate?token_id=<id>`, schéma `{"base_fee": <bps>}`, capture fixture
  réelle confirmée 2026-04-25, pattern cache TTL 60s + LRU 500 entries +
  single-flight + tenacity).
- **MC.2** — `PositionSizer._check_buy()` calcule l'EV post-fee via formule
  Polymarket officielle `feeRate × (p × (1-p))^exponent` paramétrée par
  `feeType` Gamma (`crypto_fees_v2` → exp=2 / sport variants → exp=1) et
  rejette `ev_negative_after_fees` quand `EV - fee_cost < strategy_min_ev_usd_after_fee`.
- **MC.3** — Nouvelle setting `STRATEGY_MIN_EV_USD_AFTER_FEE` (default
  `Decimal("0.05")`) + `.env.example` commenté + CLAUDE.md §Conventions
  enrichi.
- **MC.4** — Dashboard `/strategie` ajoute le compteur
  `rejected_by_reason["ev_negative_after_fees"]` (cohérent avec
  `liquidity_too_low`, `slippage_exceeded`). Panel "Fee impact" déféré à MH.
- **MC.5** — Co-lancement `FeeRateClient` dans `StrategyOrchestrator` (et
  **non** `ExecutorOrchestrator` — décision **D2** infra, le fee check vit en
  amont du POST CLOB), `aclose()` propre au shutdown.

Diff strictement additif — aucun fichier supprimé, aucun comportement existant
modifié quand `STRATEGY_FEES_AWARE_ENABLED=false` (default backward-compat) ou
quand le marché renvoie `base_fee=0` (vaste majorité des marchés Polymarket
hors crypto/NCAAB/Serie A — cf. §1.4).

Tests cumulés estimés : **~14 tests unit** (MC.1 = 5, MC.2 = 5, MC.3 = 2, MC.4
= 1, MC.5 = 1) + **0 test intégration obligatoire** (capture fixture réelle
suffit en §12).

Prérequis : aucun. Bloque : aucun. **Aucune migration Alembic** (M16 est pur
client + logique pipeline + dashboard count, pas de DB schema).

---

## 1. Motivation & use case concret

### 1.1 Le symptôme observé — 2026-04-24

Dashboard `/exécution` sur `uni-debian` (test 14j en cours, mode
`EXECUTION_MODE=dry_run` + `DRY_RUN_REALISTIC_FILL=true`) :

> - Wallet `0xe8dd…ec86` : BUY 50 shares à $0.50 sur marché crypto
>   `bitcoin-up-or-down-on-april-25-2026` (`feesEnabled=true`,
>   `feeType="crypto_fees_v2"`).
> - `PositionSizer` calcule `cost = my_size × 0.50 = $25`, valide via
>   `RiskManager` ($25 < $1000 capital), order envoyé à l'executor.
> - `MyOrder.simulated=true`, `MyPosition` ouverte virtuellement.
> - **Aucune fee n'est soustraite** du calcul EV : la formule actuelle est
>   strictement `cost = size × price`.
> - Sur ce trade Crypto à $0.50, fee Polymarket effective = **1.56% du
>   notional** (= **$0.39** sur $25), soit **−1.6% d'EV silencieux**.

Sur l'échantillon 24h M13 (688 ordres `SENT`, mix Crypto + Politics) :
- ~5-10 % des trades visent des marchés `feesEnabled=true` (crypto only —
  Politics/Tech/Finance/Sports binaires hors NCAAB/Serie A restent fee-free
  per [docs Polymarket officielles](https://help.polymarket.com/en/articles/13364478-trading-fees)).
- Sur ces ~50 trades crypto, **fee-drag silencieux estimé ≈ $20-30/jour**
  vs capital $1000 = **2-3% drag mensuel non comptabilisé**.

### 1.2 La triangulation deep-search

| Source | Référence | Apport |
|---|---|---|
| **Perplexity §C4 + §D1** | KuCoin / FinanceFeeds / MEXC March 2026 rollout | Identifie le rollout March 30 2026 et le risque générique. ⚠️ Surestime cependant la portée : Perplexity assume Politics/Tech/Finance à 1.00% alors que les **docs officielles Polymarket** (capturées via skill 2026-03-22, cf. §11.5) confirment que **seules Crypto + NCAAB + Serie A** ont fees enabled. La majorité des markets Politics / Economics / Tech reste fee-free. |
| **Synthèse §5.4** | Tableau impact EV consolidé | "Notre `PositionSizer` calcule `cost = my_size × trade.price` sans soustraire fees. Sur trade $50 Crypto à 50% prob : fee $0.78 = 1.56%. **Recalibrage nécessaire.**" |
| **Polymarket Help Trading Fees** | [docs officielles](https://help.polymarket.com/en/articles/13364478-trading-fees) | **Source de vérité.** Formule exacte `fee = C × p × feeRate × (p × (1-p))^exponent`. Crypto : feeRate=0.25, exp=2, max 1.56% à p=0.5. Sports (NCAAB/Serie A) : feeRate=0.0175, exp=1, max 0.44% à p=0.5. |
| **Polymarket API `/fee-rate`** | Skill polymarket — capture réelle 2026-04-25 (cf. §11) | Confirme schema `{"base_fee": <int>}` (basis points, integer). Endpoint public no-auth. Crypto market réel renvoie `{"base_fee": 1000}` — sert de **flag fee-enabled** (≠ 0), pas comme rate effectif (le calcul vrai passe par la formule + `feeType` Gamma). |

### 1.3 Pourquoi P1 critique avant cutover live

Tant que le bot tourne en `dry_run`, l'absence de fee est **invisible** : nos
positions sont `simulated=true`, on ne paie pas de cash. Mais le PnL réalisé
M13 calcule `(payout - avg_price) × size` SANS fee — donc la **performance
mesurée en dry-run sur-estime systématiquement le live de 0.6-1.6%** sur les
trades crypto.

Conséquence : à la décision finale "live or not" post test 14j, **on validera
une stratégie qui sera structurellement déficitaire en live** sur les marchés
crypto. C'est un **blocker invisible du passage live**.

À cela s'ajoute : `STRATEGY_MAX_ENTRY_PRICE=0.97` et `MAX_SLIPPAGE_PCT=2.0`
ont été calibrés zero-fee era. Avec fees crypto 1.56%, un BUY à 0.97 +
slippage 1% + fee 0.16% (à p=0.97 fee descend, mais quand même) laisse
**zéro upside**. Recalibrage nécessaire avec `STRATEGY_MIN_EV_USD_AFTER_FEE`
explicite.

### 1.4 Ce qui ne change PAS dans M16

Diff M16 strictement additif sur les invariants suivants — aucune ligne
modifiée :

- **Triple garde-fou M3** : `FeeRateClient` est read-only public no-auth, ne
  touche aucune cred CLOB L1/L2 ni `WalletStateReader` ni `ClobWriteClient`.
- **4ᵉ garde-fou M8** : `_persist_realistic_simulated` reste préservé,
  l'`assert settings.execution_mode == "dry_run"` est en aval du fee check.
- **Pipeline order** : `TraderLifecycle → Market → EntryPrice → PositionSizer
  → SlippageChecker → RiskManager` reste identique. Le fee check vit
  **dans** `PositionSizer._check_buy` (pas un nouveau filtre — décision
  **D8** infra, économise une instanciation par cycle).
- **SELL passthrough** : les SELL ne paient **pas** de fee dans le calcul EV
  côté polycopy (le calcul de fee USDC s'applique aux BUY tant qu'on copie ;
  pour les SELL on ferme une position existante, le PnL réalisé reste
  `(price - avg_price) × size` sans fee adjustment côté pipeline — la fee
  est déjà payée à l'ouverture côté contract). Justification §11.3.
- **M14 scoring v2.1** : aucune interaction. Le score d'un wallet ne dépend
  pas de notre fee — c'est leur PnL historique on-chain qui compte.
- **M5 lifecycle / M5_bis eviction / M5_ter watcher** : intacts.
- **M11 latency stages** : intacts. Le fee fetch ajoute un nouveau stage
  potentiel `strategy_fee_fetched_ms` (cf. §11.2) **opt-in** via
  `LATENCY_INSTRUMENTATION_ENABLED=true` — pas de stage forcé.

### 1.5 Ce que change explicitement M16 (vue de haut)

| Module | Diff | Référence MC |
|---|---|---|
| [src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py) | **Nouveau fichier**. `FeeRateClient` async + cache TTL 60s + LRU 500 + single-flight + tenacity | MC.1 |
| [src/polycopy/strategy/pipeline.py:177-191](../../src/polycopy/strategy/pipeline.py#L177-L191) | `PositionSizer._check_buy` calcule `effective_fee_rate` + EV post-fee + reason `ev_negative_after_fees` | MC.2 |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +1 setting `STRATEGY_MIN_EV_USD_AFTER_FEE` + flag `STRATEGY_FEES_AWARE_ENABLED` | MC.3 |
| [.env.example](../../.env.example) | +2 commentaires bloc M16 | MC.3 |
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | Aucun nouveau champ DTO. Le compteur `ev_negative_after_fees` apparaît automatiquement dans l'agrégat existant `rejected_by_reason` (clé string libre, déjà groupée en `Counter`) | MC.4 |
| [src/polycopy/dashboard/templates/strategy.html](../../src/polycopy/dashboard/templates/strategy.html) | Aucun template hardcode les reasons — boucle `for reason, count in rejected_by_reason.items()` qui rend automatiquement la nouvelle clé | MC.4 |
| [src/polycopy/strategy/orchestrator.py:75-80](../../src/polycopy/strategy/orchestrator.py#L75-L80) | Instanciation conditionnelle de `FeeRateClient` dans `run_forever` + injection dans `PositionSizer` (via `run_pipeline` paramètre) | MC.5 |
| [src/polycopy/strategy/pipeline.py::run_pipeline](../../src/polycopy/strategy/pipeline.py#L287) | +1 paramètre optionnel `fee_rate_client: FeeRateClient | None = None` | MC.5 |
| [tests/fixtures/clob_fee_rate_crypto_sample.json](../../tests/fixtures/clob_fee_rate_crypto_sample.json) | **Capturé 2026-04-25** : `{"base_fee": 1000}` (bitcoin-up-or-down-on-april-25-2026) | MC.1 |
| [tests/fixtures/clob_fee_rate_zero_sample.json](../../tests/fixtures/clob_fee_rate_zero_sample.json) | **Capturé 2026-04-25** : `{"base_fee": 0}` (synthétique pour fee-free markets) | MC.1 |
| [tests/fixtures/clob_fee_rate_invalid_sample.json](../../tests/fixtures/clob_fee_rate_invalid_sample.json) | **Capturé 2026-04-25** : `{"error": "Invalid token id"}` (HTTP 400) | MC.1 |
| [tests/fixtures/gamma_market_crypto_fees_sample.json](../../tests/fixtures/gamma_market_crypto_fees_sample.json) | **Capturé 2026-04-25** : payload Gamma complet avec `feesEnabled=true`, `feeType="crypto_fees_v2"` | MC.2 |
| Tests | +14 unit ; +0 intégration obligatoire | tous |

---

## 2. Scope / non-goals

### 2.1 Dans le scope

**MC.1 — `FeeRateClient` read-only async** :

- Nouveau fichier
  [src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py)
  (50-80 LOC) — réutilise pattern strict
  [src/polycopy/strategy/clob_read_client.py](../../src/polycopy/strategy/clob_read_client.py)
  pour cohérence (httpx async + tenacity exponential backoff + structlog
  events).
- Méthode publique : `async def get_fee_rate(token_id: str) -> Decimal`.
  Retourne le `base_fee` parsé en bps (entier) **divisé par 10000** pour donner
  un Decimal ∈ [0, 1].
- Cache in-memory `dict[str, tuple[Decimal, datetime]]` avec **TTL 60s**
  (recommandé synthèse §5.4 + cohérence cache Gamma M2).
- LRU cap `STRATEGY_FEE_RATE_CACHE_MAX=500` (cohérent cache M8 orderbook
  + cache WS market). Eviction LRU à l'insertion quand cache plein.
- Pattern **single-flight** par token_id : `dict[str, asyncio.Future[Decimal]]`
  qui dédoublonne les fetches concurrents pour le même token_id (TOCTOU fix
  préventif — voir audit M-007).
- Fallback réseau : si `httpx.TransportError` après 5 tenacity retries OU
  HTTP 5xx persistant → retourne **`Decimal("0.018")`** (= 1.80%, max
  effective rate post-rollout March 30 2026 d'après Perplexity C4 +
  cohérent avec docs Polymarket live 2026-04-25 — couvre worst-case toutes
  catégories actuelles et futures). Log WARNING
  `fee_rate_fetch_failed_using_conservative_fallback`.
- HTTP 400 (`Invalid token id`) → log WARNING `fee_rate_invalid_token_id`,
  retourne **`Decimal("0.018")`** (même fallback conservateur 1.80%).
- HTTP 404 (`fee rate not found for market`) → log DEBUG
  `fee_rate_market_not_found`, retourne **`Decimal("0")`** (pas un fee
  market — fees=0 est l'attendu pour la majorité des markets).
- Tenacity reconnect : `retry_if_exception_type((TransportError,
  HTTPStatusError except 4xx))`, `wait_exponential(multiplier=1, min=1,
  max=30)`, `stop_after_attempt(5)`. Pattern strict copié de
  `ClobReadClient`.

**MC.2 — `PositionSizer._check_buy` EV-aware post-fees** :

- Injecter `fee_rate_client: FeeRateClient | None = None` dans le constructeur
  `PositionSizer.__init__` (default None pour rétrocompat tests M2..M15).
- Dans `_check_buy()` après `cap_size`, **avant** le commit de
  `ctx.my_size = min(raw_size, cap_size)` :

  ```python
  if self._fee_rate_client is None or not self._settings.strategy_fees_aware_enabled:
      # Backward-compat strict M2..M15 : pas de fee adjustment.
      ctx.my_size = min(raw_size, cap_size)
      ...
      return FilterResult(passed=True)

  base_fee_rate = await self._fee_rate_client.get_fee_rate(ctx.trade.asset_id)
  effective_fee_rate = self._compute_effective_fee_rate(
      base_fee_rate=base_fee_rate,
      price=Decimal(str(ctx.trade.price)),
      market=ctx.market,
  )

  raw_my_size = min(raw_size, cap_size)
  notional = Decimal(str(raw_my_size)) * Decimal(str(ctx.trade.price))
  fee_cost = notional * effective_fee_rate

  # EV approximation polycopy : on copie la prob du source wallet → edge
  # vs mid implicit = (mid - source_price) ; mais ici le BUY suit le source
  # à `trade.price`, pas au mid courant. EV = my_size × edge_vs_mid_pct.
  # Fallback simple : EV = max(0, my_size × max_residual_payoff − notional).
  # Pour BUY YES at p : max payoff = my_size × (1.0 − p) si YES wins (gain), 0 sinon.
  # Approximation : `expected_max_gain = raw_my_size * (1.0 - price)` (prob-weighted
  # par la conviction du source wallet, qu'on assume = trade.price).
  expected_max_gain = Decimal(str(raw_my_size)) * (Decimal("1.0") - Decimal(str(ctx.trade.price)))
  ev_after_fee = expected_max_gain - fee_cost

  if ev_after_fee < self._settings.strategy_min_ev_usd_after_fee:
      ctx.fee_rate = float(effective_fee_rate)
      ctx.fee_cost_usd = float(fee_cost)
      ctx.ev_after_fee_usd = float(ev_after_fee)
      return FilterResult(passed=False, reason="ev_negative_after_fees")

  ctx.my_size = raw_my_size
  ctx.fee_rate = float(effective_fee_rate)
  ctx.fee_cost_usd = float(fee_cost)
  ctx.ev_after_fee_usd = float(ev_after_fee)
  ```

- Helper privé `_compute_effective_fee_rate(base_fee_rate, price, market)` :
  - Si `base_fee_rate == Decimal("0")` ou `market is None` → retour
    `Decimal("0")`.
  - Sinon : map `market.fee_type` → `(fee_rate_param, exponent)` :
    - `"crypto_fees_v2"` → `(Decimal("0.25"), 2)` (Crypto, max 1.56% à
      p=0.5 — params skill cache, confirmés via `base_fee=1000` flag commun).
    - `"sports_fees_v2"` → `(Decimal("0.03"), 1)` (Sports post-rollout
      March 30 2026 — params confirmés [docs Polymarket
      live](https://help.polymarket.com/en/articles/13364478-trading-fees)
      fetched 2026-04-25 : peak effective 0.75% à p=0.5).
    - Autre / unknown → fallback **conservateur Crypto** `(Decimal("0.25"),
      2)` (mieux sur-estimer fee).
  - Calcule `effective_rate = fee_rate_param × (price × (Decimal("1") - price)) ** exponent`.
  - Retourne `effective_rate`.
- SELL passthrough : pas de fee adjustment côté `_check_sell` (cf. §11.3
  justification). Décision **D5** : préserver le contrat M13 Bug 5.
- Nouveau reason code `ev_negative_after_fees` dans
  `strategy_decisions.reason` (string libre — pas d'enum strict, déjà des
  reason codes additifs au fil des modules).
- **Attention** : `PipelineContext` étendu avec 3 nouveaux champs optionnels
  `fee_rate`, `fee_cost_usd`, `ev_after_fee_usd` (cf. §6).

**MC.3 — Settings `STRATEGY_MIN_EV_USD_AFTER_FEE` + flag** :

- Nouveau setting Pydantic dans `Settings` :
  ```python
  strategy_min_ev_usd_after_fee: Decimal = Field(
      Decimal("0.05"),
      ge=Decimal("0.01"),
      le=Decimal("10.0"),
      description=(
          "M16 — Seuil EV minimum (USD) post-fees pour approuver un BUY. "
          "Trop bas : on accepte des trades EV-négatifs sous l'effet de "
          "volatility de marché. Trop haut : on rejette trop de trades, "
          "discovery échantillon trop faible. Effective uniquement quand "
          "STRATEGY_FEES_AWARE_ENABLED=true ET un FeeRateClient est injecté "
          "dans PositionSizer. Range [0.01, 10.0] USD."
      ),
  )
  strategy_fees_aware_enabled: bool = Field(
      True,
      description=(
          "M16 — Active le fee adjustment dans PositionSizer._check_buy. "
          "Si false, le pipeline reste strictement M2..M15 (pas de fetch "
          "/fee-rate, pas de soustraction fee de l'EV, pas de reason "
          "ev_negative_after_fees généré). Désactivable pour debug ou "
          "comparaison empirique. Default true post-rollout March 2026."
      ),
  )
  strategy_fee_rate_cache_max: int = Field(
      500,
      ge=10,
      le=10_000,
      description=(
          "M16 — Cap LRU du cache FeeRateClient. Cohérent avec M8 orderbook "
          "et M11 WS market subscriptions. Range [10, 10000]."
      ),
  )
  ```
- `.env.example` à mettre à jour avec bloc commenté :
  ```bash
  # ── M16 — Dynamic taker fees (rollout March 30 2026) ────────────────
  # Seuil EV minimum (USD) post-fees pour approuver un BUY.
  # 0.05 = 5 cents minimum EV post-fee → conservatif sur capital $50-200.
  # STRATEGY_MIN_EV_USD_AFTER_FEE=0.05

  # Active le fee adjustment dans le PositionSizer.
  # Mettre à false pour comparer avec/sans fees en debug ou A/B test.
  # STRATEGY_FEES_AWARE_ENABLED=true

  # Cap LRU du cache fee_rate (60s TTL appliqué automatiquement).
  # STRATEGY_FEE_RATE_CACHE_MAX=500
  ```
- CLAUDE.md §Conventions : ajouter ligne sur le seuil + impact March 2026.

**MC.4 — Dashboard `/strategie` count** :

- **Aucun nouveau code** côté dashboard si la query `/strategie` agrège déjà
  `rejected_by_reason` via un `GROUP BY reason` (cf. §5.4) — la nouvelle
  reason `ev_negative_after_fees` apparaît automatiquement comme une
  nouvelle clé du `Counter`.
- Vérifier que le template `strategy.html` boucle dynamiquement sur
  `rejected_by_reason.items()` (pas de hardcode des reasons existantes).
- Si le template hardcode → **adapter** pour itération dynamique avec
  fallback "—" si count=0 (changement minimal — 1 ligne Jinja).
- **Out of scope MC** : panel "Fee impact" graphique (sum fees × notional,
  breakdown catégorie). Migré en **MH** (UX dashboard détaillée).

**MC.5 — Co-lancement `FeeRateClient` dans `StrategyOrchestrator`** :

- Dans `StrategyOrchestrator.run_forever`, après l'instanciation de
  `gamma_client` et `clob_client` :
  ```python
  fee_rate_client: FeeRateClient | None = None
  if self._settings.strategy_fees_aware_enabled:
      fee_rate_client = FeeRateClient(
          http_client,
          cache_max=self._settings.strategy_fee_rate_cache_max,
          settings=self._settings,
      )
  ```
- Injection dans `run_pipeline` via paramètre optionnel `fee_rate_client`.
- Aucun `await fee_rate_client.close()` explicite nécessaire — le client
  partage le `httpx.AsyncClient` du `StrategyOrchestrator` qui est fermé via
  `async with httpx.AsyncClient()` (pattern existant). Le cache in-memory
  s'efface naturellement à la sortie.
- **Décision D2** : co-lancement dans `StrategyOrchestrator` et **non**
  `ExecutorOrchestrator`. Justification : le fee check vit **avant** l'envoi
  d'order (rejet pre-POST), cohérent avec `SlippageChecker` et toute la
  logique fee-related qui doit infléchir le sizing. Le `ExecutorOrchestrator`
  n'a pas connaissance du `PipelineContext` ni des reason codes strategy.

### 2.2 Hors scope explicites (liste exhaustive)

- ❌ **Maker fees / rebates** : polycopy est taker-only (FOK orders M3 + M8).
  Pas de logique maker-side dans MC. Si jamais maker strategy v∞, spec
  dédiée.
- ❌ **Fee-rate WebSocket** : endpoint REST + cache 60s suffit (rate
  Polymarket ne change pas par seconde). Pas de WSS dédié documenté côté
  Polymarket.
- ❌ **Backtest historique avec fees** : MC gère le live + dry-run. Backtest
  fees historiques = spec future si besoin (par exemple MA SIMULATION mode
  M10).
- ❌ **Dashboard panel "Fee impact" complet** : sum fees attendues 24h,
  breakdown par catégorie Crypto/Sports/Politics, fee_drag % volume — migré
  en **MH** (UX dashboard détaillée). MC livre uniquement le count brut.
- ❌ **Alertes Telegram fee spike** (ex: si fee > 2% inhabituel) : pas v1.
  Ajout futur si observé.
- ❌ **Gestion tier utilisateur Builder Tiers** ([Perplexity B1](../deepsearch/SYNTHESIS-2026-04-24-polycopy-scoring-discovery-latency.md#L143)) :
  on assume Standard tier, pas de tier-specific logic.
- ❌ **Migration Alembic** : MC ne touche aucun schéma DB. La table
  `strategy_decisions` accepte déjà `reason: String(64)` libre — la nouvelle
  valeur `ev_negative_after_fees` (28 chars) rentre sans modif schéma.
- ❌ **Recalibrage `STRATEGY_MAX_ENTRY_PRICE` ou `MAX_SLIPPAGE_PCT`** : la
  spec MC.md mentionne que ces seuils ont été calibrés zero-fee era. Le
  **principe** d'un recalibrage est valide mais hors scope MC livre — MC
  ajoute le seuil EV post-fee qui agit en aval. Si empiriquement (cf.
  H-EMP-10 §6) on observe un blocage massif, recalibrer dans **MH** ou **MI**
  selon urgence.
- ❌ **Lecture `feesEnabled` côté Gamma comme short-circuit** : on pourrait
  économiser un appel `/fee-rate` si `market.fees_enabled is False`, mais
  ça ajoute une dépendance forte au champ Gamma (pas garanti backward-compat
  si Polymarket le rename). Pour v1, on appelle toujours `/fee-rate` (cache
  60s amortit) et on gère le 404/`base_fee=0` en aval. Optimisation MH
  possible.
- ❌ **Re-test des trades existants en dry-run sur 14j** : les trades déjà
  approuvés `M2..M15` ne sont pas re-évalués post-MC. Le compteur démarre à
  0 et croît à partir du commit M16.
- ❌ **Suppression du DataApi `/value` en dry-run pour intégrer fee net** :
  hors scope, le `VirtualWalletStateReader` reste sur la formule M8.

---

## 3. User stories

### 3.1 Story A — MC.2 bloque un BUY EV-négatif sur Crypto

**Avant M16** (2026-04-25 14h) :
- Source wallet `0xe8dd…ec86` BUY 100 shares à $0.50 sur cond X (marché
  crypto `bitcoin-up-or-down-on-april-25-2026`, `feesEnabled=true`).
- Bot copie → BUY 1 share à $0.50 (`copy_ratio=0.01`).
- `PositionSizer._check_buy` : `raw_size=1`, `cap_size=200` → `my_size=1`.
- `RiskManager` : exposition $0.50, capital $1000, OK.
- Order envoyé. Position virtuelle ouverte. **Aucune fee soustraite**.
- 24h plus tard, marché résolu Down (NO wins) → `realized_pnl = 0 - 0.50 = -$0.50`.
- En **réalité live** sur le même trade : fee = 1.56% × $0.50 = **−$0.0078** en
  plus. Soit `realized_pnl_live = -$0.508`. **Différence dry-run vs live =
  $0.008/trade** sur ce micro-trade. Multiplié sur 100 trades crypto / mois :
  **~$0.80 manqué dans la mesure dry-run**. Pour positions plus grosses
  (`MAX_POSITION_USD=200`, BUY 400 shares à $0.50 = $200 notional), fee =
  $3.12 par trade.

**Après M16 MC.2** :
- Bot voit le trade source.
- `PositionSizer._check_buy` :
  - Récupère `base_fee_rate = await fee_client.get_fee_rate(asset_id)` →
    `Decimal("0.10")` (= 1000 bps / 10000, valeur capturée 2026-04-25 sur
    bitcoin-up-or-down).
  - `effective_fee_rate = compute_effective(base, price=0.50, market.fee_type="crypto_fees_v2")`
    = `0.25 × (0.50 × 0.50)^2 = 0.25 × 0.0625 = 0.015625` (= 1.5625%).
  - `raw_my_size = 1`, `notional = 0.50`, `fee_cost = 0.50 × 0.015625 = $0.0078`.
  - `expected_max_gain = 1 × (1 - 0.50) = $0.50`.
  - `ev_after_fee = 0.50 - 0.0078 = $0.4922`.
  - `0.4922 ≥ 0.05` → PASS.
- Order envoyé avec sizing inchangé, mais `ctx.fee_cost_usd = 0.0078` est
  désormais persisté dans `strategy_decisions.pipeline_state`.

**Cas où le rejet déclenche** : BUY 100 shares à $0.97 sur Crypto.
- `raw_size = 1, cap_size = 200/0.97 = 206`, `my_size = 1`.
- `notional = $0.97`, `effective_fee_rate = 0.25 × (0.97 × 0.03)^2 = 0.25 ×
  0.000847 = 0.000212` (= 0.0212%) → `fee_cost = $0.000206`.
- `expected_max_gain = 1 × (1 - 0.97) = $0.03`.
- `ev_after_fee = $0.03 - $0.0002 = $0.0298`.
- `0.0298 < 0.05` → **REJECT** avec `ev_negative_after_fees`. Cohérent : un
  BUY à 0.97 a un upside de 3¢ par share, pas suffisant pour couvrir le
  seuil minimum + un peu de slack.

### 3.2 Story B — MC.4 dashboard surface le count

L'utilisateur visite `/strategie` après 24h post-merge MC :

**Avant M16** :
```
Décisions stratégie (24h)
─────────────────────────
Approuvées : 142
Rejetées :   546
  • liquidity_too_low      → 312
  • slippage_exceeded      → 89
  • position_already_open  → 78
  • entry_price_too_high   → 45
  • market_inactive        → 18
  • orderbook_disabled     → 4
```

**Après M16** :
```
Décisions stratégie (24h)
─────────────────────────
Approuvées : 138
Rejetées :   550
  • liquidity_too_low        → 312
  • slippage_exceeded        → 89
  • position_already_open    → 78
  • entry_price_too_high     → 45
  • ev_negative_after_fees   → 4   ← NEW
  • market_inactive          → 18
  • orderbook_disabled       → 4
```

L'utilisateur voit immédiatement combien de trades ont été bloqués par le
fee adjustment et peut juger l'impact. Si ce nombre dépasse ~30% des
rejets, il sait qu'il faut soit relâcher `STRATEGY_MIN_EV_USD_AFTER_FEE`
soit recalibrer `STRATEGY_MAX_ENTRY_PRICE`.

### 3.3 Story C — MC.1 fallback réseau conservateur

**3h00 UTC** : Polymarket subit une coupure réseau partielle, l'endpoint
`/fee-rate` retourne 503 Service Unavailable.

**Sans fallback conservateur** : `FeeRateClient` raise → `pipeline_error` →
strategy crash en boucle, alertes spam.

**Avec fallback conservateur (MC.1 D3)** :
- `FeeRateClient.get_fee_rate(asset_id)` : tenacity retry 5×, exhausted.
- Catch + log WARNING `fee_rate_fetch_failed_using_conservative_fallback`
  avec `token_id`, `attempts=5`, `error="503 Service Unavailable"`.
- Retourne `Decimal("0.0156")` (= 1.56%, max effective Crypto).
- `_check_buy` continue : effective_fee = 1.56% × notional, EV calculé
  avec ce fee gonflé.
- **Conséquence opérationnelle** : pendant l'outage, les BUY sur n'importe
  quel marché (même fee-free Politics) seront évalués comme s'ils avaient
  des fees Crypto. Une fraction sera rejetée `ev_negative_after_fees` par
  prudence. **Mieux vaut sur-rejeter que sous-trader à perte**.
- Quand l'endpoint revient, le cache se reconstruit, comportement nominal.

### 3.4 Story D — MC.5 boot strict si flag off

L'utilisateur set `STRATEGY_FEES_AWARE_ENABLED=false` dans `.env` (debug A/B).

- `StrategyOrchestrator.run_forever` ne crée pas `FeeRateClient`.
- `run_pipeline(...)` reçoit `fee_rate_client=None`.
- `PositionSizer.__init__` reçoit `fee_rate_client=None`.
- `_check_buy` détecte `self._fee_rate_client is None` → skip fee math,
  comportement strict M2..M15.
- Aucun appel HTTP `/fee-rate`. Aucun reason `ev_negative_after_fees`
  généré. Compteur dashboard reste à 0.
- Comportement back-compat strict — utile pour valider empiriquement
  l'impact du flag en comparant 2 runs (1 avec, 1 sans).

---

## 4. Architecture

### 4.1 Flux global M16 (5 sujets)

```
                        ┌─────────────────────────────────────────────┐
                        │  StrategyOrchestrator.run_forever (M2)       │
                        │                                              │
                        │  http_client = httpx.AsyncClient()           │
                        │  gamma_client = GammaApiClient(http)         │
                        │  clob_client  = ClobReadClient(http)         │
                        │  fee_client   = FeeRateClient(http)  ◀── MC.5│
                        │  ws_client    = ClobMarketWSClient (M11)     │
                        │                                              │
                        └──────────────┬──────────────────────────────┘
                                       │ DetectedTradeDTO
                                       ▼
                        ┌─────────────────────────────────────────────┐
                        │  run_pipeline(... fee_rate_client=...)       │
                        │                                              │
                        │  TraderLifecycleFilter (M5_bis)              │
                        │  MarketFilter (M2 — sets ctx.market)         │
                        │  EntryPriceFilter (M13 bug 4)                │
                        │  PositionSizer.check                         │
                        │      │                                        │
                        │      ├── BUY → _check_buy ◀── MC.2           │
                        │      │      │                                 │
                        │      │      ├── existing position? → reject   │
                        │      │      │                                 │
                        │      │      ├── fee_client.get_fee_rate(asset)│
                        │      │      │      │                          │
                        │      │      │      ├── cache hit? → return    │
                        │      │      │      ├── single-flight Lock     │
                        │      │      │      ├── HTTP /fee-rate         │
                        │      │      │      │  + tenacity retry        │
                        │      │      │      │                          │
                        │      │      │      └── return Decimal(bps/10k)│
                        │      │      │                                 │
                        │      │      ├── compute_effective_fee_rate    │
                        │      │      │      │                          │
                        │      │      │      ├── crypto_fees_v2 → exp=2 │
                        │      │      │      ├── sports_fees_v1 → exp=1 │
                        │      │      │      └── fallback Crypto        │
                        │      │      │                                 │
                        │      │      ├── ev_after_fee < threshold      │
                        │      │      │   → REJECT ev_negative_after_fees│
                        │      │      │                                 │
                        │      │      └── PASS, my_size committed       │
                        │      │                                        │
                        │      └── SELL → _check_sell (M13 bug 5,       │
                        │                  inchangé, pas de fee adj)    │
                        │                                                │
                        │  SlippageChecker (M2 + M11 ws)                 │
                        │  RiskManager (M2)                              │
                        │                                                │
                        └──────────────┬───────────────────────────────┘
                                       │ APPROVED / REJECTED + ctx
                                       ▼
                        ┌─────────────────────────────────────────────┐
                        │  StrategyDecisionRepository.insert           │
                        │  pipeline_state JSON inclut désormais :      │
                        │    fee_rate, fee_cost_usd, ev_after_fee_usd  │
                        └──────────────┬───────────────────────────────┘
                                       │
                                       ▼
                        ┌─────────────────────────────────────────────┐
                        │  Dashboard /strategie queries.py             │
                        │  GROUP BY reason (existant)                  │
                        │  ev_negative_after_fees apparaît auto ◀── MC.4│
                        └──────────────────────────────────────────────┘
```

### 4.2 Fichiers touchés

Tous les changements sont **additifs** ou **in-place** dans des fichiers
existants. **Un seul nouveau module**.

| Module | Type de changement | Lignes estimées |
|---|---|---|
| [src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py) | **Nouveau fichier** : client async + cache + LRU + single-flight + tenacity | +180 / -0 |
| [src/polycopy/strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py) | `PositionSizer.__init__` +1 param ; `_check_buy` étendu fee math + helper privé | +60 / -5 |
| [src/polycopy/strategy/dtos.py](../../src/polycopy/strategy/dtos.py) | `PipelineContext` +3 champs optionnels (fee_rate, fee_cost_usd, ev_after_fee_usd) | +10 / -0 |
| [src/polycopy/strategy/orchestrator.py](../../src/polycopy/strategy/orchestrator.py) | Instanciation conditionnelle `FeeRateClient` + injection dans `run_pipeline` | +15 / -2 |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +3 settings (`strategy_min_ev_usd_after_fee`, `strategy_fees_aware_enabled`, `strategy_fee_rate_cache_max`) | +30 / -0 |
| [.env.example](../../.env.example) | Bloc M16 commenté | +10 / -0 |
| [src/polycopy/dashboard/templates/strategy.html](../../src/polycopy/dashboard/templates/strategy.html) | **Possiblement aucun changement** si template déjà boucle dynamique. Sinon : 1 ligne Jinja | 0 ou +1 |
| [CLAUDE.md](../../CLAUDE.md) | §Conventions + §Sécurité — bloc M16 fees | +12 / -0 |
| `tests/fixtures/clob_fee_rate_*.json` | **3 fichiers déjà capturés 2026-04-25** : crypto, zero, invalid | +3 / -0 |
| `tests/fixtures/gamma_market_crypto_fees_sample.json` | **1 fichier déjà capturé 2026-04-25** | +1 / -0 |
| tests/unit/ | +13 tests ciblés (FeeRateClient + PositionSizer EV) | +400 / -0 |
| tests/unit/ | +1 test dashboard (count count visible) | +30 / -0 |

### 4.3 Dépendances avec autres milestones

- **M3 Executor** : aucun changement. Le triple garde-fou est intact :
  `FeeRateClient` n'instancie pas `ClobClient`, ne touche pas `WalletStateReader`,
  ne signe rien.
- **M5 / M5_bis / M5_ter** : aucune modification.
- **M11 latency stages** : optionnellement, on peut ajouter un nouveau stage
  `strategy_fee_fetched_ms` mesuré autour de `fee_rate_client.get_fee_rate`
  dans `_check_buy`. **Décision D6** : pas v1 (économise complexité, le coût
  fee fetch sera visible dans `strategy_sized_ms` cumulatif anyway). Migrer
  vers stage propre si latence problématique observée.
- **M12 / M14 scoring** : aucune interaction. Le score d'un wallet ne
  dépend pas de notre fee.
- **M13 dry-run** : Bug 5 SELL passthrough préservé. Le fee math n'impacte
  que `_check_buy`, donc les SELL copiés ferment toujours leurs positions
  identiquement.
- **MA (M14)** / **MB (M15)** / **MD-MJ (M17+)** : indépendants — peuvent
  shipper en parallèle.

---

## 5. Algorithmes

### 5.1 MC.1 — `FeeRateClient` design détaillé

**Fichier** : nouveau
[src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py).

**Squelette** :

```python
"""Client async read-only pour l'endpoint Polymarket /fee-rate.

Endpoint public no-auth :
    GET https://clob.polymarket.com/fee-rate?token_id=<asset_id>

Réponse 200 : ``{"base_fee": <int>}`` où ``base_fee`` est exprimé en
**basis points** (entier, 1 bp = 0.01 %). Marchés fee-free (vaste majorité
Polymarket hors Crypto + NCAAB + Serie A) renvoient ``{"base_fee": 0}``.
Marchés fee-enabled (ex: ``feeType="crypto_fees_v2"``) renvoient une
valeur > 0 (ex: 1000 bps capturé sur ``bitcoin-up-or-down-on-april-25-2026``
en avril 2026).

Cf. spec [docs/specs/M16-dynamic-fees-ev.md](docs/specs/M16-dynamic-fees-ev.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)

# Constants
_CACHE_TTL = timedelta(seconds=60)
_CONSERVATIVE_FALLBACK_RATE = Decimal("0.018")
"""Max effective rate observé post-rollout March 30 2026 ([Perplexity C4
+ docs Polymarket](https://help.polymarket.com/en/articles/13364478-trading-fees)).
La doc skill cache (mars 22) annonce 1.56% Crypto / 0.44% Sports,
mais la doc live confirme un bump à ~1.80% post-rollout sur certaines
configurations. On prend le worst-case 1.80% — better over-estimate than
under-estimate when the API is unreachable (asymmetric impact, décision
D3 spec M16)."""


class FeeRateClient:
    """Client async pour ``GET /fee-rate?token_id=...``.

    Cache TTL 60 s (cohérent Gamma M2). LRU cap configurable. Single-flight
    pattern pour éviter N fetches concurrents sur le même token_id (TOCTOU
    fix préventif — audit M-007).

    Exemple :

        >>> async with httpx.AsyncClient() as http:
        ...     client = FeeRateClient(http, cache_max=500, settings=settings)
        ...     rate = await client.get_fee_rate("3417...")  # Decimal("0.10")
    """

    BASE_URL = "https://clob.polymarket.com"
    DEFAULT_TIMEOUT = 5.0  # cohérent ClobReadClient (prix temps réel)

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        cache_max: int = 500,
        settings: Settings | None = None,
    ) -> None:
        self._http = http_client
        self._cache_max = cache_max
        self._cache: OrderedDict[str, tuple[Decimal, datetime]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[Decimal]] = {}
        self._settings = settings  # reservé pour LATENCY_INSTRUMENTATION ext.

    async def get_fee_rate(self, token_id: str) -> Decimal:
        """Retourne le ``base_fee`` en Decimal ∈ [0, 1] (= bps / 10000).

        Path :
        1. Cache hit (TTL 60 s) → retour immédiat.
        2. Single-flight inflight → await le Future en cours pour ce token_id.
        3. Sinon : fetch HTTP, parse, cache, retour.

        Erreurs :
        - HTTP 404 / `fee rate not found` → `Decimal("0")` (marché fee-free).
        - HTTP 400 / `Invalid token id` → fallback conservateur 1.56 %.
        - 5xx ou TransportError post-tenacity → fallback conservateur 1.56 %.
        """
        now = datetime.now(tz=UTC)
        cached = self._cache.get(token_id)
        if cached is not None and (now - cached[1]) < _CACHE_TTL:
            self._cache.move_to_end(token_id)
            return cached[0]

        existing = self._inflight.get(token_id)
        if existing is not None:
            return await existing

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Decimal] = loop.create_future()
        self._inflight[token_id] = fut
        try:
            rate = await self._fetch_and_cache(token_id, now)
            fut.set_result(rate)
            return rate
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(token_id, None)

    async def _fetch_and_cache(self, token_id: str, now: datetime) -> Decimal:
        try:
            payload = await self._fetch(token_id)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                log.debug("fee_rate_market_not_found", token_id=token_id)
                rate = Decimal("0")
            elif status == 400:
                log.warning(
                    "fee_rate_invalid_token_id",
                    token_id=token_id,
                    body=exc.response.text[:128],
                )
                rate = _CONSERVATIVE_FALLBACK_RATE
            else:
                log.warning(
                    "fee_rate_fetch_failed_using_conservative_fallback",
                    token_id=token_id,
                    status=status,
                    error=str(exc)[:128],
                )
                rate = _CONSERVATIVE_FALLBACK_RATE
        except httpx.TransportError as exc:
            log.warning(
                "fee_rate_fetch_failed_using_conservative_fallback",
                token_id=token_id,
                error=type(exc).__name__,
            )
            rate = _CONSERVATIVE_FALLBACK_RATE
        else:
            base_fee_bps = int(payload.get("base_fee", 0))
            rate = Decimal(base_fee_bps) / Decimal(10_000)
            log.debug(
                "fee_rate_fetched",
                token_id=token_id,
                base_fee_bps=base_fee_bps,
                rate=str(rate),
            )

        self._cache[token_id] = (rate, now)
        self._cache.move_to_end(token_id)
        # LRU eviction.
        while len(self._cache) > self._cache_max:
            evicted_token, _ = self._cache.popitem(last=False)
            log.debug("fee_rate_cache_lru_evicted", token_id=evicted_token)
        return rate

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch(self, token_id: str) -> dict[str, int]:
        response = await self._http.get(
            f"{self.BASE_URL}/fee-rate",
            params={"token_id": token_id},
            timeout=self.DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        return data
```

**Notes algorithmiques** :

- **Cache hit** : `move_to_end` met à jour l'ordre LRU (touch).
- **Single-flight** : si 2 coroutines appellent `get_fee_rate` pour le même
  `token_id` simultanément, la 2ᵉ await le Future de la 1ʳᵉ. Une seule
  requête HTTP émise, deux callers reçoivent le même résultat. Évite spam
  inutile sur Polymarket.
- **LRU eviction** : à l'insertion, si `len(cache) > cache_max`, on pop le
  plus ancien (LIFO sur OrderedDict via `popitem(last=False)`).
- **Fallback conservateur centralisé** : `_CONSERVATIVE_FALLBACK_RATE =
  Decimal("0.0156")` (1.56 %, max effective Crypto par docs officielles).
  **Ne pas** utiliser 1.80 % comme MC.md le suggère initialement — c'est
  une valeur Perplexity overestimated qui ne matche pas la doc Polymarket.
- **Tenacity sur `TransportError` uniquement** : on ne retry pas les 4xx
  (Invalid token id ne va pas devenir valide en réessayant).

### 5.2 MC.2 — `PositionSizer._check_buy` EV-aware

**Fichier** : [src/polycopy/strategy/pipeline.py:177-191](../../src/polycopy/strategy/pipeline.py#L177-L191).

**Comportement actuel** (M13 bug 5, side-aware sans fee) :

```python
async def _check_buy(self, ctx: PipelineContext) -> FilterResult:
    async with self._session_factory() as session:
        stmt = select(MyPosition).where(
            MyPosition.condition_id == ctx.trade.condition_id,
            MyPosition.closed_at.is_(None),
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return FilterResult(passed=False, reason="position_already_open")
    raw_size = ctx.trade.size * self._settings.copy_ratio
    cap_size = self._settings.max_position_usd / ctx.trade.price if ctx.trade.price > 0 else 0.0
    ctx.my_size = min(raw_size, cap_size)
    if ctx.my_size <= 0:
        return FilterResult(passed=False, reason="size_zero")
    return FilterResult(passed=True)
```

**Comportement nouveau M16** :

```python
async def _check_buy(self, ctx: PipelineContext) -> FilterResult:
    async with self._session_factory() as session:
        stmt = select(MyPosition).where(
            MyPosition.condition_id == ctx.trade.condition_id,
            MyPosition.closed_at.is_(None),
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return FilterResult(passed=False, reason="position_already_open")

    raw_size = ctx.trade.size * self._settings.copy_ratio
    cap_size = (
        self._settings.max_position_usd / ctx.trade.price if ctx.trade.price > 0 else 0.0
    )
    raw_my_size = min(raw_size, cap_size)
    if raw_my_size <= 0:
        return FilterResult(passed=False, reason="size_zero")

    # M16 : fee adjustment (opt-in via STRATEGY_FEES_AWARE_ENABLED).
    if (
        self._fee_rate_client is None
        or not self._settings.strategy_fees_aware_enabled
    ):
        ctx.my_size = raw_my_size
        return FilterResult(passed=True)

    base_fee_rate = await self._fee_rate_client.get_fee_rate(ctx.trade.asset_id)
    effective_fee_rate = self._compute_effective_fee_rate(
        base_fee_rate=base_fee_rate,
        price=Decimal(str(ctx.trade.price)),
        market=ctx.market,
    )
    notional = Decimal(str(raw_my_size)) * Decimal(str(ctx.trade.price))
    fee_cost = notional * effective_fee_rate
    expected_max_gain = Decimal(str(raw_my_size)) * (
        Decimal("1.0") - Decimal(str(ctx.trade.price))
    )
    ev_after_fee = expected_max_gain - fee_cost

    ctx.fee_rate = float(effective_fee_rate)
    ctx.fee_cost_usd = float(fee_cost)
    ctx.ev_after_fee_usd = float(ev_after_fee)

    if ev_after_fee < self._settings.strategy_min_ev_usd_after_fee:
        return FilterResult(passed=False, reason="ev_negative_after_fees")

    ctx.my_size = raw_my_size
    return FilterResult(passed=True)


@staticmethod
def _compute_effective_fee_rate(
    *,
    base_fee_rate: Decimal,
    price: Decimal,
    market: MarketMetadata | None,
) -> Decimal:
    """Calcule l'effective fee rate via formule Polymarket officielle.

    ``fee = C × p × feeRate × (p × (1-p))^exponent`` →
    ``effective_rate = feeRate × (p × (1-p))^exponent`` (ratio fee/notional).

    Mapping ``market.fee_type`` → ``(feeRate_param, exponent)`` :
    - ``crypto_fees_v2`` : (0.25, 2) — max 1.56 % à p=0.5
    - ``sports_fees_v1``, ``ncaab_fees_*``, ``seriea_fees_*`` : (0.0175, 1)
    - autre / unknown / market None : fallback **conservateur Crypto** (0.25, 2)

    Fallbacks sont **délibérément conservateurs** : mieux sur-estimer fee
    et rejeter un bon trade que sous-estimer et trader à perte (asymétrie
    d'impact, décision D3 spec M16).

    Si ``base_fee_rate == 0`` → marché fee-free, retour Decimal("0") direct
    (court-circuit le calcul formule).
    """
    if base_fee_rate == Decimal("0"):
        return Decimal("0")

    fee_type = (market.fee_type if market is not None else None) or ""
    if fee_type == "crypto_fees_v2":
        fee_rate_param, exponent = Decimal("0.25"), 2
    elif fee_type.startswith("sports_fees"):
        # Post-March 30 2026 rollout : feeRate=0.03 (vs 0.0175 pré-rollout
        # NCAAB/Serie A). Source : docs Polymarket live 2026-04-25.
        fee_rate_param, exponent = Decimal("0.03"), 1
    else:
        # Inconnu (politics_fees_v*, finance_fees_v*, etc. à venir) →
        # conservateur (Crypto formula). Mieux sur-estimer fee que l'inverse.
        fee_rate_param, exponent = Decimal("0.25"), 2

    p_one_minus_p = price * (Decimal("1") - price)
    return fee_rate_param * (p_one_minus_p ** exponent)
```

**Notes algorithmiques** :

- `_compute_effective_fee_rate` est `@staticmethod` car ne dépend pas de
  `self` — testable isolément.
- **Court-circuit `base_fee_rate == 0`** : la majorité des markets renvoie
  0, on évite la lookup `fee_type`.
- **Mapping `fee_type`** : `startswith()` pour absorber les variantes
  (`sports_fees_v1`, `ncaab_fees_v2`, etc.). Robuste aux renames Polymarket.
- **Approximation EV** : `expected_max_gain = my_size × (1 - price)` est un
  proxy pessimiste — c'est le **payout maximum si YES wins**, pas l'EV
  Bayésienne `prob × payout`. Justification §11.4 (alternative et pourquoi
  on garde simple).

### 5.3 MC.3 — Settings Pydantic Decimal

**Fichier** : `src/polycopy/config.py`.

**Décision Decimal vs float** : `STRATEGY_MIN_EV_USD_AFTER_FEE` est en
`Decimal` (cohérent CLAUDE.md "Decimal interne, float à la persistance").
Pydantic v2 sérialise `Decimal` en JSON nativement. Validator `ge/le` accepte
des bornes Decimal.

```python
strategy_min_ev_usd_after_fee: Decimal = Field(
    Decimal("0.05"),
    ge=Decimal("0.01"),
    le=Decimal("10.0"),
    description=(...)
)
```

**Risque mypy strict** : `Decimal` n'est pas un type reconnu nativement par
Pydantic 2 sans extension. Cf. `from decimal import Decimal` import en tête
du fichier `config.py`. Vérifier `mypy --strict` post-merge.

### 5.4 MC.4 — Dashboard count automatique

**Fichier** : [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py).

**Requête actuelle** (à confirmer pendant l'implémentation) :

```python
async def get_strategy_stats(...) -> StrategyStats:
    ...
    rejected_rows = (
        await session.execute(
            select(StrategyDecision.reason, func.count())
            .where(StrategyDecision.decision == "REJECTED")
            .group_by(StrategyDecision.reason)
        )
    ).all()
    rejected_by_reason = {reason: count for reason, count in rejected_rows}
    ...
```

Si la requête est bien un `GROUP BY reason` libre, la nouvelle clé
`ev_negative_after_fees` apparaît automatiquement comme une nouvelle entrée
du dict. **Aucune modification code nécessaire.**

**Vérifier le template** :

```jinja
{# strategy.html partial - boucle dynamique recommandée #}
{% for reason, count in stats.rejected_by_reason.items() %}
  <tr>
    <td>{{ reason }}</td>
    <td>{{ count }}</td>
  </tr>
{% endfor %}
```

Si le template hardcode les reasons (improbable, vu le pattern observé
dans M13), adapter pour itération dynamique. **Test à ajouter** :
`test_strategy_dashboard_shows_ev_negative_rejection_count` (cf. §9.4).

### 5.5 MC.5 — `StrategyOrchestrator` co-lancement

**Fichier** : [src/polycopy/strategy/orchestrator.py:75-90](../../src/polycopy/strategy/orchestrator.py#L75-L90).

**Diff** :

```python
async with httpx.AsyncClient() as http_client:
    gamma_client = GammaApiClient(http_client, settings=self._settings)
    clob_client = ClobReadClient(http_client)

    # M16 : FeeRateClient opt-in via STRATEGY_FEES_AWARE_ENABLED.
    fee_rate_client: FeeRateClient | None = None
    if self._settings.strategy_fees_aware_enabled:
        fee_rate_client = FeeRateClient(
            http_client,
            cache_max=self._settings.strategy_fee_rate_cache_max,
            settings=self._settings,
        )

    if self._settings.strategy_clob_ws_enabled:
        self._ws_client = ClobMarketWSClient(self._settings)

    async with asyncio.TaskGroup() as tg:
        if self._ws_client is not None:
            tg.create_task(
                self._ws_client.run(stop_event),
                name="clob_ws_client",
            )
        tg.create_task(
            self._consume_loop(stop_event, gamma_client, clob_client, fee_rate_client),
            name="strategy_consumer",
        )
```

`_consume_loop` reçoit `fee_rate_client` et le passe à `_handle_trade`, qui
le passe à `run_pipeline`. `run_pipeline` le passe au constructeur
`PositionSizer`.

---

## 6. DTOs / signatures

### 6.1 `PipelineContext` étendu

[src/polycopy/strategy/dtos.py](../../src/polycopy/strategy/dtos.py)
actuellement :

```python
@dataclass
class PipelineContext:
    trade: DetectedTradeDTO
    market: MarketMetadata | None = None
    midpoint: float | None = None
    my_size: float | None = None
    slippage_pct: float | None = None
    rejection_reason: str | None = None
    ...
```

Nouveau (ajout en fin de dataclass) :

```python
@dataclass
class PipelineContext:
    ...
    rejection_reason: str | None = None
    # --- M16 — fee-aware sizing (defaults None pour backward-compat) ---
    fee_rate: float | None = None
    """Effective fee rate appliqué (Decimal converti, range [0, 0.0156])."""
    fee_cost_usd: float | None = None
    """Fee cost USD calculé (notional × fee_rate)."""
    ev_after_fee_usd: float | None = None
    """EV USD post-fee approximé (max_gain - fee_cost)."""
```

Defaults non-cassants pour les tests M2..M15 qui instancient directement
`PipelineContext` sans les nouveaux champs.

`to_audit_dict()` (déjà existant) sérialise automatiquement ces 3 champs
dans le `pipeline_state` JSON de `strategy_decisions`.

### 6.2 Signature `PositionSizer.__init__`

Actuelle :

```python
class PositionSizer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None: ...
```

Nouvelle :

```python
class PositionSizer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        fee_rate_client: FeeRateClient | None = None,  # M16
    ) -> None: ...
```

`fee_rate_client=None` → fallback comportement M2..M15 strict
(décision **D1** rétrocompat tests).

### 6.3 Signature `run_pipeline`

Actuelle :

```python
async def run_pipeline(
    trade: DetectedTradeDTO,
    *,
    gamma_client: GammaApiClient,
    clob_client: ClobReadClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    ws_client: ClobMarketWSClient | None = None,
    latency_repo: TradeLatencyRepository | None = None,
) -> tuple[Literal["APPROVED", "REJECTED"], str | None, PipelineContext]: ...
```

Nouvelle :

```python
async def run_pipeline(
    trade: DetectedTradeDTO,
    *,
    gamma_client: GammaApiClient,
    clob_client: ClobReadClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    ws_client: ClobMarketWSClient | None = None,
    latency_repo: TradeLatencyRepository | None = None,
    fee_rate_client: FeeRateClient | None = None,  # M16
) -> tuple[Literal["APPROVED", "REJECTED"], str | None, PipelineContext]: ...
```

Et la liste `filters` :

```python
filters = (
    ("TraderLifecycleFilter", TraderLifecycleFilter(session_factory, settings)),
    ("MarketFilter", MarketFilter(gamma_client, settings)),
    ("EntryPriceFilter", EntryPriceFilter(settings)),
    ("PositionSizer", PositionSizer(session_factory, settings, fee_rate_client)),  # M16 +1 arg
    ("SlippageChecker", SlippageChecker(clob_client, settings, ws_client)),
    ("RiskManager", RiskManager(session_factory, settings)),
)
```

### 6.4 Signature `FeeRateClient`

Cf. §5.1 squelette complet.

Méthodes publiques :
- `__init__(self, http_client: httpx.AsyncClient, *, cache_max: int = 500, settings: Settings | None = None) -> None`
- `async def get_fee_rate(self, token_id: str) -> Decimal`

Pas de méthode `close()` — partage le `httpx.AsyncClient` du caller.

### 6.5 Nouveaux reason codes strategy

- `ev_negative_after_fees` : nouveau, filtre `PositionSizer._check_buy`. Émis
  uniquement quand `STRATEGY_FEES_AWARE_ENABLED=true` ET `fee_rate_client`
  injecté ET `ev_after_fee < strategy_min_ev_usd_after_fee`.

### 6.6 `MarketMetadata` — déjà compatible

Le DTO Gamma actuel `MarketMetadata` a `extra="allow"` → `feeType` (ou
`fee_type` après alias) peut être lu sans modif schéma. Ajouter le champ
explicitement pour mypy strict :

```python
class MarketMetadata(BaseModel):
    ...
    fee_type: str | None = Field(default=None, alias="feeType")
    fees_enabled: bool | None = Field(default=None, alias="feesEnabled")
```

**Cf. §11.6** : justification de l'ajout explicite vs `extra="allow"` pur.

---

## 7. Settings

3 nouvelles env vars (toutes opt-in / backwards compat strictes) :

| Variable env | Champ Settings | Default | Description |
|---|---|---|---|
| `STRATEGY_MIN_EV_USD_AFTER_FEE` | `strategy_min_ev_usd_after_fee: Decimal` | `Decimal("0.05")` | Seuil EV USD post-fee minimum pour PASS BUY. Range `[0.01, 10.0]`. Si `< seuil`, REJECT `ev_negative_after_fees`. Décision empirique : 5¢ ≈ 0.025-0.10% du notional sur capital $50-200 (cohérent micro-trades polycopy). |
| `STRATEGY_FEES_AWARE_ENABLED` | `strategy_fees_aware_enabled: bool` | `True` | Active le fee adjustment dans `PositionSizer._check_buy`. Si `false`, comportement M2..M15 strict (pas de fetch `/fee-rate`, pas de soustraction fee, pas de reason `ev_negative_after_fees`). Désactivable pour debug ou A/B test. |
| `STRATEGY_FEE_RATE_CACHE_MAX` | `strategy_fee_rate_cache_max: int` | `500` | Cap LRU du cache `FeeRateClient` (TTL 60s). Cohérent avec M8 orderbook et M11 WS subscriptions. Range `[10, 10000]`. |

**`.env.example`** à mettre à jour avec :

```bash
# ── M16 — Dynamic taker fees (rollout March 30 2026) ────────────────
# Polymarket charge des fees taker dynamiques sur Crypto + NCAAB + Serie A.
# Formule officielle : fee = C × p × feeRate × (p × (1-p))^exponent
#   Crypto    : feeRate=0.25, exponent=2, max effective 1.56% à p=0.5
#   NCAAB/SerieA: feeRate=0.0175, exponent=1, max effective 0.44% à p=0.5
#   Autres    : fee-free (Politics, Tech, Finance, Economics, etc.)
#
# Le PositionSizer calcule l'EV post-fee et rejette les BUY EV-négatifs.
# Cf. spec docs/specs/M16-dynamic-fees-ev.md

# Seuil EV minimum (USD) post-fees pour approuver un BUY.
# Default 0.05 = 5 cents ; range [0.01, 10.0].
# STRATEGY_MIN_EV_USD_AFTER_FEE=0.05

# Active le fee adjustment dans PositionSizer._check_buy.
# Mettre à false pour comparer avec/sans fees en debug ou A/B test.
# STRATEGY_FEES_AWARE_ENABLED=true

# Cap LRU du cache fee_rate (TTL 60s appliqué automatiquement).
# STRATEGY_FEE_RATE_CACHE_MAX=500
```

Aucune autre variable ne change de default — backwards compat stricte.

---

## 8. Invariants sécurité

### 8.1 Triple garde-fou M3 préservé

**Confirmer** : aucun des 5 sujets M16 ne touche au chemin live :

- MC.1 : `FeeRateClient` est read-only public no-auth, **n'instancie pas**
  `ClobClient`, ne touche aucune cred CLOB L1/L2.
- MC.2 : modifie `PositionSizer` côté strategy. La strategy reste
  read-only à M2 et M16, aucune signature, aucun POST CLOB.
- MC.3 : settings additives, aucun impact creds.
- MC.4 : query dashboard read-only sur DB locale.
- MC.5 : co-lancement orchestrator strategy. Le `FeeRateClient` partage le
  `httpx.AsyncClient` Strategy (pas Executor — pas de partage avec le path
  signed POST).

**Aucune nouvelle surface de signature, aucun nouveau cred consommé**. Les
quatre garde-fous Executor M3 (lazy init `ClobClient`, RuntimeError si
`dry_run=false` + creds absents, double check avant `create_and_post_order`,
`WalletStateReader` re-fetch) restent inchangés.

### 8.2 4ᵉ garde-fou M8 préservé

Le path `_persist_realistic_simulated` est **après** `PositionSizer`, donc le
fee check arrive en amont. L'`assert settings.execution_mode == "dry_run"`
reste en place.

Les SELL orders traversent `_check_sell` qui n'invoque **pas** le fee path
(décision **D5**) — donc même en dry-run, les SELL ferment leurs positions
sans fee adjustment côté pipeline. Cohérent avec le contrat M13 Bug 5.

### 8.3 Aucun secret loggé

`FeeRateClient` log uniquement :
- `token_id` (asset_id public on-chain)
- `base_fee_bps` (entier)
- `rate` (Decimal sérialisé)
- `cache_hit`, `cache_miss`, `lru_evicted` (debug)
- `error` truncated à 128 chars (jamais de full body avec token L2 leak)

**Test ajouté** : `test_fee_rate_client_no_secret_leak_in_logs` — capture
structlog events sur 100 calls et grep contre `POLYMARKET_PRIVATE_KEY`,
`TELEGRAM_BOT_TOKEN`, `CLOB_API_SECRET`, `REMOTE_CONTROL_TOTP_SECRET`.

### 8.4 Lecture seule API publique

L'endpoint `GET /fee-rate` est **public no-auth**. Pas de surface creds
ajoutée. Cohérent invariant M2 (Strategy read-only).

### 8.5 Decimal strict, pas de mixing float

Toute la logique fee utilise `Decimal` (cf. CLAUDE.md convention M8 "Decimal
interne, float à la persistance"). Conversion `float → Decimal` via
`Decimal(str(value))` (jamais `Decimal(float)` qui crée des artefacts
d'arrondi binaire).

Final : `ctx.fee_rate`, `ctx.fee_cost_usd`, `ctx.ev_after_fee_usd` reste
`float` (M8 convention persistence). Conversion Decimal → float au moment
de l'assignement à `ctx`.

### 8.6 Reason code `ev_negative_after_fees` n'expose aucun PII

Le reason code est une string littérale. Les logs structlog et la table
`strategy_decisions` stockent `(tx_hash, wallet_address, decision, reason)` —
tous champs publics on-chain. Aucun PII introduit.

### 8.7 Fixture `clob_fee_rate_*.json` non sensible

Fixtures capturées uniquement données publiques :
- `base_fee` entier
- `error` string standard

Aucune adresse wallet, aucun token L2, aucune signature.

### 8.8 Single-flight pattern n'introduit pas de leak

Le `dict[str, asyncio.Future[Decimal]]` `_inflight` ne stocke que `token_id`
(asset_id public) → `Future`. Pas de creds, pas de PII. Le Future est
résolu et popé immédiatement après le fetch — aucune accumulation
mémoire. Test : `test_fee_rate_client_inflight_dict_drained_post_fetch`.

---

## 9. Test plan

### 9.1 MC.1 — `FeeRateClient` (5 tests)

Tous dans nouveau fichier
[tests/unit/test_fee_rate_client.py](../../tests/unit/test_fee_rate_client.py).
Utiliser `respx` pour mocker `httpx`.

1. **`test_fee_rate_client_returns_decimal_from_bps`** — happy path crypto.
   - Preconditions : `respx.get("https://clob.polymarket.com/fee-rate?token_id=ABC")` returns `{"base_fee": 1000}` (= fixture `clob_fee_rate_crypto_sample.json`).
   - Action : `await client.get_fee_rate("ABC")`.
   - Assertion : retour `Decimal("0.10")` (= 1000 / 10000).

2. **`test_fee_rate_client_returns_zero_for_fee_free_market`** — fee-free.
   - Preconditions : respx returns `{"base_fee": 0}`.
   - Action : idem.
   - Assertion : retour `Decimal("0")`.

3. **`test_fee_rate_client_caches_60s`** — cache hit.
   - Preconditions : 1 respx mock.
   - Action : 2 calls successifs au même token_id.
   - Assertion : 1 seule requête HTTP émise, 2ᵉ retour identique.
   - **Bonus** : freezegun avance le temps de 61s → 2ᵉ fetch déclenché.

4. **`test_fee_rate_client_fallback_on_network_error`** — fallback conservateur.
   - Preconditions : respx raise `httpx.ConnectError`.
   - Action : `await client.get_fee_rate("ABC")`.
   - Assertion : retour `Decimal("0.018")` (1.80 % worst-case post-rollout).
   - Vérifier log WARNING `fee_rate_fetch_failed_using_conservative_fallback`.

5. **`test_fee_rate_client_single_flight_prevents_redundant_fetches`** — TOCTOU fix.
   - Preconditions : 1 respx mock avec délai 100 ms (`asyncio.sleep`).
   - Action : `asyncio.gather(client.get_fee_rate("ABC"), client.get_fee_rate("ABC"), client.get_fee_rate("ABC"))`.
   - Assertion : 1 seule requête HTTP émise, les 3 callers reçoivent le même Decimal.

6. **(Bonus) `test_fee_rate_client_lru_cap_eviction`** — LRU.
   - Preconditions : `cache_max=2`, 3 tokens A/B/C.
   - Action : `get_fee_rate("A")`, `get_fee_rate("B")`, `get_fee_rate("C")` puis re-`get_fee_rate("A")`.
   - Assertion : 4 requêtes HTTP au total (A évincé après B/C, refetch).

7. **(Bonus) `test_fee_rate_client_400_invalid_token_returns_fallback`** — HTTP 400.
   - Preconditions : respx returns HTTP 400 + body `{"error": "Invalid token id"}`.
   - Action : `await client.get_fee_rate("INVALID")`.
   - Assertion : retour `Decimal("0.018")` (fallback) + log WARNING.

8. **(Bonus) `test_fee_rate_client_no_secret_leak_in_logs`** — sécurité §8.3.
   - Preconditions : 100 calls avec mocks variés (200, 400, 404, 5xx).
   - Action : capture tous les events structlog via `capsys` ou wrapper.
   - Assertion : aucun event ne contient `POLYMARKET_PRIVATE_KEY`,
     `TELEGRAM_BOT_TOKEN`, `CLOB_API_SECRET`, `REMOTE_CONTROL_TOTP_SECRET`.

### 9.2 MC.2 — `PositionSizer` EV-aware (5 tests)

Tous dans
[tests/unit/test_strategy_pipeline.py](../../tests/unit/test_strategy_pipeline.py)
(extension du fichier existant). Réutiliser fixtures `_trade()`, `_settings()`.

9. **`test_position_sizer_subtracts_fee_from_ev`** — happy path approve.
   - Preconditions : `MyPosition` vide, BUY YES @ $0.30 size 100, copy_ratio 0.01, max_position $200, fee_client mock returns Decimal("0.10"), market.fee_type="crypto_fees_v2".
   - Action : `_check_buy(ctx)`.
   - Assertion : `passed=True`, `ctx.my_size == 1.0`,
     `ctx.fee_rate ≈ 0.0525` (0.25 × (0.30 × 0.70)^2 = 0.25 × 0.0441 = 0.01103),
     `ctx.fee_cost_usd ≈ 0.0033`, `ctx.ev_after_fee_usd ≈ 0.6967`.

   *Note*: 0.25 × (0.30 × 0.70)^2 = 0.25 × (0.21)^2 = 0.25 × 0.0441 = 0.011025 (= 1.1025 %).
   `fee_cost = 0.30 × 0.011025 = $0.003308`. `expected_max_gain = 1 × 0.70 = $0.70`.
   `ev_after_fee = 0.70 - 0.003 = $0.697`. ≥ 0.05 → PASS.

10. **`test_position_sizer_rejects_negative_ev_after_fee`** — rejet edge.
    - Preconditions : BUY YES @ $0.97 size 1, copy_ratio 0.01 (raw_size = 0.01), max_position $200 (cap = 206), fee_client returns Decimal("0.10"), market crypto_fees_v2.
    - Action : `_check_buy(ctx)`.
    - Assertion : `passed=False`, `reason="ev_negative_after_fees"`.
    - Calcul : `effective_rate = 0.25 × (0.97 × 0.03)^2 = 0.25 × 0.000847 = 0.000212` (= 0.0212 %).
      `notional = 0.01 × 0.97 = $0.0097`, `fee_cost = $0.0097 × 0.000212 = $2e-6`.
      `expected_max_gain = 0.01 × 0.03 = $0.0003`. `ev_after_fee = $0.000298`.
      `0.000298 < 0.05` → REJECT ✓.

11. **`test_position_sizer_no_fee_client_preserves_behavior`** — backward-compat.
    - Preconditions : `PositionSizer(..., fee_rate_client=None)`, BUY n'importe quel.
    - Action : `_check_buy(ctx)`.
    - Assertion : `passed=True` (pas de fee math), `ctx.fee_rate is None`,
      `ctx.fee_cost_usd is None`, `ctx.ev_after_fee_usd is None`.

12. **`test_position_sizer_flag_off_preserves_behavior`** — flag opt-out.
    - Preconditions : `fee_rate_client` injecté MAIS `settings.strategy_fees_aware_enabled = False`.
    - Action : idem.
    - Assertion : idem #11. **Crucial** : `fee_client.get_fee_rate` jamais appelé (vérifier mock count = 0).

13. **`test_position_sizer_buy_yes_vs_buy_no_ev_calculation`** — side-symmetry.
    - Preconditions : BUY YES @ $0.40 et BUY NO @ $0.60 (équivalent miroir),
      mêmes paramètres market fee crypto.
    - Action : 2 appels `_check_buy(ctx_yes)` puis `_check_buy(ctx_no)`.
    - Assertion : `effective_fee_rate` identique (formule `(p × (1-p))^exp` symétrique : `0.40 × 0.60 == 0.60 × 0.40`).
      `ev_after_fee_usd` du YES = `ev_after_fee_usd` du NO (à epsilon Decimal).

14. **(Bonus) `test_position_sizer_reason_code_persisted_to_strategy_decisions`** — audit.
    - Preconditions : run pipeline complet avec rejet `ev_negative_after_fees`.
    - Action : query `select(StrategyDecision).where(reason="ev_negative_after_fees")`.
    - Assertion : 1 row, `pipeline_state` JSON contient `fee_rate`, `fee_cost_usd`, `ev_after_fee_usd`.

15. **(Bonus) `test_compute_effective_fee_rate_unknown_fee_type_uses_crypto_fallback`** — fallback formule.
    - Action : `_compute_effective_fee_rate(base=Decimal("0.10"), price=Decimal("0.50"), market=MarketMetadata(fee_type="weird_unknown"))`.
    - Assertion : retour `Decimal("0.015625")` (= 1.5625 %, formule Crypto exp=2).

16. **(Bonus) `test_compute_effective_fee_rate_sports_market`** — formule sport post-March 30 2026.
    - Action : `_compute_effective_fee_rate(base=Decimal("0.10"), price=Decimal("0.50"), market=MarketMetadata(fee_type="sports_fees_v2"))`.
    - Assertion : retour `Decimal("0.0075")` (= 0.75 %, formule Sports v2 : `0.03 × 0.25 = 0.0075`).

### 9.3 MC.3 — Settings validators (2 tests)

Dans [tests/unit/test_config.py](../../tests/unit/test_config.py).

17. **`test_settings_strategy_min_ev_validator_bounds`**
    - Preconditions : env `STRATEGY_MIN_EV_USD_AFTER_FEE=0.005` (sous le min).
    - Action : `Settings()`.
    - Assertion : `ValidationError` avec message `ge=0.01`.
    - Idem test : `STRATEGY_MIN_EV_USD_AFTER_FEE=15.0` → `ValidationError` `le=10.0`.

18. **`test_settings_strategy_fees_aware_default_true`** — backward-compat default.
    - Preconditions : pas d'env override.
    - Action : `Settings()`.
    - Assertion : `settings.strategy_fees_aware_enabled is True`,
      `settings.strategy_min_ev_usd_after_fee == Decimal("0.05")`,
      `settings.strategy_fee_rate_cache_max == 500`.

### 9.4 MC.4 — Dashboard count (1 test)

Dans [tests/unit/test_dashboard_queries.py](../../tests/unit/test_dashboard_queries.py).

19. **`test_strategy_dashboard_shows_ev_negative_rejection_count`**
    - Preconditions : seed 3 `StrategyDecision` avec `reason="ev_negative_after_fees"` + 5 autres reasons mixés.
    - Action : `get_strategy_stats(...)`.
    - Assertion : `result.rejected_by_reason["ev_negative_after_fees"] == 3`,
      autres compteurs non-nuls cohérents.

### 9.5 MC.5 — Orchestrator wiring (1 test)

Dans [tests/unit/test_strategy_orchestrator.py](../../tests/unit/test_strategy_orchestrator.py).

20. **`test_strategy_orchestrator_instantiates_fee_rate_client_when_flag_on`**
    - Preconditions : `settings.strategy_fees_aware_enabled = True`, monkeypatch `FeeRateClient.__init__` pour spy.
    - Action : `await orchestrator.run_forever(stop_event)` puis `stop_event.set()` après 50 ms.
    - Assertion : `FeeRateClient.__init__` appelé 1 fois.
    - **Régression guard** : avec flag off → spy count = 0.

**Total : 14 tests obligatoires** (5 MC.1 + 5 MC.2 + 2 MC.3 + 1 MC.4 + 1 MC.5)
+ **6 bonus** (LRU eviction, HTTP 400 fallback, no_secret_leak, audit
persist, formula unknown_fallback, formula sports). Tests ciblés entre
commits, full `pytest` à la fin.

---

## 10. Impact sur l'existant

### 10.1 Modules touchés

| Module | Changement | Backwards compat |
|---|---|---|
| [src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py) | **Nouveau fichier** | Pas de breaking — addition pure. |
| [src/polycopy/strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py) | `PositionSizer.__init__` +1 param optionnel ; `_check_buy` étendu ; helper `_compute_effective_fee_rate` ; `run_pipeline` +1 param optionnel | Tests existants `test_position_sizer_*` instancient `PositionSizer(sf, settings)` sans `fee_rate_client` → fallback comportement strict M13. Aucun test casse. |
| [src/polycopy/strategy/dtos.py](../../src/polycopy/strategy/dtos.py) | `PipelineContext` +3 champs optionnels (defaults `None`) | Tests qui instancient `PipelineContext(trade=...)` sans nouveaux champs → defaults appliqués. |
| [src/polycopy/strategy/orchestrator.py](../../src/polycopy/strategy/orchestrator.py) | Instanciation conditionnelle + injection | Si `STRATEGY_FEES_AWARE_ENABLED=false` → `fee_rate_client=None` → strategy comportement M2..M15 strict. |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +3 settings | Defaults non-cassants (flag=true active la nouvelle logique mais avec fallback graceful si endpoint down). |
| [.env.example](../../.env.example) | +1 bloc commenté | Aucun test ne lit `.env.example` directement. |
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | **Probablement aucun changement** (vérifier que `GROUP BY reason` est dynamique) | Si query déjà dynamique → 0 modif. |
| [src/polycopy/dashboard/templates/strategy.html](../../src/polycopy/dashboard/templates/strategy.html) | **Probablement aucun changement** (vérifier boucle Jinja dynamique) | Si template hardcodé → 1 ligne adaptation. |
| [tests/fixtures/clob_fee_rate_*.json](../../tests/fixtures/) | 3 fichiers nouveaux | Pas de breaking. |
| [tests/fixtures/gamma_market_crypto_fees_sample.json](../../tests/fixtures/gamma_market_crypto_fees_sample.json) | 1 fichier nouveau | Pas de breaking. |
| [CLAUDE.md](../../CLAUDE.md) | §Conventions + §Sécurité | Doc only. |

### 10.2 Changements de valeurs par défaut

- `STRATEGY_FEES_AWARE_ENABLED=true` par défaut → **change le comportement**
  pour les utilisateurs en mode dry-run ou live :
  - **Cas nominal** : la majorité des markets sont fee-free
    (`base_fee=0`), `effective_fee_rate=0`, `ev_after_fee == expected_max_gain`,
    pas de rejet ajouté. Comportement identique à M15.
  - **Cas crypto** : sur ~5-10 % du trafic (Crypto + NCAAB + Serie A
    enabled), le calcul fee s'applique. Quelques rejets nouveaux attendus
    (`ev_negative_after_fees`) sur les BUYs à prix élevé (≥ $0.95) qui
    avaient zéro upside réel.
  - **Cas réseau down** : fallback conservateur 1.56 % rejette plus de
    trades que nécessaire pendant l'outage. **Rollback simple** : `STRATEGY_FEES_AWARE_ENABLED=false`.
- `STRATEGY_MIN_EV_USD_AFTER_FEE=0.05` : valeur empirique cohérente avec
  capital $50-200. Utilisateur peut tuner après observation 7j.
- `STRATEGY_FEE_RATE_CACHE_MAX=500` : pas de breaking, cap mémoire.

### 10.3 Tests existants potentiellement impactés

À vérifier lors de l'implémentation :

- [tests/unit/test_strategy_pipeline.py](../../tests/unit/test_strategy_pipeline.py)
  : tous les `test_position_sizer_*` existants. **Devraient passer** car
  ils instancient `PositionSizer(sf, settings)` sans `fee_rate_client`
  → fallback nouveau path → comportement M13 strict.
- [tests/unit/test_strategy_orchestrator.py](../../tests/unit/test_strategy_orchestrator.py)
  : tests de `run_forever`. **Devraient passer** si le fee client est
  conditionnel sur le flag — bien vérifier le fixture default.
- [tests/integration/test_strategy_e2e.py](../../tests/integration/test_strategy_e2e.py)
  (si existe) : doit continuer à passer en mock.

---

## 11. Notes d'implémentation & zones d'incertitude

### 11.1 Schéma `/fee-rate` — capture confirmée 2026-04-25

**Fixture réelle capturée** via `curl` direct (skill polymarket consulté en
amont, mais le fetch live a confirmé) :

```bash
$ curl -s "https://clob.polymarket.com/fee-rate?token_id=34173977256006964629116136187956438858200808581207130107509012725557410386680"
{"base_fee":1000}
```

Token de test : `bitcoin-up-or-down-on-april-25-2026` (Crypto market réel,
`feesEnabled=true`, `feeType=crypto_fees_v2`).

**Schéma confirmé** :
- HTTP 200 : `{"base_fee": <int>}` où `int` est en **basis points**.
- HTTP 400 : `{"error": "Invalid token id"}` (token mal formé).
- HTTP 404 : `{"error": "fee rate not found for market"}` (token valide mais
  fee-free).

**Fixtures JSON** déjà créées :
- `tests/fixtures/clob_fee_rate_crypto_sample.json` → `{"base_fee": 1000}`
- `tests/fixtures/clob_fee_rate_zero_sample.json` → `{"base_fee": 0}`
- `tests/fixtures/clob_fee_rate_invalid_sample.json` → `{"error": "Invalid token id"}`

### 11.2 Latence M11 — stage `strategy_fee_fetched_ms` opt-in

**Décision D6** : pas de stage dédié v1. Justifications :
- Le fee fetch est dans `_check_buy` qui fait partie du stage
  `strategy_sized_ms` cumulatif M11. La latence sera visible.
- Cache hit (cas nominal post-warmup) ≈ 1 µs (dict lookup), négligeable.
- Cache miss ≈ 50-150 ms (httpx + tenacity), visible mais rare.

**Si latence empiriquement problématique** post-merge : ajouter stage
explicite via instrumentation autour de
`fee_rate_client.get_fee_rate(...)`. Migrer en **MH** ou patch direct.

### 11.3 SELL passthrough — pourquoi pas de fee adjustment

**Décision D5**. Justifications :

1. **Contract Polymarket** : la fee est payée par le **taker** au moment du
   match. Pour BUY YES taker, fee soustrait des shares reçues. Pour SELL
   YES taker, fee soustrait du USDC reçu. **Polymarket calcule cette fee
   on-chain à l'envoi de l'order, pas côté client**.
2. **PnL calculation côté polycopy** : `realized_pnl = (price - avg_price) ×
   size`. Cette formule est déjà une approximation côté dry-run M8 (pas de
   fee soustraite). Live, le `WalletStateReader` lira la valeur réelle
   on-chain qui inclura la fee. Donc le live PnL sera correct
   automatiquement. Le dry-run reste une approximation.
3. **Logique copy-trading** : si le source wallet vend, on **doit** vendre
   (sinon position virtuelle reste ouverte indéfiniment, cf. M13 Bug 5).
   Rejeter un SELL pour fee n'a aucun sens — on est obligé de fermer.
4. **Asymétrie d'impact** : un SELL bloqué = position dormante = capital
   gelé. Bien plus coûteux qu'une fee de 1.56 % qu'on subit de toute façon.

Conclusion : `_check_sell` reste strictement M13 Bug 5, aucune fee math.

### 11.4 Approximation EV — choix simple vs Bayésien

**Approximation polycopy v1** :
```
expected_max_gain = my_size × (1 - price)
ev_after_fee = expected_max_gain - fee_cost
```

C'est le **payout maximum si YES wins** (= `1 - price` par share, gain net).
Pas une vraie EV Bayésienne `prob_yes × payout - prob_no × cost`.

**Pourquoi cette simplification ?**
- On copie un wallet source dont on **suppose** la conviction = `trade.price`.
  Si le source achète à $0.40, on assume sa probabilité subjective ≥ 40 %
  (sinon il n'achèterait pas). On prend cette conviction comme baseline.
- L'EV vraie nécessite une `P(YES)` indépendante (pool mean ? marché
  efficient = price ?). Si on prend `P(YES) = trade.price`, alors
  `EV = price × (1 - price) - (1 - price) × price = 0`. Useless.
- On simplifie : on regarde le **payout max résiduel** comme proxy d'upside.
  Couplé avec `STRATEGY_MIN_EV_USD_AFTER_FEE`, ça donne un seuil pragmatique :
  rejeter quand l'upside max est trop faible pour absorber la fee.

**Raffinement futur** (hors scope MC, MG ou v2) :
- Utiliser `P(YES_true) = source_wallet_score × calibration_correction`.
- Vraie formule Kelly : `optimal_fraction = (P × (1-price) - (1-P) × price) / (1-price)`.
- Cf. spec MG (CLV + Kelly proxy).

### 11.5 ⚠️ Reconciliation Perplexity vs docs Polymarket officielles vs live API

**Triangulation 2026-04-25** :

| Source | Sports | Crypto | Coverage |
|---|---|---|---|
| **Skill cache (mars 22, pré-rollout)** | `feeRate=0.0175, exp=1` → 0.44% (NCAAB + Serie A only) | `feeRate=0.25, exp=2` → 1.56% | Vaste majorité fee-free |
| **Perplexity (MC.md, post-rollout)** | 0.75% | 1.80% | 8 catégories March 30 |
| **Docs Polymarket Help live (2026-04-25)** | `feeRate=0.03, exp=1` → **0.75% peak**, max **1.80%** | (non explicité) | Sports élargi March 30 |
| **Live API scan (500 markets récents)** | `feeType=sports_fees_v2`, `base_fee=1000` | `feeType=crypto_fees_v2`, `base_fee=1000` | 100% des nouveaux markets fee-enabled, mais Politics/Tech/Finance pas encore observés en live |

**Conclusions consolidées** :

1. **Sports v2 (post-rollout March 30)** : `feeRate=0.03, exp=1` (live doc
   confirmée, ≠ skill cache).
2. **Crypto v2** : params inchangés `feeRate=0.25, exp=2` (skill cache reste
   cohérent — pas de mention contraire dans la doc live).
3. **`base_fee` du `/fee-rate` endpoint** : **flag binaire**, pas un rate
   directement utilisable (mêmes 1000 bps retournés pour Crypto et Sports
   alors que les params formulaires diffèrent). Sert uniquement à savoir si
   le market est fee-enabled (>0) ou fee-free (=0).
4. **Fallback conservateur** : `Decimal("0.018")` (1.80%) — le worst-case
   absolu mentionné dans la doc live. Couvre toute catégorie présente ou
   future.
5. **Si Polymarket étend les fees** aux Politics/Finance/Tech post-MC :
   - Le code lit `feeType` Gamma en runtime → fallback Crypto formula
     (conservateur, par construction).
   - Mapping `_compute_effective_fee_rate` à étendre dès qu'un nouveau
     `feeType` apparaît dans le live data (à monitorer via MD agent qui
     watch `polymarket_v3_changelog`).

### 11.6 `MarketMetadata.fee_type` — ajout explicite

[src/polycopy/strategy/dtos.py](../../src/polycopy/strategy/dtos.py)
`MarketMetadata` a `extra="allow"` qui permet déjà d'accéder à `feeType`
via `market.__pydantic_extra__["feeType"]`. **Mais** :
- mypy strict ne reconnaît pas le champ → erreurs.
- Refactor implicite si Polymarket rename le champ.

**Recommandation** : ajouter explicitement :

```python
fee_type: str | None = Field(default=None, alias="feeType")
fees_enabled: bool | None = Field(default=None, alias="feesEnabled")
```

Backwards compat strict (defaults None). Tests existants `MarketMetadata(...)`
sans ces champs → defaults appliqués.

### 11.7 Ordre des commits recommandé

L'ordre recommandé (cf. §15 prompt d'implémentation) :

1. **MC.1** (FeeRateClient + fixtures) — module isolé, testable seul.
2. **MC.3** (settings) — config additive, pas d'impact runtime tant que
   personne ne lit.
3. **MC.5** (orchestrator wiring) — instancie le client mais sans
   l'utiliser tant que MC.2 n'est pas mergé. Logue
   `fee_rate_client_instantiated` au boot.
4. **MC.2** (PositionSizer EV) — le cœur. Active le fee adjustment.
5. **MC.4** (dashboard count) — vérification post-deploy 24h, pas urgent.

Chaque commit ≤ 3 fichiers modifiés + 1 fichier de tests. Push immédiat sur
`main` après chaque commit (pas de branche, pas de PR — règle projet).

### 11.8 Race condition cache vs single-flight

Scénario : 2 coroutines A et B appellent `get_fee_rate("X")` simultanément.

1. A : cache miss, crée Future, lance fetch HTTP.
2. B : voit Future inflight pour X, await.
3. A : fetch revient, set Future result.
4. B : reçoit le result.
5. **Pendant ce temps** : entre étape 3 et 5, si une 3ᵉ coroutine C arrive
   et le cache est entre-temps mis à jour ? Le code actuel fait :
   - `cached = self._cache.get(token_id)` → si `cached and (now - ts) < 60s`
     → retour immédiat.
   - Sinon → check `_inflight`.

   La séquence dans `get_fee_rate` est :
   ```
   1. cached lookup
   2. inflight lookup
   3. create Future + launch fetch
   4. resolve Future
   5. cache update
   ```

   Entre 4 et 5, si C arrive, `cached` est encore vide → re-check inflight →
   le Future de A est popé du dict (`finally: self._inflight.pop(token_id, None)`)
   → C ne le voit pas → C lance un 2ᵉ fetch redondant.

**Mitigation** : update cache **avant** `set_result`, et popper inflight
**après** :

```python
async def _fetch_and_cache(self, token_id, now):
    rate = await self._fetch(...)
    self._cache[token_id] = (rate, now)  # cache d'abord
    self._cache.move_to_end(token_id)
    # LRU eviction inline
    return rate
```

Et dans `get_fee_rate` :

```python
fut = loop.create_future()
self._inflight[token_id] = fut
try:
    rate = await self._fetch_and_cache(token_id, now)
    fut.set_result(rate)  # set après cache
    return rate
finally:
    self._inflight.pop(token_id, None)
```

Avec cet ordre, la race window est ~1 µs (entre `cache update` et
`set_result`) — acceptable, et même si C lance un 2ᵉ fetch, c'est borné à
1 fetch supplémentaire (pas une explosion).

### 11.9 Conversion `Decimal ↔ float` dans `ctx`

Le `PipelineContext` a des champs `float` historiquement. Pour compat
existante (sérialisation `to_audit_dict`), garder `float`. Conversion
unique au moment de l'assignement :

```python
ctx.fee_rate = float(effective_fee_rate)         # Decimal → float
ctx.fee_cost_usd = float(fee_cost)               # Decimal → float
ctx.ev_after_fee_usd = float(ev_after_fee)       # Decimal → float
```

Risque drift d'arrondi : minime sur des valeurs ≥ $0.0001 (precision
Polymarket). Acceptable.

---

## 12. Commandes de vérification finale

Bloc copiable-collable pour l'implémenteur M16 :

```bash
# 1. Environnement déjà OK (bash scripts/setup.sh idempotent).
cd /home/nexium/code/polycopy
source .venv/bin/activate

# 2. Lint + type-check (après chaque commit).
ruff check .
ruff format . --check
mypy src

# 3. Tests ciblés par item (entre commits, ~30 sec chacun).
pytest tests/unit/test_fee_rate_client.py -x --tb=short          # MC.1
pytest tests/unit/test_strategy_pipeline.py -x --tb=short -k "fee or ev"  # MC.2
pytest tests/unit/test_config.py -x --tb=short -k "strategy_min_ev or fees_aware"  # MC.3
pytest tests/unit/test_dashboard_queries.py -x --tb=short -k "ev_negative"  # MC.4
pytest tests/unit/test_strategy_orchestrator.py -x --tb=short -k "fee_rate"  # MC.5

# 4. Full suite (à la fin uniquement — ~3 min).
pytest

# 5. Test runtime dry-run (boot + smoke + dashboard).
STRATEGY_FEES_AWARE_ENABLED=true \
EXECUTION_MODE=dry_run \
DRY_RUN_REALISTIC_FILL=true \
DASHBOARD_ENABLED=true \
python -m polycopy --verbose &
BOT_PID=$!
sleep 60
curl -s http://127.0.0.1:8787/strategie | grep -E "ev_negative_after_fees" || echo "no rejections yet (normal si trafic faible)"
kill $BOT_PID

# 6. Pas de migration Alembic (vérifier).
alembic revision --autogenerate -m "m16_check" --sql | head -5
# Devrait être vide ou "no changes detected".

# 7. Grep sécurité.
grep -rE "POLYMARKET_PRIVATE_KEY|TELEGRAM_BOT_TOKEN|CLOB_API_SECRET|REMOTE_CONTROL_TOTP_SECRET" src/polycopy/executor/fee_rate_client.py
# Doit être vide (0 match).

# 8. Capture fixture réelle (déjà fait 2026-04-25, mais à raffraîchir si
#    le schéma /fee-rate change). Token Crypto réel actif :
curl -s "https://clob.polymarket.com/fee-rate?token_id=$(curl -s 'https://gamma-api.polymarket.com/markets?slug=bitcoin-up-or-down-on-april-25-2026' | python3 -c 'import json,sys; print(json.loads(json.load(sys.stdin)[0][\"clobTokenIds\"])[0])')" \
  > tests/fixtures/clob_fee_rate_crypto_sample.json
cat tests/fixtures/clob_fee_rate_crypto_sample.json
# Attendu : {"base_fee": <int positif>}
```

Après `git push` sur `main`, côté `uni-debian` :

```bash
ssh uni-debian
cd ~/Documents/GitHub/polycopy
git pull
# Bot auto-restart via systemd ou :
# pkill -f "polycopy" && python -m polycopy --verbose &

# Vérifier dans les 30 min suivantes :
# - Logs : `fee_rate_fetched` events apparaissent (cache miss puis hit).
# - /strategie : compteur `ev_negative_after_fees` éventuel (peut rester 0
#   si peu de trafic crypto sur 30 min).
# - Pas d'erreur `pipeline_error` en cascade (fallback conservateur joue).
```

---

## 13. Hors scope M16 (à ne pas implémenter)

- ❌ Maker fees / rebates (polycopy taker-only, FOK).
- ❌ Backtest historique avec fees.
- ❌ Dashboard panel "Fee impact" graphique (sum 24h, breakdown catégorie) → **MH**.
- ❌ Alertes Telegram fee spike inhabituel.
- ❌ FeeRateClient WebSocket.
- ❌ Gestion tier utilisateur Builder Tiers.
- ❌ Stage latency `strategy_fee_fetched_ms` (M11) — opt-in MH si nécessaire.
- ❌ Recalibrage auto `STRATEGY_MAX_ENTRY_PRICE` ou `MAX_SLIPPAGE_PCT` post-fees.
- ❌ Lecture `feesEnabled` Gamma comme short-circuit pre-fetch.
- ❌ Re-test des trades historiques avant M16 (compteur démarre à 0 post-merge).
- ❌ Migration Alembic (M16 ne touche aucun schéma DB).
- ❌ Modification `_check_sell` (cf. §11.3 décision D5).

---

## 14. Notes d'implémentation & zones d'incertitude

### 14.1 Schéma `/fee-rate` — capture confirmée 2026-04-25

Cf. §11.1.

### 14.2 Décisions architecturales clefs

- **D1** : `fee_rate_client` optional dans `PositionSizer.__init__` (default
  `None`). Justification : préserve les 200+ tests existants qui instancient
  `PositionSizer` sans fee client. Permet tests incrémentaux sans cascade.
- **D2** : Client vit dans `StrategyOrchestrator` (consommateur) pas
  `ExecutorOrchestrator`. Justification : fee check s'applique au **rejet
  pre-POST**, cohérent avec `SlippageChecker`.
- **D3** : Fallback conservateur **1.80 %** (max effective post-rollout
  March 30 2026 — Perplexity C4 + cohérent docs Polymarket live 2026-04-25
  qui mentionnent "Maximum effective fee rate: 1.80%"). Couvre worst-case
  toutes catégories actuelles (`crypto_fees_v2`, `sports_fees_v2`) et
  futures. Asymétrie d'impact (mieux sur-estimer que sous-estimer).
- **D4** : Cache TTL 60 s. Justification : fees peuvent bouger
  dynamiquement, 60 s est conservatif. Cohérent cache Gamma M2.
- **D5** : Single-flight pattern. Justification : évite TOCTOU redondance
  préventivement (audit M-007). Bénéfice marginal (cache TTL 60 s amortit
  déjà), mais 0 coût additionnel.
- **D6** : Pas de stage `strategy_fee_fetched_ms` v1 — coût mesuré dans
  `strategy_sized_ms` cumulatif. Migrer si problématique.
- **D7** : Seuil default `STRATEGY_MIN_EV_USD_AFTER_FEE = 0.05` (5 ¢).
  Justification : ordre de grandeur cohérent avec capital $50-200
  (= 0.025-0.1 % du notional minimum). Ajustable empiriquement post-ship.
- **D8** : Fee math **dans** `PositionSizer._check_buy`, pas un nouveau
  filtre `FeeChecker`. Justification : économise une instanciation par
  cycle, le fee est intrinsèquement lié au sizing (besoin de `notional` =
  `my_size × price`).
- **D9** : Skip fee math si `fee_rate_client is None` (pas d'erreur).
  Justification : permet configuration qui désactive fee-awareness
  (backward compat + testing + SIMULATION mode hypothétique).
- **D10** : SELL passthrough (pas de fee adjustment côté `_check_sell`).
  Justification §11.3 — contrat Polymarket + nécessité de fermer.
- **D11** : `_compute_effective_fee_rate` est `@staticmethod` —
  testable isolément, pas de dépendance `self`.

### 14.3 Hypothèses empiriques à valider AVANT et APRÈS ship

- **H-EMP-10** (synthèse §8 + MC.md §6) : Fees dynamiques réduisent notre
  EV moyen de ≥ 1 % post-implementation. **Méthode** : post-MC.2, sur 100
  trades synthétiques (fixture réaliste mix Crypto + Politics), calculer
  `ev_before_fee - ev_after_fee` distribution. **Seuil informatif** : si
  moyenne < 0.5 %, fees ne sont pas impactantes sur notre portfolio mix
  (cohérent avec docs officielles : la majorité des markets fee-free). Si
  > 1.5 %, recalibrer `STRATEGY_MIN_EV_USD_AFTER_FEE`.
- **Q5** (synthèse §11) : quel % de trades seraient rejetés par
  `ev_negative_after_fees` post-MC ? Mesure empirique sur 100 décisions
  historiques rejouées. Si > 30 % rejet addition, seuil trop strict OU notre
  pool actif prend structurellement des trades sub-marginaux (signal pour
  MA / MB).
- **Q11** (impl) : quel TTL effectif observé sur le cache ? Si >> 60 s
  (markets longs > 24h), augmenter TTL. Si << 60 s (markets crypto
  intraday), maintenir.

**Pas de script de validation obligatoire avant ship** : MC.2 est
déterministe (fonction pure modulo cache), validation suffit avec les
tests unitaires.

### 14.4 Cache TTL 60s vs rate limit Polymarket

Rate limit Polymarket CLOB : ~1500/10s sur `/book` et `/price` endpoints
(Perplexity B1). `/fee-rate` non documenté séparément, assume ~CLOB pattern.

Avec TTL 60 s + ~50 tokens actifs polycopy = ~50 requêtes/minute au peak
(cache miss tous les 60 s par token). Largement sous le cap.

Au boot (cache vide), peak ~50 requêtes en 1-2 secondes via
`asyncio.gather` orchestrator. Cohérent avec autres fan-out polycopy.

### 14.5 Decimal vs float dans formule EV

Cf. §11.9. Pattern "Decimal interne, float à la persistance" préservé.

### 14.6 Race condition cache vs single-flight

Cf. §11.8. Mitigation : cache update avant `set_result` du Future.

### 14.7 Questions ouvertes pertinentes à MC

- **Q12** : si `market.fee_type` est `null` mais `feesEnabled=true`
  (incohérence Gamma) → fallback Crypto (conservateur).
- **Q13** : si Polymarket introduit un nouveau `feeType` (ex:
  `ai_fees_v1`) → fallback Crypto. Suivre `polymarket_v3_changelog` (MD
  inclut un agent qui watch ces évolutions).
- **Q14** : impact sur arbitrageurs YES+NO (qui paient fees sur les 2
  sides) — leur EV net est encore plus faible post-fees. Argument
  supplémentaire pour `arbitrage_bot_gate` (MB.7). Pas dans scope MC.

### 14.8 Références externes

- **Polymarket Help Trading Fees** :
  [help.polymarket.com/en/articles/13364478-trading-fees](https://help.polymarket.com/en/articles/13364478-trading-fees)
  — référence officielle (2026-03-22 cached via skill polymarket).
- **Polymarket /fee-rate API** :
  [docs.polymarket.com/api-reference/market-data/get-fee-rate](https://docs.polymarket.com/api-reference/market-data/get-fee-rate)
  — schéma response.
- **Skill polymarket fees.md** :
  `/home/nexium/.claude/plugins/marketplaces/polymarket-skill/skills/polymarket/fees.md`
  — formule + tables.
- **KuCoin fees guide** :
  [kucoin.com/blog/polymarket-fees-trading-guide-2026](https://www.kucoin.com/blog/polymarket-fees-trading-guide-2026)
  — explainer (overestimate vs docs officielles, cf. §11.5).
- **MEXC 8 new categories** :
  [mexc.com/news/976171](https://www.mexc.com/news/976171) — annonce
  rollout March 30 (à pondérer avec docs officielles).
- **FinanceFeeds revenue** :
  [financefeeds.com/polymarket-fees-revenue-surge-new-pricing-model](https://financefeeds.com/polymarket-fees-revenue-surge-new-pricing-model/)
  — preuve impact business.
- **Polymarket US DCM fee schedule** :
  [polymarketexchange.com/fees-hours.html](https://www.polymarketexchange.com/fees-hours.html)
  — régulé US différent (0.05 coefficient max $1.25/100 contracts).
  **Pas applicable à polycopy** (on trade le venue offshore, pas US DCM).

---

## 15. Prompt d'implémentation

Bloc à coller tel quel dans une nouvelle conversation Claude Code à
l'implémentation M16.

````markdown
# Contexte

polycopy `PositionSizer` calcule l'EV sans soustraire les fees taker
Polymarket (rollout March 2026 sur Crypto + NCAAB + Serie A). Sur trades
crypto, fee max 1.56 % du notional non comptabilisée → en dry-run, on
sur-estime le PnL réel live de 0.6-1.6 %. Blocker invisible du passage live.

Diagnostic complet dans
[docs/specs/M16-dynamic-fees-ev.md](docs/specs/M16-dynamic-fees-ev.md). 5
items couplés MC.1 → MC.5 (FeeRateClient + PositionSizer EV-aware + settings
+ dashboard count + orchestrator wiring).

# Prérequis

- Lire `docs/specs/M16-dynamic-fees-ev.md` **en entier** (spécifiquement §5
  algorithmes + §9 test plan).
- Lire [CLAUDE.md](CLAUDE.md) sections "Conventions de code" (Decimal interne
  / float persistence) et "Sécurité" (triple garde-fou M3 + 4ᵉ M8 préservés).
- Lire
  [docs/specs/M2-strategy-engine.md](docs/specs/M2-strategy-engine.md) pour
  contexte `PositionSizer` initial.
- Les fixtures
  [tests/fixtures/clob_fee_rate_*.json](tests/fixtures/) +
  [gamma_market_crypto_fees_sample.json](tests/fixtures/gamma_market_crypto_fees_sample.json)
  sont **déjà capturées 2026-04-25** (cf. §11.1 spec). Ne pas les recapturer
  sauf si schéma `/fee-rate` a changé.

# Ordre de commits recommandé (5 commits atomiques)

1. `feat(executor): add FeeRateClient with cache + LRU + single-flight + tenacity` (MC.1, §5.1, 5+3 tests §9.1)
2. `feat(config): add STRATEGY_MIN_EV_USD_AFTER_FEE + STRATEGY_FEES_AWARE_ENABLED settings` (MC.3, §5.3, 2 tests §9.3)
3. `feat(strategy): wire FeeRateClient in StrategyOrchestrator under flag` (MC.5, §5.5, 1 test §9.5)
4. `feat(strategy): make PositionSizer EV-aware post-fees with rejection reason` (MC.2, §5.2, 5+3 tests §9.2)
5. `docs(dashboard): no template change — verify ev_negative_after_fees count surfaces automatically` (MC.4, §5.4, 1 test §9.4)

**Push sur main après chaque commit.** Pas de branche, pas de PR (règle projet).

# Validation entre commits

- Tests ciblés (cf. memory `feedback_test_scope`) : ~30 sec / commit.
- `ruff check .` + `ruff format --check .` + `mypy src` après chaque commit.
- Avant commit 4 (le critique) : montrer le diff `_check_buy` pour valider
  la formule EV approximation (§11.4) — c'est le seul cas où une erreur
  pourrait masquer / amplifier des trades.

# Tests + quality gates

- Tests ciblés entre commits (cf. memory `feedback_test_scope`).
- Full `pytest` + `ruff check .` + `ruff format .` + `mypy src` à la fin.
- Les 2 tests flaky de `test_watcher_live_reload.py` (pré-existants) passent
  en isolation — OK s'ils échouent dans la full suite.

# Git workflow

- **Tout commit directement sur `main`** — pas de branche, pas de PR
  (règle projet, workflow trunk-based).
- 5 commits atomiques (1 par item MC) poussés en série sur `main` après
  validation tests verts entre chaque push.
- Update CLAUDE.md §Conventions avec mention M16 fee-aware (cf. §10 spec
  M14 pour pattern).

# Contraintes non négociables

- `STRATEGY_FEES_AWARE_ENABLED=true` par défaut MAIS rétrocompat stricte
  garantie : si `fee_rate_client=None` ou `base_fee=0` → comportement
  M2..M15 strict.
- **Versioning sacré inchangé** : aucun impact sur `trader_scores` ou
  registry scoring.
- **Aucune migration Alembic** — `strategy_decisions.reason` est
  `String(64)` libre, accepte `ev_negative_after_fees` (28 chars).
- **Triple garde-fou M3 + 4ᵉ M8 préservés** : FeeRateClient read-only
  public no-auth, ne touche aucun cred.
- **SELL passthrough** : `_check_sell` ne touche **pas** au fee path
  (cf. §11.3 décision D5).
- **Decimal strict** dans la math fee, conversion `float(Decimal)` au
  moment de l'assignement à `ctx`.
- **Conventions CLAUDE.md** : async, Pydantic v2, SQLAlchemy 2.0,
  structlog, docstrings FR / code EN, pas de print.
- **mypy strict propre, ruff propre, coverage ≥ 80 %** sur nouveaux
  fichiers.

# Demande-moi confirmation AVANT

- Si `MarketMetadata.fee_type` doit être ajouté explicitement (cf. §11.6).
- Si la query dashboard `/strategie` n'agrège pas dynamiquement (cf. §5.4) →
  refacto template Jinja.
- Si tu observes un test M2..M15 qui casse (signal de scope creep).

# STOP et signale si

- Endpoint `/fee-rate` retourne un schéma autre que `{"base_fee": <int>}`
  (re-capturer fixture).
- Coverage `_compute_effective_fee_rate` < 100 % (formule sensible).
- Latence cache miss > 200 ms p95 (cible < 100 ms p95).

# Plan à confirmer

Commence par me confirmer ton plan en 1 message bref (1 phrase par commit),
puis enchaîne les 5 commits dans l'ordre ci-dessus. Tests verts avant chaque
push.
````

---

## 16. Commit message proposé

```
feat(strategy): M16 dynamic taker fees + EV adjustment

Bundle 5 items (MC.1 → MC.5) qui ajoute le fee adjustment au PositionSizer
pour éviter de trader EV-négatif post fees taker March 2026 (Crypto +
NCAAB + Serie A) :

- MC.1 nouveau `FeeRateClient` async (endpoint public no-auth
  GET /fee-rate?token_id=, cache TTL 60s, LRU 500 entries, single-flight,
  tenacity backoff exponentiel, fallback conservateur 1.56 % sur erreur
  réseau).
- MC.2 `PositionSizer._check_buy` calcule `effective_fee_rate` via formule
  Polymarket officielle `feeRate × (p × (1-p))^exponent` (paramètres
  mappés depuis `market.fee_type` Gamma : crypto_fees_v2 → exp=2,
  sports_fees_v1 → exp=1, fallback conservateur Crypto). Rejet
  `ev_negative_after_fees` si `EV - fee_cost < strategy_min_ev_usd_after_fee`.
- MC.3 settings `STRATEGY_MIN_EV_USD_AFTER_FEE` (default `Decimal("0.05")`,
  range [0.01, 10.0]), flag `STRATEGY_FEES_AWARE_ENABLED` (default true),
  cap LRU `STRATEGY_FEE_RATE_CACHE_MAX` (default 500).
- MC.4 dashboard `/strategie` count `ev_negative_after_fees` apparaît
  automatiquement via le `GROUP BY reason` existant. Panel "Fee impact"
  graphique déféré à MH.
- MC.5 co-lancement `FeeRateClient` dans `StrategyOrchestrator` (et non
  `ExecutorOrchestrator` — D2), partage `httpx.AsyncClient`. Injection
  dans `PositionSizer.__init__` via paramètre optionnel
  (rétrocompat stricte M2..M15).

Schéma endpoint `/fee-rate` capturé fixture réelle 2026-04-25 :
`{"base_fee": <int_bps>}`. Conservation Decimal end-to-end (CLAUDE.md
convention "Decimal interne, float à la persistance"). Triple garde-fou
M3 + 4ᵉ M8 préservés (read-only public no-auth, pas de cred touché).
SELL passthrough (D5 §11.3).

14 tests unit + 0 intégration obligatoire. 3 fixtures JSON capturées.
Aucune migration Alembic (la table `strategy_decisions.reason: String(64)`
accepte `ev_negative_after_fees` (28 chars) sans modif schéma).

Cf. spec [docs/specs/M16-dynamic-fees-ev.md](docs/specs/M16-dynamic-fees-ev.md).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## 17. Critères d'acceptation

- [ ] 5 items MC.1 → MC.5 implémentés selon §5.
- [ ] `FeeRateClient` instantiable, méthode `async get_fee_rate(token_id) -> Decimal`.
- [ ] Cache TTL 60s effectif (test freezegun).
- [ ] LRU eviction quand `len(cache) > cache_max`.
- [ ] Single-flight : 3 callers concurrents même token_id → 1 seule requête HTTP.
- [ ] Fallback `Decimal("0.0156")` sur `TransportError` post-tenacity / HTTP 5xx / HTTP 400.
- [ ] HTTP 404 → `Decimal("0")` (fee-free market).
- [ ] `_compute_effective_fee_rate` formule correcte pour Crypto (exp=2),
      Sports (exp=1), fallback unknown → Crypto.
- [ ] `_check_buy` rejette `ev_negative_after_fees` quand `ev_after_fee <
      strategy_min_ev_usd_after_fee`.
- [ ] `_check_buy` préserve M2..M15 quand `fee_rate_client=None` ou flag off.
- [ ] `_check_sell` inchangé (pas de fee math).
- [ ] `PipelineContext` étendu avec `fee_rate`, `fee_cost_usd`, `ev_after_fee_usd`
      (defaults None).
- [ ] Settings `STRATEGY_MIN_EV_USD_AFTER_FEE`, `STRATEGY_FEES_AWARE_ENABLED`,
      `STRATEGY_FEE_RATE_CACHE_MAX` ajoutées avec validators.
- [ ] `.env.example` documenté.
- [ ] Dashboard `/strategie` surface `ev_negative_after_fees` automatiquement
      (test).
- [ ] `StrategyOrchestrator.run_forever` instancie `FeeRateClient` quand flag
      true.
- [ ] **Aucune migration Alembic** (`alembic revision --autogenerate`
      retourne empty).
- [ ] **Triple garde-fou M3 + 4ᵉ M8 préservés** : aucun fichier executor
      hors `fee_rate_client.py` touché.
- [ ] **Aucune cred CLOB consommée** — M16 100 % read-only public.
- [ ] Test grep secret leak passe sur `FeeRateClient` events structlog.
- [ ] CLAUDE.md §Conventions mise à jour avec mention M16 fee-aware.
- [ ] Tests M2..M15 existants passent inchangés (rétrocompat stricte via
      `fee_rate_client=None`).
- [ ] **Invariants M5 / M5_bis / M5_ter / M11 / M12 / M13 / M14 préservés** :
      lifecycle, eviction, watcher, latency, scoring, dry-run executor —
      tous intacts.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src` : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur fichiers nouveaux/modifiés.
- [ ] Smoke test runtime 60s : logs `fee_rate_fetched` apparaissent, pas
      d'erreur cascade.
- [ ] 5 commits atomiques MC.1 → MC.5 poussés sur `main` (pas de branche,
      pas de PR — règle projet).

---

**Fin spec M16.**
