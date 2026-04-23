# M13 — Dry-run observability & neg_risk resolution

**Status** : Draft — 2026-04-23
**Depends on** : M2 (Strategy pipeline), M4 (PnL snapshots), M6 (Dashboard /home), M7 (Telegram daily summary), M8 (Dry-run resolution v1), M10 (execution_mode modes)
**Ne bloque pas** : M14+ (futurs), aucune migration DB structurelle (aucun nouveau schéma, juste des valeurs agrégées en requête).

---

## 0. TL;DR

M13 livre un dry-run **exploitable** pour le test 14 jours en cours sur `uni-debian`. 5 sujets bundle :

- **Bug 5** (critique, débloquant) : [PositionSizer.check()](../../src/polycopy/strategy/pipeline.py#L168) rejette aujourd'hui SELL + BUY indistinctement sur `position_already_open`. Rend side-aware → les SELL copiés ferment enfin les positions virtuelles, le PnL se matérialise, le capital se libère.
- **Bug 6** (UX) : 3 nouvelles cartes KPI sur /home — `Exposition`, `Gain max latent`, `Win rate global`.
- **Bug 7** (UX) : recap Telegram daily `volume_executed_usd: $0.00` en dry-run (même pathologie que Bug 2 côté dashboard, pattern oublié dans `monitoring/daily_summary_queries.py`) → mode-aware aligné sur `execution_mode`.
- **Feature "PnL latent"** : 4ᵉ carte /home → `total_usdc − initial_capital − realized_pnl`. Clarifie la distinction PnL réalisé (cristallisé) vs latent (mark-to-market) que l'utilisateur confond actuellement.
- **Feature M8 v2 — résolution neg_risk** : suppression du skip `dry_run_resolution_neg_risk_unsupported` dans [dry_run_resolution_watcher.py:90](../../src/polycopy/executor/dry_run_resolution_watcher.py#L90). Les marchés neg_risk sont structurellement binaires (YES/NO par candidat, résolution via `outcome_prices` identique), la résolution automatique peut être activée sans changer le moteur.

Résultat attendu ≤ 24h après deploy : **PnL réalisé / latent / exposition / gain max / win rate** tous non-nuls et cohérents sur /home, /activity peuplée, /performance leaderboard opérationnel, positions neg_risk fermées automatiquement. Le test 14 jours produit enfin des données de performance exploitables.

---

## 1. Motivation & use case concret

### 1.1 Le symptôme observé — 2026-04-23

Timeline bot sur `uni-debian` :

> - 2026-04-22 ~18h : bot démarré avec 6 commits M1..M12 + 4 fix (M13 bug 1-4 déjà shippés). `EXECUTION_MODE=dry_run`, `DRY_RUN_REALISTIC_FILL=true`.
> - 2026-04-23 ~17h : 24h plus tard. Dashboard home affiche `TOTAL USDC: $998`, `DRAWDOWN: 1.4%`, `POSITIONS OUVERTES: 512`, `VOLUME TRADÉ: $1.2k`, `FILLS: 1073`, **`PNL RÉALISÉ: $0.00`**.
> - `/positions?state=closed` : vide.
> - `/activity` : "Aucune position fermée pour le moment — aucun SELL encore copié ni marché résolu".
> - `/performance` : 9 traders listés, tous avec `Closed: 0`, `Win rate: —`, `PnL total: $0.00`.
> - Recap Telegram daily : `Volume exécuté : $0.00` (incohérent avec /home qui montre $1.2k).

**L'utilisateur n'a aucun signal de performance réel**. Le graphe /pnl bouge entre $970-$1020 mais c'est du mark-to-market latent sur les 512 positions ouvertes, pas du PnL réalisé.

### 1.2 Les 3 voies de fermeture d'une position en dry-run — 2 cassées

Une position virtuelle M8 se ferme via :

| Voie | M8 v1 status | Bloquant |
|---|---|---|
| SELL copié d'un source wallet | **Cassée par Bug 5** | `PositionSizer` rejette `position_already_open` |
| Résolution auto marché binaire | OK | Fonctionne pour les rares marchés binaires |
| Résolution auto marché neg_risk | **Cassée par design M8 v1** | Skip `dry_run_resolution_neg_risk_unsupported` |

Or la quasi-totalité des marchés que le bot trade sont **neg_risk** (élections, championnats, drafts NFL, catégories Best Picture — visible via les tags `neg-risk` omniprésents sur /execution). Et les SELL copiés sont bloqués par Bug 5. **Résultat : aucune des 3 voies ne fonctionne concrètement** pour les positions du test 14 jours, d'où les $0 partout.

### 1.3 Pourquoi un test 14 jours sans PnL réalisé est inexploitable

Sans signal de réalisation :

- Impossible de mesurer `win_rate` (combien de trades gagnants vs perdants).
- Impossible de mesurer le PnL net (seul le mark-to-market latent bouge, mais il est volatile et réversible).
- Impossible d'évaluer si la stratégie de scoring v1/v2 sélectionne bien les traders (le leaderboard /performance reste vide).
- Impossible de mesurer les performances par filtre (combien de `entry_price_too_high` évités des pertes, combien de `slippage_exceeded` étaient justifiés).

M13 est donc **un pré-requis absolu à l'évaluation du test 14 jours**. Sans M13, les 14 jours produisent uniquement un graphe PnL latent oscillant autour de $1000 — aucune donnée cristallisée exploitable pour la décision live/pas live à la fin.

### 1.4 Pourquoi une spec formelle

Le scope couvre 5 sujets dans 5 modules différents (strategy, dashboard, monitoring, executor, templates). 4 d'entre eux sont simples (bugs), 1 (M8 v2) demande de valider le schéma Gamma neg_risk et la formule PnL. Sans spec, les détails (ordre des commits, interactions race condition SELL copié vs résolution M8 v2 en parallèle, gestion des 512 positions orphelines déjà en DB, définition exacte de `initial_capital` pour le PnL latent) risquent d'être tranchés à la va-vite à l'implémentation. La spec M13 fige ces choix en amont.

---

## 2. Scope / non-goals

### 2.1 Dans le scope

- **Bug 5** : refactor `PositionSizer.check()` en logique side-aware. BUY check `condition_id` (comportement actuel préservé), SELL check `(condition_id, asset_id)`. Nouveau reason code `sell_without_position` pour les SELL orphelins. Sizing SELL proportional à `copy_ratio` cappé à `existing.size`.
- **Bug 6** : 3 nouvelles cartes sur /home :
  - `Exposition` (Σ size × avg_price sur positions ouvertes, mode-aware).
  - `Gain max latent` (Σ size × (1 − avg_price) sur positions ouvertes, mode-aware).
  - `Win rate global` (agrégat du leaderboard /performance sur toutes positions fermées, mode-aware).
- **Bug 7** : [daily_summary_queries._orders_stats_since](../../src/polycopy/monitoring/daily_summary_queries.py#L169) filtre mode-aware basé sur `settings.execution_mode`.
- **PnL latent** : 4ᵉ carte /home calculée côté query (`total_usdc − initial_capital − realized_pnl`). Définition de `initial_capital` : champ optionnel `DRY_RUN_INITIAL_CAPITAL_USD` avec fallback sur le `total_usdc` du plus ancien `PnlSnapshot` si non défini.
- **M8 v2 neg_risk resolution** : suppression du skip dans [dry_run_resolution_watcher.py:90-96](../../src/polycopy/executor/dry_run_resolution_watcher.py#L90) sous flag `DRY_RUN_NEG_RISK_RESOLUTION_ENABLED=true` (défaut). Logique de résolution partagée avec les binaires (les 2 cas utilisent `outcome_prices + max ≥ 0.99`). Nouvel event Telegram `dry_run_market_resolved_neg_risk` émis à chaque fermeture.

### 2.2 Hors scope explicites (liste exhaustive)

- ❌ **Pas de rétro-résolution automatique des 512 positions existantes** sur des marchés déjà résolus avant M8 v2. Option backfill script optionnel documentée (§11.3) mais non implémentée v1 — l'utilisateur l'exécute manuellement s'il veut récupérer les données historiques.
- ❌ **Pas de changement de la logique de fill dry-run** ([simulate_fill](../../src/polycopy/executor/pipeline.py#L384) orderbook M8 v1 inchangé).
- ❌ **Pas de support "invalid resolution"** (marché qui se résout sans winner — très rare sur Polymarket). Documenté comme "skip + warning `dry_run_resolution_winning_outcome_unknown`" déjà en place M8 v1.
- ❌ **Pas de backtest offline / SIMULATION mode M10** — M13 vit strictement en `execution_mode == "dry_run"` et `live`. `simulation` non touché.
- ❌ **Pas de nouveau endpoint dashboard** — uniquement extension des queries existantes et ajout de cartes au template `home.html`.
- ❌ **Pas de modification des autres filtres strategy** — `SlippageChecker`, `RiskManager`, `TraderLifecycleFilter`, `EntryPriceFilter` (M13 bug 4 déjà shippé), `MarketFilter` restent inchangés. `PositionSizer` est le seul filtre touché.
- ❌ **Pas de multi-fragment SELL cumulative tracking** — si un même SELL source est split entre plusieurs cycles (partial fills virtuels), on accepte la simplification v1 Bug 1 : `realized_pnl` calculé uniquement sur la fermeture finale. Documenté dans le code + spec M13 Bug 1.
- ❌ **Pas de UI widget "conversion précoce neg_risk"** (`negRiskAdapter` exposer le mécanisme de conversion early). Read-only dry-run, pas pertinent.
- ❌ **Pas de changement du `PnlSnapshotWriter`** — il continue à écrire `realized_pnl=0.0` et `unrealized_pnl=0.0` en DB (pré-calculés à 0 depuis M4). M13 calcule PnL latent/réalisé **au moment de la lecture** côté dashboard queries, pas à l'écriture côté writer. Refacto plus propre du writer reporté à un M14+ si le besoin émerge.

---

## 3. User stories

### 3.1 Story A — Bug 5 débloque le cycle BUY → SELL

**Avant M13** (2026-04-23) :
- 14h00 : source wallet `0xe8dd…ec86` BUY 100 shares à $0.40 sur cond X (marché neg_risk "Best Picture 2026").
- 14h00 : bot copie → BUY 1 share à $0.40, position virtuelle ouverte cond X / asset_YES.
- 14h30 : source wallet SELL 100 shares à $0.60 sur cond X (il prend son profit).
- 14h30 : bot voit le SELL signal → `PositionSizer.check()` trouve `existing` sur cond X → rejette avec `position_already_open`. Le SELL n'atteint jamais l'executor. La position reste ouverte indéfiniment.
- 18h00 : /activity affiche toujours 0 closed. `realized_pnl` stuck à NULL. Capital fully deployed.

**Après M13 Bug 5** :
- 14h30 : bot voit le SELL signal → `PositionSizer.check()` en branche SELL → trouve match `(condX, asset_YES)` dans `MyPosition` → accepte avec `my_size = min(100 × 0.01, 1.0) = 1.0`.
- 14h30 : executor simule le SELL → `upsert_virtual(SELL)` décrémente size à 0 → ferme position → Bug 1 fix (déjà shippé M13 pré-spec) calcule `realized_pnl = 1.0 × (0.60 − 0.40) = +$0.20`.
- 14h31 : /home `PNL RÉALISÉ: +$0.20`, /activity montre 1 closed avec PnL +$0.20, /performance wallet `0xe8dd…ec86` `Win rate 100%, Closed 1, PnL $0.20`.

### 3.2 Story B — PnL latent vs réalisé

L'utilisateur regarde /home 3 fois sur la journée. Analogie : il a acheté une action à $100.

**12h00** : /home après BUY initial
- PnL réalisé : $0.00
- PnL latent : **+$0.00** (mid = prix entrée)
- Exposition : $1000 (tout son capital est dans la position)
- Gain max latent : $0.00 (acheté à $1, pas d'upside)

**14h00** : le mid est passé de $0.40 à $0.50 sur les positions
- PnL réalisé : $0.00 (toujours rien vendu)
- PnL latent : **+$20** (théorique, pas encore cristallisé)
- Exposition : $1000 (capital engagé inchangé)
- Gain max latent : diminue à mesure que les prix montent (moins d'upside restant)

**18h00** : une position se ferme à $0.60 grâce à un SELL copié
- PnL réalisé : **+$0.20** (cristallisé, locked in, ne peut plus bouger)
- PnL latent : **+$10** (mark-to-market des positions toujours ouvertes)
- Exposition : $998 (une position fermée a libéré un peu de capital)

Avant M13 l'utilisateur ne voyait que le premier chiffre (PnL réalisé $0) et pensait que le bot ne faisait rien. Avec M13 il comprend la mécanique complète.

### 3.3 Story C — M8 v2 ferme automatiquement une position neg_risk résolue pendant la nuit

**2026-04-24 03h00 UTC** : l'Académie annonce le gagnant des Oscars 2026. Polymarket met à jour les 10 marchés neg_risk de l'event "Best Picture 2026" :
- "Will X win Best Picture?" pour le winner → `closed=true, outcomePrices=["1.0","0.0"]`
- "Will Y win Best Picture?" pour les 9 losers → `closed=true, outcomePrices=["0.0","1.0"]`

Le bot a 3 positions virtuelles sur cet event (2 YES sur des losers, 1 YES sur le winner) ouvertes depuis 2 semaines.

**Avant M13** : rien ne se passe. Toutes les 30 min, `DryRunResolutionWatcher.run_once()` liste les 3 positions, voit `market.neg_risk=true`, logue `dry_run_resolution_neg_risk_unsupported`, continue. Positions ouvertes à l'infini.

**Après M13 M8 v2** : 03h30 UTC (prochain cycle du watcher)
- Winner position (avg_price $0.30, size 5) : `payout = 1.0`, `realized_pnl = (1.0 − 0.30) × 5 = +$3.50`. Close.
- Loser position 1 (avg_price $0.15, size 3) : `payout = 0.0`, `realized_pnl = (0.0 − 0.15) × 3 = −$0.45`. Close.
- Loser position 2 (avg_price $0.20, size 4) : `payout = 0.0`, `realized_pnl = (0.0 − 0.20) × 4 = −$0.80`. Close.

3 alertes Telegram `dry_run_market_resolved_neg_risk` envoyées avec PnL détaillé. Le matin l'utilisateur voit +$2.25 net sur ces 3 positions dans /home PnL réalisé.

### 3.4 Story D — Recap Telegram matinal cohérent (Bug 7)

**Avant M13** (recap observé 2026-04-23 18h) :
```
💼 Exécution
• Ordres : 688 envoyés · 0 remplis · 10 rejetés
• Volume exécuté : $0.00
```

Contradiction flagrante : si 688 ordres ont été envoyés mais 0 remplis, comment a-t-on $1.2k de volume sur /home ? (Réponse : `volume_executed_usd` filtre sur `FILLED`/`PARTIALLY_FILLED` uniquement, 0 match en dry-run où tout est `SIMULATED`.)

**Après M13 Bug 7** :
```
💼 Exécution
• Ordres : 688 envoyés · 688 remplis virtuels · 10 rejetés
• Volume exécuté : $1182.45
```

Cohérent avec /home. Le mot "remplis virtuels" remplace "remplis" en dry-run pour clarifier (optionnel, tolérable "remplis" tout court).

---

## 4. Architecture

### 4.1 Flux global M13 (5 sujets)

```
                         ┌──────────────────────────────────────────────┐
                         │  Pipeline strategy (M2)                       │
                         │                                               │
  DetectedTrade  ──────▶ │  TraderLifecycle → Market → EntryPrice       │
                         │       → PositionSizer [Bug 5: side-aware]    │
                         │       → SlippageChecker → RiskManager         │
                         │                                               │
                         └──────────────┬───────────────────────────────┘
                                        │ OrderApproved
                                        ▼
                         ┌──────────────────────────────────────────────┐
                         │  Executor dry-run (M3/M8)                     │
                         │                                               │
                         │  _persist_realistic_simulated()               │
                         │       → upsert_virtual(BUY | SELL)            │
                         │            └── Bug 1 fix (shippé pré-M13)     │
                         │                computes realized_pnl on close │
                         └──────────────┬───────────────────────────────┘
                                        │ MyPosition (closed_at, realized_pnl)
                                        ▼
                         ┌──────────────────────────────────────────────┐
                         │  DryRunResolutionWatcher (M8, 30 min cycle)   │
                         │                                               │
                         │  _run_once()                                  │
                         │  ├── list_open_virtual()                      │
                         │  ├── fetch Gamma markets batch                │
                         │  ├── for each resolved market:                │
                         │  │     ├── binary M8 v1    ──┐                │
                         │  │     └── neg_risk M8 v2  ──┼─ same logic    │
                         │  │                          │                 │
                         │  │     → close_virtual(realized_pnl)          │
                         │  │     → emit Telegram alert                  │
                         │  └── [M8 v2] new event dry_run_market_resolved_neg_risk │
                         └──────────────┬───────────────────────────────┘
                                        │ closed positions with PnL
                                        ▼
                         ┌──────────────────────────────────────────────┐
                         │  Dashboard queries (M6)                       │
                         │                                               │
                         │  get_home_alltime_stats(pnl_mode)             │
                         │  ├── realized_pnl_total     (existant)        │
                         │  ├── volume_usd_total       (Bug 2 shippé)    │
                         │  ├── fills_count            (Bug 2 shippé)    │
                         │  ├── open_exposition_usd    [Bug 6 new]       │
                         │  ├── open_max_profit_usd    [Bug 6 new]       │
                         │  ├── open_latent_pnl_usd    [PnL latent new]  │
                         │  └── win_rate_pct           [Bug 6 new]       │
                         └──────────────┬───────────────────────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────────────────────┐
                         │  /home cards rendering (home.html)            │
                         │  → 6 KPI cards ligne 1 + 7 stats ligne 2      │
                         └───────────────────────────────────────────────┘

         ┌───────────────────────────────────────────────────────────┐
         │  Parallel: Telegram daily summary (M7)                     │
         │                                                            │
         │  daily_summary_queries._orders_stats_since()               │
         │  └── [Bug 7] filter FILLED|SIMULATED based on              │
         │             settings.execution_mode                         │
         └────────────────────────────────────────────────────────────┘
```

### 4.2 Fichiers touchés

Tous les changements sont **additifs** ou **in-place** dans des fichiers existants. Aucun nouveau module.

| Module | Type de changement | Lignes estimées |
|---|---|---|
| [src/polycopy/strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py) | `PositionSizer.check()` refactor side-aware | +40 / -15 |
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | Étend `HomeAllTimeStats` + `get_home_alltime_stats` | +60 / -0 |
| [src/polycopy/dashboard/templates/home.html](../../src/polycopy/dashboard/templates/home.html) | +4 cartes KPI | +40 / -0 |
| [src/polycopy/monitoring/daily_summary_queries.py](../../src/polycopy/monitoring/daily_summary_queries.py) | `_orders_stats_since` mode-aware | +15 / -5 |
| [src/polycopy/executor/dry_run_resolution_watcher.py](../../src/polycopy/executor/dry_run_resolution_watcher.py) | Supprime skip neg_risk + émet alert | +25 / -8 |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +2 settings (`dry_run_neg_risk_resolution_enabled`, `dry_run_initial_capital_usd`) | +25 / -0 |
| [src/polycopy/monitoring/dtos.py](../../src/polycopy/monitoring/dtos.py) | +1 Alert template `dry_run_market_resolved_neg_risk` | +5 / -0 |
| [assets/telegram/](../../assets/telegram/) ou [src/polycopy/monitoring/templates/](../../src/polycopy/monitoring/templates/) | +1 template `dry_run_market_resolved_neg_risk.md.j2` | +15 / -0 |
| tests/unit/ | +15-18 tests ciblés | +400 / -0 |

### 4.3 Dépendances avec autres milestones

- **M5_ter** (watcher live-reload) et **M5_bis** (competitive eviction) : **aucune modification**. M13 ne touche ni au watcher ni au discovery/eviction orchestrator.
- **M11** (pipeline temps réel, `clob_ws_client`) : **aucune modification**. Les stages de latence restent inchangés.
- **M12** (scoring v2) : **aucune modification**. M13 lit les scores existants pour le top trader card, rien de nouveau.
- **M12_bis** (remote control + multi-machine) : **aucune modification**. Les alertes `dry_run_market_resolved_neg_risk` bénéficient automatiquement du `MACHINE_ID` et du `dashboard_url` injectés par le `AlertRenderer`.

---

## 5. Algorithmes

### 5.1 Bug 5 — `PositionSizer` side-aware

**Fichier** : [src/polycopy/strategy/pipeline.py:128-179](../../src/polycopy/strategy/pipeline.py#L128-L179).

**Comportement actuel** (un seul check, side-agnostic) :
```python
async def check(self, ctx: PipelineContext) -> FilterResult:
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

**Nouveau comportement M13** :

```python
async def check(self, ctx: PipelineContext) -> FilterResult:
    if ctx.trade.side == "BUY":
        return await self._check_buy(ctx)
    return await self._check_sell(ctx)

async def _check_buy(self, ctx: PipelineContext) -> FilterResult:
    """BUY : check coarse sur condition_id (pas de double-buy YES/NO).

    Préserve strictement le comportement M2..M12 — les tests existants
    ne doivent pas régresser.
    """
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
    ctx.my_size = min(raw_size, cap_size)
    if ctx.my_size <= 0:
        return FilterResult(passed=False, reason="size_zero")
    return FilterResult(passed=True)

async def _check_sell(self, ctx: PipelineContext) -> FilterResult:
    """SELL : match fin sur (condition_id, asset_id) pour ne fermer que la
    position correspondante. Un SELL YES sur cond X ne ferme pas une
    position NO sur cond X (asset_id différent).

    Sizing : proportional à copy_ratio, cappé à existing.size (on ne vend
    jamais plus qu'on détient). Cap max_position_usd N/A pour SELL (on
    ferme une position existante, on ne la dimensionne pas).
    """
    async with self._session_factory() as session:
        stmt = select(MyPosition).where(
            MyPosition.condition_id == ctx.trade.condition_id,
            MyPosition.asset_id == ctx.trade.asset_id,
            MyPosition.closed_at.is_(None),
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        # Orphan SELL — source wallet vend un asset qu'on n'a jamais BUYé.
        # Cause typique : source wallet trading avant qu'on découvre/pinne
        # ce trader. Distinct du warning executor `dry_run_sell_without_position`
        # qui lui couvre les oversells (SELL size > existing.size).
        return FilterResult(passed=False, reason="sell_without_position")
    raw_size = ctx.trade.size * self._settings.copy_ratio
    ctx.my_size = min(raw_size, float(existing.size))
    if ctx.my_size <= 0:
        return FilterResult(passed=False, reason="size_zero")
    return FilterResult(passed=True)
```

**Edge cases à couvrir** :
- SELL avec `existing.size == 0.0` mais `closed_at IS NULL` (cas théorique, jamais observé en prod) : `min(raw_size, 0.0) = 0.0` → reject `size_zero`. Acceptable.
- SELL exactement sur la bonne (cond, asset) avec `source.size × copy_ratio > existing.size` : oversell proportional capé à `existing.size`. L'executor M8 gère le reste via mon fix Bug 1 shippé (`closed_portion = min(size_filled, size_before)`).
- BUY existant sur YES, SELL reçu sur NO (sister asset même cond) : le nouveau check fin matche `(cond, asset_NO)` qui n'existe pas → reject `sell_without_position`. Conservateur et sûr — un SELL NO ne clôture pas une position YES.

**Reason codes** :
- `position_already_open` : réservé aux BUYs (comportement M2..M12 préservé).
- `sell_without_position` : **nouveau**, pour les SELL orphelins sans position matching. Distinct du warning executor `dry_run_sell_without_position` (post-strategy, pour les oversells détectés dans `upsert_virtual`).

### 5.2 Bug 6 — 3 nouvelles cartes /home (`Exposition`, `Gain max`, `Win rate`)

**Fichiers** :
- [src/polycopy/dashboard/queries.py:120](../../src/polycopy/dashboard/queries.py#L120) — DTO `HomeAllTimeStats`.
- [src/polycopy/dashboard/queries.py:749](../../src/polycopy/dashboard/queries.py#L749) — `get_home_alltime_stats()`.
- [src/polycopy/dashboard/templates/home.html](../../src/polycopy/dashboard/templates/home.html) — rendu cartes.

**DTO extension** (section 6.1) + nouvelle logique de requête :

```python
# Dans get_home_alltime_stats, après le bloc volume/fills :

# --- Bug 6 / PnL latent : agrégats sur positions OUVERTES filtrés par mode.
open_filter = [MyPosition.closed_at.is_(None)]
if pnl_mode == "real":
    open_filter.append(MyPosition.simulated.is_(False))
elif pnl_mode == "dry_run":
    open_filter.append(MyPosition.simulated.is_(True))

open_stats = (
    await session.execute(
        select(
            func.coalesce(func.sum(MyPosition.size * MyPosition.avg_price), 0.0).label(
                "exposition"
            ),
            func.coalesce(func.sum(MyPosition.size * (1.0 - MyPosition.avg_price)), 0.0).label(
                "max_profit"
            ),
        ).where(*open_filter)
    )
).first()
open_exposition_usd = float(open_stats.exposition) if open_stats else 0.0
open_max_profit_usd = float(open_stats.max_profit) if open_stats else 0.0

# --- Bug 6 win rate : agrégat wins/losses sur positions fermées avec realized_pnl.
closed_filter = [
    MyPosition.closed_at.is_not(None),
    MyPosition.realized_pnl.is_not(None),
]
if pnl_mode == "real":
    closed_filter.append(MyPosition.simulated.is_(False))
elif pnl_mode == "dry_run":
    closed_filter.append(MyPosition.simulated.is_(True))

closed_rows = (
    await session.execute(select(MyPosition.realized_pnl).where(*closed_filter))
).scalars().all()
wins = sum(1 for p in closed_rows if float(p) > 0)
losses = sum(1 for p in closed_rows if float(p) < 0)
decided = wins + losses
win_rate_pct: float | None = (wins / decided * 100.0) if decided > 0 else None
```

**Placement dans le template** ([home.html](../../src/polycopy/dashboard/templates/home.html)) :

- **Ligne 1 (état actuel)** : `TOTAL USDC` / `DRAWDOWN` / `POSITIONS OUVERTES` / **`Exposition`** / **`Gain max latent`** / **`PnL latent`** / `TRADES DÉTECTÉS`. Grid Tailwind `grid-cols-4 xl:grid-cols-7` avec wrap mobile ligne 2 pour les écrans < xl.
- **Ligne 2 (stats all-time)** : `PNL RÉALISÉ` / **`Win rate`** / `VOLUME TRADÉ` / `FILLS` / `APPROVE STRATÉGIE` / `TOP TRADER` / `DURÉE DE RUN`. Même grid.

Icônes Lucide :
- Exposition → `shield` (capital protégé/exposé)
- Gain max latent → `trending-up`
- PnL latent → `activity` (pulse ; distinct du `dollar-sign` du PnL réalisé)
- Win rate → `target`

Format : `format_usd` pour les 3 dollars, `format_pct(with_sign=False)` pour le winrate. `None` → `—` via le pattern existant.

**Edge cases** :
- Aucune position ouverte → `exposition = 0`, `max_profit = 0`, `latent_pnl = total_usdc - initial_capital - realized_pnl`.
- Aucune position fermée → `win_rate_pct = None` → affiche `—`.
- Toutes positions fermées à `realized_pnl = 0` exactement → `wins=0, losses=0, win_rate=None`. Acceptable.
- Mode `real` sans position live → tous les nouveaux champs à 0 / None (cohérent avec les autres champs `only_real` existants).

### 5.3 Bug 7 — Telegram daily recap volume mode-aware

**Fichier** : [src/polycopy/monitoring/daily_summary_queries.py:169-190](../../src/polycopy/monitoring/daily_summary_queries.py#L169-L190).

**Comportement actuel** : filtre strict sur `status IN ("FILLED", "PARTIALLY_FILLED")`. En dry-run, 0 match.

**Nouveau comportement** : cascade basée sur `settings.execution_mode` :

```python
async def _orders_stats_since(
    session_factory: async_sessionmaker[AsyncSession],
    since: datetime,
    settings: Settings,  # nouveau paramètre
) -> tuple[int, int, int, float]:
    # Bug 7 fix : en dry-run, les ordres "remplis virtuels" ont status
    # SIMULATED (M8 realistic_fill). Filtrer selon execution_mode pour
    # que le Telegram recap reste cohérent avec le dashboard.
    if settings.execution_mode == "live":
        filled_statuses = ("FILLED", "PARTIALLY_FILLED")
    else:  # dry_run OR simulation (même sémantique volume)
        filled_statuses = ("SIMULATED",)

    async with session_factory() as session:
        stmt = (
            select(MyOrder.status, func.count(MyOrder.id).label("n"))
            .where(MyOrder.sent_at >= since)
            .group_by(MyOrder.status)
        )
        counts = {row[0]: int(row[1]) for row in (await session.execute(stmt)).all()}
        volume_stmt = select(
            func.coalesce(func.sum(MyOrder.size * MyOrder.price), 0.0),
        ).where(
            MyOrder.sent_at >= since,
            MyOrder.status.in_(filled_statuses),
        )
        volume = float((await session.execute(volume_stmt)).scalar_one() or 0.0)

    sent = sum(counts.get(s, 0) for s in ("SENT", "FILLED", "PARTIALLY_FILLED", "SIMULATED"))
    filled = sum(counts.get(s, 0) for s in filled_statuses)
    rejected = counts.get("REJECTED", 0) + counts.get("FAILED", 0)
    return sent, filled, rejected, volume
```

**Propagation** : `build_daily_summary_context()` doit recevoir et transmettre `settings` à `_orders_stats_since`. Signature déjà en place — `build_daily_summary_context` a accès à `settings`.

**Wording template** (optionnel) : remplacer `{{ orders_filled }} remplis` par `{{ orders_filled }} {% if is_dry_run %}remplis virtuels{% else %}remplis{% endif %}` dans [daily_summary.md.j2:23](../../src/polycopy/monitoring/templates/daily_summary.md.j2). Nécessite d'ajouter `is_dry_run: bool` au `DailySummaryContext` (dérivé de `settings.execution_mode != "live"`). V1 sans ce wording = acceptable.

### 5.4 PnL latent — 4ᵉ carte /home

**Définition** :
```
latent_pnl = total_usdc − initial_capital − realized_pnl
```

Où :
- `total_usdc` = valeur actuelle du wallet virtuel (cash disponible + mark-to-market des positions ouvertes). Lu du dernier `PnlSnapshot` ou calculé via `VirtualWalletStateReader` au moment de la requête.
- `initial_capital` = capital de départ. Source :
  1. Setting explicite `DRY_RUN_INITIAL_CAPITAL_USD` si défini.
  2. Sinon fallback sur `PnlSnapshot.total_usdc` le plus ancien (via `SELECT total_usdc FROM pnl_snapshots ORDER BY timestamp ASC LIMIT 1`).
  3. Sinon fallback final sur `settings.risk_available_capital_usd_stub` (= 1000.0 par défaut).
- `realized_pnl` = déjà calculé dans `get_home_alltime_stats()`, même mode-aware.

**Algo** (dans `get_home_alltime_stats`, après le calcul `realized_pnl_total` existant) :

```python
# PnL latent : total_usdc courant − capital initial − PnL réalisé.
# Représente le mark-to-market non cristallisé des positions ouvertes.
# Peut être positif (les prix ont monté en notre faveur) ou négatif.

latest_snapshot_total = (
    await session.execute(
        select(PnlSnapshot.total_usdc).order_by(PnlSnapshot.timestamp.desc()).limit(1)
    )
).scalar_one_or_none()

if latest_snapshot_total is None:
    # Pas de snapshot → pas de signal latent possible (bot vient de démarrer).
    open_latent_pnl_usd = 0.0
else:
    # Détermine initial_capital selon la cascade.
    if settings.dry_run_initial_capital_usd is not None:
        initial_capital = settings.dry_run_initial_capital_usd
    else:
        oldest = (
            await session.execute(
                select(PnlSnapshot.total_usdc).order_by(PnlSnapshot.timestamp.asc()).limit(1)
            )
        ).scalar_one_or_none()
        initial_capital = (
            float(oldest) if oldest is not None else settings.risk_available_capital_usd_stub
        )
    open_latent_pnl_usd = float(latest_snapshot_total) - initial_capital - realized_pnl_total
```

**Note : ce calcul dépend de `settings`**, donc `get_home_alltime_stats` doit accepter `settings` en paramètre. Aujourd'hui il prend seulement `session_factory` et `pnl_mode` — étendre la signature (backwards compat : rendre `settings` optionnel avec default `None` → comportement actuel sans PnL latent).

**Edge cases** :
- Moins d'1 snapshot en DB → `open_latent_pnl_usd = 0.0`. L'utilisateur voit "0" et sait que le bot démarre.
- `initial_capital` défini mais incohérent avec `PnlSnapshot` historiques (ex: user change la valeur au milieu du test) → on accepte la valeur setting-first. Le PnL latent peut devenir faux mais c'est documenté.
- Mode `real` avec `total_usdc` live → même formule, sourced depuis les vraies valeurs wallet on-chain via `WalletStateReader`. Cohérent.

### 5.5 M8 v2 — résolution neg_risk

**Fichier** : [src/polycopy/executor/dry_run_resolution_watcher.py](../../src/polycopy/executor/dry_run_resolution_watcher.py).

**Le fix principal** (diff minimal) :

```python
# Avant (ligne 90-96) :
if market.neg_risk:
    log.warning(
        "dry_run_resolution_neg_risk_unsupported",
        asset_id=pos.asset_id,
        condition_id=pos.condition_id,
    )
    continue

# Après :
if market.neg_risk and not self._settings.dry_run_neg_risk_resolution_enabled:
    # Flag opt-out si l'utilisateur veut revenir au comportement M8 v1.
    log.debug(
        "dry_run_resolution_neg_risk_skipped_by_flag",
        asset_id=pos.asset_id,
        condition_id=pos.condition_id,
    )
    continue
```

**Logique de résolution partagée** : les fonctions `_winning_outcome_index` et `_resolution_payout` (lignes 122-153) fonctionnent **déjà** pour les marchés neg_risk sans modification, parce que structurellement :
- Un marché neg_risk a 2 outcomes (`outcomes=["Yes","No"]`).
- 2 `clobTokenIds` (YES token + NO token pour le candidat).
- `outcome_prices` converge vers `["1.0","0.0"]` ou `["0.0","1.0"]` à résolution.
- Le check `max(prices) >= 0.99` dans `_winning_outcome_index` filtre les états "closed mais pas encore matérialisés" (bug Gamma connu où `closed=true` précède la mise à jour des prix finaux).

**Alert Telegram** : nouvelle branche conditionnelle après résolution :

```python
payout = _resolution_payout(pos, market, winning_idx)
realized_pnl = (payout - pos.avg_price) * pos.size
await self._positions_repo.close_virtual(
    pos.id,
    closed_at=resolved_at,
    realized_pnl=realized_pnl,
)
log.info(
    "dry_run_position_resolved",
    asset_id=pos.asset_id,
    condition_id=pos.condition_id,
    size=pos.size,
    avg_price=pos.avg_price,
    payout=payout,
    realized_pnl=realized_pnl,
    neg_risk=market.neg_risk,  # nouveau champ pour filtrage structlog
)

# M13 M8 v2 : émettre une alerte Telegram distincte pour les résolutions
# neg_risk — aide l'utilisateur à tracer les gros events multi-outcomes.
if market.neg_risk and self._alerts_queue is not None:
    self._alerts_queue.put_nowait(
        Alert(
            level="INFO",
            event="dry_run_market_resolved_neg_risk",
            body={
                "condition_id": pos.condition_id,
                "question": market.question,
                "asset_id": pos.asset_id,
                "size": pos.size,
                "avg_price": pos.avg_price,
                "payout": payout,
                "realized_pnl": realized_pnl,
            },
            cooldown_key=f"neg_risk_resolved_{pos.condition_id}",
        )
    )
```

**Dépendance nouvelle** : `DryRunResolutionWatcher.__init__` doit accepter un `alerts_queue: asyncio.Queue[Alert] | None` (déjà pattern standard dans les autres orchestrators). Si absent, skip l'alerte silencieusement — pas de crash.

**Template Telegram** ([src/polycopy/monitoring/templates/dry_run_market_resolved_neg_risk.md.j2](../../src/polycopy/monitoring/templates/dry_run_market_resolved_neg_risk.md.j2), à créer) :

```jinja
📊 *Marché neg\_risk résolu*

{% if question %}*{{ question | telegram_md_escape }}*{% endif %}
• PnL réalisé : *{{ realized_pnl | format_usd_tg }}*
• Taille : {{ size | round(2) }} @ {{ avg_price | round(3) }}
• Payout : {{ payout | round(2) }} USDC/share
• Condition : `{{ condition_id[:10] ~ '...' ~ condition_id[-6:] }}`
```

**Gestion race condition SELL vs resolver** : cf. §14.3 pour la discussion détaillée.

**Positions orphelines historiques (les 512 actuelles)** : cf. §11.3 pour le backfill script optionnel.

---

## 6. DTOs / signatures

### 6.1 `HomeAllTimeStats` étendu

[src/polycopy/dashboard/queries.py:120](../../src/polycopy/dashboard/queries.py#L120) actuellement :

```python
@dataclass(frozen=True)
class HomeAllTimeStats:
    realized_pnl_total: float
    volume_usd_total: float
    fills_count: int
    fills_rate_pct: float | None
    strategy_approve_rate_pct: float | None
    top_trader: dict[str, float | str | None] | None
    uptime: timedelta | None
```

Nouveau (ajout en fin de dataclass) :

```python
@dataclass(frozen=True)
class HomeAllTimeStats:
    realized_pnl_total: float
    volume_usd_total: float
    fills_count: int
    fills_rate_pct: float | None
    strategy_approve_rate_pct: float | None
    top_trader: dict[str, float | str | None] | None
    uptime: timedelta | None
    # --- M13 Bug 6 + PnL latent (nouveaux) ---
    open_exposition_usd: float = 0.0
    open_max_profit_usd: float = 0.0
    open_latent_pnl_usd: float = 0.0
    win_rate_pct: float | None = None
```

Defaults non-cassants pour les tests existants qui instancient directement le DTO sans les nouveaux champs.

### 6.2 Signature `get_home_alltime_stats`

Actuelle :

```python
async def get_home_alltime_stats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pnl_mode: Literal["real", "dry_run", "both"] = "both",
) -> HomeAllTimeStats: ...
```

Nouvelle :

```python
async def get_home_alltime_stats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pnl_mode: Literal["real", "dry_run", "both"] = "both",
    settings: Settings | None = None,  # nouveau, nullable pour compat tests
) -> HomeAllTimeStats: ...
```

Si `settings is None`, le PnL latent n'est pas calculé (reste à 0.0) — comportement pré-M13 préservé pour les tests unitaires qui n'injectent pas `settings`. Le caller `/home` route passe toujours `settings` depuis la dépendance FastAPI.

### 6.3 Signature `_orders_stats_since` (Bug 7)

Actuelle :

```python
async def _orders_stats_since(
    session_factory: async_sessionmaker[AsyncSession],
    since: datetime,
) -> tuple[int, int, int, float]: ...
```

Nouvelle :

```python
async def _orders_stats_since(
    session_factory: async_sessionmaker[AsyncSession],
    since: datetime,
    settings: Settings,  # nouveau paramètre obligatoire
) -> tuple[int, int, int, float]: ...
```

Le caller `build_daily_summary_context` a déjà `settings` en scope (ligne 29).

### 6.4 Signature `DryRunResolutionWatcher.__init__` (M8 v2)

Actuelle :

```python
def __init__(
    self,
    session_factory: async_sessionmaker[AsyncSession],
    gamma_client: GammaApiClient,
    settings: Settings,
) -> None: ...
```

Nouvelle :

```python
def __init__(
    self,
    session_factory: async_sessionmaker[AsyncSession],
    gamma_client: GammaApiClient,
    settings: Settings,
    alerts_queue: "asyncio.Queue[Alert] | None" = None,  # nouveau, nullable
) -> None: ...
```

`alerts_queue=None` = pas d'alerte Telegram émise (comportement M8 v1 pur). Le caller `ExecutorOrchestrator.run_forever` passe l'alerts queue existante.

### 6.5 Nouveaux reason codes strategy

- `sell_without_position` : nouveau, filtre `PositionSizer._check_sell`. Distinct du warning executor `dry_run_sell_without_position` (qui reste).

### 6.6 Nouveaux event types monitoring

- `dry_run_market_resolved_neg_risk` : nouveau, émis par `DryRunResolutionWatcher` sur résolution d'un marché neg_risk. Level `INFO`. Cooldown key `f"neg_risk_resolved_{condition_id}"` (idempotent par marché, ne spam pas).

---

## 7. Settings

Nouvelles env vars (toutes optionnelles, backwards compat totale) :

| Variable env | Champ Settings | Default | Description |
|---|---|---|---|
| `DRY_RUN_NEG_RISK_RESOLUTION_ENABLED` | `dry_run_neg_risk_resolution_enabled: bool` | `true` | M8 v2 : active la résolution automatique des marchés neg_risk. Si `false`, comportement M8 v1 préservé (skip + warning). Opt-out strict — l'utilisateur peut désactiver si une régression neg_risk apparaît sur sa machine. |
| `DRY_RUN_INITIAL_CAPITAL_USD` | `dry_run_initial_capital_usd: float \| None` | `None` | Capital initial explicite pour le calcul PnL latent. Si `None`, fallback sur `PnlSnapshot.total_usdc` le plus ancien. Si aucun snapshot, fallback final sur `risk_available_capital_usd_stub`. Range Pydantic : `Field(default=None, ge=10.0, le=10_000_000.0)`. |

**`.env.example` à mettre à jour** avec commentaires :
```bash
# M13 — Résolution automatique des marchés neg_risk (opt-out)
# DRY_RUN_NEG_RISK_RESOLUTION_ENABLED=true

# M13 — Capital initial explicite pour le calcul PnL latent
# DRY_RUN_INITIAL_CAPITAL_USD=1000.0
```

Aucune autre variable ne change de default — backwards compat stricte.

---

## 8. Invariants sécurité

### 8.1 Triple garde-fou M3 préservé

**Confirmer** : aucun des 5 sujets M13 ne touche au chemin live :
- Bug 5 : modifie `PositionSizer` côté strategy (M2). La strategy est read-only à M2 et M13, aucune signature, aucun POST CLOB.
- Bug 6/7/PnL latent : queries dashboard et monitoring, read-only sur DB uniquement.
- M8 v2 : extension du `DryRunResolutionWatcher` qui lit Gamma (publique, no-auth). La fonction `close_virtual` est appelée depuis le watcher pour des positions `simulated=True` uniquement — garde-fou Pydantic déjà en place [repositories.py:743](../../src/polycopy/storage/repositories.py#L743) :
  ```python
  if not position.simulated:
      raise ValueError(...)
  ```

**Aucune nouvelle surface de signature, aucun nouveau cred consommé**. Les quatre garde-fous Executor M3 (lazy init ClobClient, RuntimeError si dry_run=false + creds absents, double check avant create_and_post_order, WalletStateReader re-fetch) restent inchangés.

### 8.2 Pas de fuite de secret

Vérifier via [test_dashboard_security.py](../../tests/unit/test_dashboard_security.py) et [test_dashboard_security_m6.py](../../tests/unit/test_dashboard_security_m6.py) que les nouveaux champs (`open_exposition_usd`, `open_max_profit_usd`, `open_latent_pnl_usd`, `win_rate_pct`) ne laissent pas leak les env vars sensibles : `POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, `CLOB_API_SECRET`, `REMOTE_CONTROL_TOTP_SECRET`.

**Grep automatique à ajouter** : `assert SENSITIVE_MARKER not in response.text` sur /home, /api/home-stats, et tous les nouveaux partials. Les tests existants couvrent déjà le template — étendre avec les 4 nouveaux champs.

### 8.3 Reason code `sell_without_position` n'expose aucun PII

Le reason code est une string littérale. Les logs structlog et la table `strategy_decisions` stockent `(tx_hash, wallet_address, decision, reason)` — tous champs publics on-chain. Aucun PII introduit.

### 8.4 Alerte Telegram `dry_run_market_resolved_neg_risk`

Le template liste : `question`, `condition_id` (short hash), `size`, `avg_price`, `payout`, `realized_pnl`. Tous champs publics. Le `question` est user-controlled côté Polymarket (polymarket.com) mais déjà échappé via `| telegram_md_escape` cohérent M7. Aucun secret ne transite.

### 8.5 Fichier fixture neg_risk

[tests/fixtures/gamma_neg_risk_resolved_sample.json](../../tests/fixtures/gamma_neg_risk_resolved_sample.json) est synthétique (dérivé du sample public `gamma_market_sample.json`, champs non-sensibles). Pas d'adresses wallet, pas de tokens, pas de signatures.

---

## 9. Test plan

### 9.1 Bug 5 — `PositionSizer` side-aware (6 tests)

Tous dans [tests/unit/test_strategy_pipeline.py](../../tests/unit/test_strategy_pipeline.py). Utiliser les fixtures `_trade()` et `_settings()` existantes.

1. **`test_position_sizer_buy_existing_rejected`** — régression guard.
   - Preconditions : 1 `MyPosition` ouverte sur `cond=0xc, asset=A` (simulated=False).
   - Action : `check()` sur BUY `cond=0xc, asset=A, size=10, price=0.5`.
   - Assertion : `FilterResult(passed=False, reason="position_already_open")`, `ctx.my_size is None`.

2. **`test_position_sizer_buy_new_passes_with_cap`** — existant préservé.
   - Preconditions : DB vide.
   - Action : `check()` BUY `size=10000, price=0.5, copy_ratio=0.01, max_position_usd=100` → cap size = 200.
   - Assertion : `passed=True`, `ctx.my_size == 100.0` (raw=100 < cap=200, prend raw).

3. **`test_position_sizer_sell_matches_open_position`** — core Bug 5 happy path.
   - Preconditions : 1 `MyPosition` ouverte (cond=0xc, asset=A, size=10, simulated=True).
   - Action : SELL `cond=0xc, asset=A, source.size=1000, price=0.6, copy_ratio=0.01` → raw=10.
   - Assertion : `passed=True`, `ctx.my_size == 10.0` (cappé à existing.size).

4. **`test_position_sizer_sell_proportional_when_source_smaller`** — proportional sub-cap.
   - Preconditions : 1 `MyPosition` ouverte (size=10).
   - Action : SELL `source.size=500, copy_ratio=0.01` → raw=5.
   - Assertion : `passed=True`, `ctx.my_size == 5.0` (pas capé, proportional strict).

5. **`test_position_sizer_sell_orphan_rejected`** — core Bug 5 edge.
   - Preconditions : DB vide.
   - Action : SELL `cond=0xc, asset=A, size=1000, price=0.6`.
   - Assertion : `FilterResult(passed=False, reason="sell_without_position")`.

6. **`test_position_sizer_sell_wrong_asset_rejected`** — asset match strict.
   - Preconditions : 1 `MyPosition` ouverte (cond=0xc, asset=A).
   - Action : SELL `cond=0xc, asset=B` (sister token même cond).
   - Assertion : `FilterResult(passed=False, reason="sell_without_position")`.

### 9.2 Bug 6 — 3 cartes /home (4 tests)

Dans [tests/unit/test_dashboard_queries.py](../../tests/unit/test_dashboard_queries.py).

7. **`test_home_alltime_stats_exposition_and_gain_max_dry_run`**
   - Preconditions : seed 2 positions virtuelles ouvertes : (size=10, avg=0.30), (size=5, avg=0.80).
   - Action : `get_home_alltime_stats(pnl_mode="dry_run")`.
   - Assertion : `open_exposition_usd == 10*0.30 + 5*0.80 == 7.0`, `open_max_profit_usd == 10*0.70 + 5*0.20 == 8.0`.

8. **`test_home_alltime_stats_exposition_ignores_closed_positions`**
   - Preconditions : 1 position closed avec `realized_pnl=5.0`, 1 position ouverte (size=10, avg=0.30).
   - Action : `get_home_alltime_stats(pnl_mode="dry_run")`.
   - Assertion : `open_exposition_usd == 3.0` (seule la position ouverte compte), `open_max_profit_usd == 7.0`.

9. **`test_home_alltime_stats_win_rate_dry_run`**
   - Preconditions : 3 virtual closed avec `realized_pnl` = +2.0, +3.0, −1.0.
   - Action : `get_home_alltime_stats(pnl_mode="dry_run")`.
   - Assertion : `win_rate_pct == 66.666...` (2 wins / 3 decided).

10. **`test_home_alltime_stats_win_rate_none_when_no_closed`**
    - Preconditions : 0 closed position.
    - Action : `get_home_alltime_stats(pnl_mode="dry_run")`.
    - Assertion : `win_rate_pct is None`.

### 9.3 Bug 7 — Telegram daily summary (2 tests)

Dans [tests/unit/test_daily_summary_queries.py](../../tests/unit/test_daily_summary_queries.py) (à créer ou étendre le fichier existant).

11. **`test_daily_summary_orders_stats_dry_run_counts_simulated`**
    - Preconditions : seed 3 `SIMULATED` (total volume $45) + 1 `REJECTED`. Settings mode `dry_run`.
    - Action : `_orders_stats_since(sf, since=epoch, settings=dry_run_settings)`.
    - Assertion : `(sent, filled, rejected, volume) == (3, 3, 1, 45.0)`.

12. **`test_daily_summary_orders_stats_live_counts_filled`** — régression guard.
    - Preconditions : seed 2 `FILLED` + 1 `SIMULATED`. Settings mode `live`.
    - Action : idem.
    - Assertion : `filled == 2`, `volume == Σ(2 FILLED)` seulement. Le SIMULATED est dans `sent` mais pas dans `filled`.

### 9.4 PnL latent — 4ᵉ carte /home (2 tests)

13. **`test_home_alltime_stats_latent_pnl_with_explicit_initial_capital`**
    - Preconditions : insert 2 `PnlSnapshot` (total_usdc=1000, puis total_usdc=1050). Settings `dry_run_initial_capital_usd=1000`. 1 position virtual closed `realized_pnl=10`.
    - Action : `get_home_alltime_stats(pnl_mode="dry_run", settings=...)`.
    - Assertion : `open_latent_pnl_usd == 1050 - 1000 - 10 == 40.0`.

14. **`test_home_alltime_stats_latent_pnl_fallback_oldest_snapshot`**
    - Preconditions : 2 `PnlSnapshot` (950, 980). `dry_run_initial_capital_usd=None`. 0 closed.
    - Action : idem.
    - Assertion : `open_latent_pnl_usd == 980 - 950 - 0 == 30.0` (fallback sur le plus ancien).

### 9.5 M8 v2 — résolution neg_risk (5 tests)

Dans [tests/unit/test_dry_run_resolution_watcher.py](../../tests/unit/test_dry_run_resolution_watcher.py) (étendre le fichier existant).

15. **`test_resolution_binary_still_works`** — régression M8 v1.
    - Preconditions : 1 virtual pos (cond=0xB, asset=A, size=5, avg=0.40). Gamma retourne market binaire `closed=true, neg_risk=false, outcomePrices=["1.0","0.0"], clobTokenIds=[A, B]`.
    - Action : `_run_once()`.
    - Assertion : position fermée avec `realized_pnl = (1.0 - 0.40) × 5 = 3.0`.

16. **`test_resolution_neg_risk_yes_wins`** — happy path M8 v2.
    - Preconditions : 1 virtual pos (cond=0xN, asset=Y, size=10, avg=0.25). Gamma retourne `neg_risk=true, closed=true, outcomePrices=["1.0","0.0"], clobTokenIds=[Y, N]`. Flag `DRY_RUN_NEG_RISK_RESOLUTION_ENABLED=true`.
    - Action : `_run_once()`.
    - Assertion : position fermée avec `realized_pnl = (1.0 - 0.25) × 10 = 7.5`. Alert `dry_run_market_resolved_neg_risk` émise.

17. **`test_resolution_neg_risk_no_wins`** — losing candidate.
    - Preconditions : 1 virtual pos (cond=0xN2, asset=Y, size=10, avg=0.25). Gamma `neg_risk=true, closed=true, outcomePrices=["0.0","1.0"], clobTokenIds=[Y, N]`.
    - Action : `_run_once()`.
    - Assertion : `realized_pnl = (0.0 - 0.25) × 10 = -2.5` (perte totale).

18. **`test_resolution_neg_risk_skipped_when_flag_off`** — opt-out.
    - Preconditions : idem test 16 mais `DRY_RUN_NEG_RISK_RESOLUTION_ENABLED=false`.
    - Action : `_run_once()`.
    - Assertion : position reste ouverte (`closed_at IS NULL`). Log `dry_run_resolution_neg_risk_skipped_by_flag`.

19. **`test_resolution_neg_risk_prices_not_converged_skipped`** — garde défensif.
    - Preconditions : Gamma `neg_risk=true, closed=true, outcomePrices=["0.95","0.05"]` (pas encore 1.0).
    - Action : `_run_once()`.
    - Assertion : position reste ouverte. Log `dry_run_resolution_winning_outcome_unknown`. Le watcher retentera au prochain cycle quand Gamma aura converged.

**Total : 19 tests** couvrant les 5 sujets. Tests ciblés entre commits, full `pytest` à la fin.

---

## 10. Impact sur l'existant

### 10.1 Modules touchés

| Module | Changement | Backwards compat |
|---|---|---|
| [src/polycopy/strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py) | `PositionSizer.check()` refactor side-aware + 1 nouveau reason code | Les tests existants (`test_position_sizer_position_already_open`, `test_position_sizer_pass_with_cap`, `test_position_sizer_pass_no_cap`, `test_full_pipeline_*`) testent tous des BUYs → passent sans modification. |
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | `HomeAllTimeStats` +4 champs avec defaults + `get_home_alltime_stats` +1 param optionnel | Les tests qui instancient `HomeAllTimeStats(...)` ne spécifient pas les nouveaux champs → defaults appliqués. Les callers de `get_home_alltime_stats(sf, pnl_mode=...)` continuent sans le paramètre `settings` → PnL latent à 0 (même valeur qu'aujourd'hui vu que personne ne le lit encore). |
| [src/polycopy/dashboard/templates/home.html](../../src/polycopy/dashboard/templates/home.html) | +4 cartes KPI (2 ligne 1, 2 ligne 2) | Aucun test casse — les tests de template font grep sur des champs spécifiques, pas sur le nombre de cartes. Ajouter 1-2 asserts sur les nouvelles cartes. |
| [src/polycopy/monitoring/daily_summary_queries.py](../../src/polycopy/monitoring/daily_summary_queries.py) | `_orders_stats_since` +1 param obligatoire `settings` | Breaking si un test mock appelle `_orders_stats_since` directement avec les 2 anciens args. Chercher les callers dans les tests et les mettre à jour (probablement 1-2 tests max). |
| [src/polycopy/executor/dry_run_resolution_watcher.py](../../src/polycopy/executor/dry_run_resolution_watcher.py) | Supprime skip neg_risk + émet alerte + `__init__` +1 param optionnel | `alerts_queue=None` par défaut → compat stricte. Les 2 tests existants [test_dry_run_resolution_watcher.py](../../tests/unit/test_dry_run_resolution_watcher.py) ne passent pas `alerts_queue` → passent inchangés. |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +2 settings optionnelles | Defaults non-cassants (flag=True active la nouvelle logique ; capital=None ne casse rien). |
| [src/polycopy/monitoring/dtos.py](../../src/polycopy/monitoring/dtos.py) | `Alert.event` Literal étendu | Pydantic Literal : ajouter `"dry_run_market_resolved_neg_risk"` à l'union existante. |
| [src/polycopy/monitoring/templates/](../../src/polycopy/monitoring/templates/) | +1 fichier template | Nouveau fichier, rien ne casse. |
| [src/polycopy/executor/orchestrator.py](../../src/polycopy/executor/orchestrator.py) | Passe l'alerts_queue au `DryRunResolutionWatcher` | Modif interne, aucun test public touché. |

### 10.2 Changements de valeurs par défaut

- `DRY_RUN_NEG_RISK_RESOLUTION_ENABLED=true` par défaut → **change le comportement** pour les utilisateurs qui tournent en dry-run. Acceptable parce que M8 v1 était déjà documenté comme "cas neg_risk non supporté, à résoudre v2". Aucun breaking — les positions s'ouvrent identiquement, seule la fermeture auto devient active.
- `DRY_RUN_INITIAL_CAPITAL_USD=None` par défaut → pas de breaking, fallback sur snapshots.

### 10.3 Tests existants potentiellement impactés

À vérifier lors de l'implémentation :
- [tests/unit/test_daily_summary_queries.py](../../tests/unit/test_daily_summary_queries.py) : tous les appels à `_orders_stats_since` doivent passer `settings`.
- [tests/unit/test_home.py](../../tests/unit/test_home.py) et variantes : les asserts sur `HomeAllTimeStats(...)` reçoivent les defaults.
- [tests/integration/test_dashboard_e2e.py](../../tests/integration/test_dashboard_e2e.py) (si existe) : doit continuer à passer.

---

## 11. Migration / Backwards compat

### 11.1 Aucune migration Alembic

M13 n'ajoute **aucun schéma DB**. Tous les nouveaux champs sont calculés à la volée dans les queries. La table `my_positions` a déjà `realized_pnl` (M8 v1), `simulated` (M8 v1), `closed_at` (M3). Les tables `my_orders`, `pnl_snapshots`, `trader_events` sont inchangées.

**Confirmer avant commit** : `alembic revision --autogenerate` doit produire un fichier vide ou crasher explicitement (no-op).

### 11.2 Rollback path

Si une régression neg_risk apparaît en production :
- `DRY_RUN_NEG_RISK_RESOLUTION_ENABLED=false` dans `.env` → revert au comportement M8 v1 (skip + warning).
- Restart bot suffit (setting lue au boot par Pydantic).

Si Bug 5 cause un side-effect imprévu (très peu probable, logique isolée) :
- Pas de flag opt-out prévu — l'ancien comportement était buggé, retourner en arrière n'est pas souhaitable. Si absolu, revert le commit (`git revert <sha>`).

### 11.3 Gestion des 512 positions orphelines existantes

Sur `uni-debian`, au moment du deploy M13 :
- 512 positions virtuelles ouvertes, dont probablement 400+ sur des marchés déjà résolus avant M8 v2 (notamment les grands events neg_risk passés).
- Sans intervention, ces positions ne se fermeront **que si** :
  - Un SELL copié arrive (Bug 5 fix) sur leur (cond, asset) — improbable si source wallets ont déjà bougé.
  - Le `DryRunResolutionWatcher` les voit à son prochain cycle (30 min par défaut) et les marchés sont toujours visibles sur Gamma avec `closed=true + outcomePrices convergées`. Probable pour la plupart.

**Backfill script optionnel** (`scripts/backfill_resolved_positions_m13.py`) :
- Liste les 512 positions ouvertes.
- Batch query Gamma pour leurs condition_ids.
- Pour chaque marché `closed=true` avec prices convergées → calcule `realized_pnl` et ferme la position via `close_virtual`.
- Log le résumé : "N positions backfilled, X+$ realized, Y positions still open (marchés non encore résolus)".
- **Non implémenté v1** — documenté ici comme référence si l'utilisateur veut le développer lui-même. Alternative naturelle : laisser le `DryRunResolutionWatcher` M8 v2 faire le travail sur les cycles suivants (30 min × quelques cycles = quelques heures pour tout ratisser).

**Recommandation pour ton deploy M13** : laisse tourner le watcher M8 v2 pendant 2-3 cycles (environ 1h30) après le deploy. Tu verras les positions orphelines se fermer en masse, avec les PnL correspondants qui remontent sur /home. Si au bout de quelques heures il reste des positions stale, c'est soit que les marchés ne sont pas résolus sur Gamma, soit qu'il y a un mismatch (rare).

---

## 12. Commandes de vérification finale

Bloc copiable-collable pour l'implémenteur M13 :

```bash
# 1. Environnement déjà OK (bash scripts/setup.sh idempotent).
cd /home/nexium/code/polycopy
source .venv/bin/activate

# 2. Lint + type-check (après chaque commit).
ruff check .
ruff format . --check
mypy src

# 3. Tests ciblés par bug (entre commits, ~30 sec chacun).
pytest tests/unit/test_strategy_pipeline.py -x --tb=short          # Bug 5
pytest tests/unit/test_dashboard_queries.py -x --tb=short -k "home_alltime"  # Bug 6 + PnL latent
pytest tests/unit/test_daily_summary_queries.py -x --tb=short      # Bug 7
pytest tests/unit/test_dry_run_resolution_watcher.py -x --tb=short # M8 v2

# 4. Full suite (à la fin uniquement — ~3 min).
pytest

# 5. Test runtime dry-run (boot + dashboard).
python -m polycopy --dry-run &
BOT_PID=$!
sleep 30
curl -s http://127.0.0.1:8787/home | grep -E "Exposition|Gain max|PnL latent|Win rate"
kill $BOT_PID

# 6. Pas de migration Alembic (vérifier).
alembic revision --autogenerate -m "m13_check" --sql | head -5
# Devrait être vide ou "no changes detected".

# 7. Grep sécurité.
grep -r "POLYMARKET_PRIVATE_KEY\|TELEGRAM_BOT_TOKEN" src/polycopy/dashboard/templates/home.html
# Doit être vide (0 match).
```

Après `git push` sur `main`, côté `uni-debian` :

```bash
ssh uni-debian
cd ~/Documents/GitHub/polycopy
git pull
# Bot auto-restart si systemd unit en place, sinon :
# pkill -f "polycopy" && python -m polycopy --dry-run &

# Vérifier dans les 30 min qui suivent :
# - /home affiche 4 nouvelles cartes
# - /activity commence à se peupler (positions neg_risk résolues)
# - /performance leaderboard a des winrates non-nuls
# - Recap Telegram quotidien (20h local) montre volume non-zéro
```

---

## 13. Hors scope M13 (à ne pas implémenter)

- ❌ Rétro-résolution automatique des 512 positions orphelines au boot (`scripts/backfill_resolved_positions_m13.py`). Documenté §11.3 comme optionnel, implémentation reportée si user demande.
- ❌ Nouveau mode `execution_mode`.
- ❌ Modification des autres 5 filtres strategy (seul `PositionSizer` touché).
- ❌ Refactor `PnlSnapshotWriter` pour écrire les vrais `realized_pnl`/`unrealized_pnl` en DB (M13 les calcule à la lecture côté queries). Reporté à M14+ si le besoin émerge.
- ❌ Support "conversion précoce neg_risk" via `negRiskAdapter` contract.
- ❌ Frais CLOB (pas dans le scope M13, feature à part pour M14+).
- ❌ Goldsky streaming pour les résolutions (Gamma polling 30 min suffit pour v1).
- ❌ Dashboard widget dédié "historique résolutions neg_risk" (onglet séparé). Les résolutions apparaissent déjà dans /activity et les alertes Telegram.

---

## 14. Notes d'implémentation & zones d'incertitude

### 14.1 Schéma Gamma neg_risk — validation finale à l'implémentation

La fixture [tests/fixtures/gamma_neg_risk_resolved_sample.json](../../tests/fixtures/gamma_neg_risk_resolved_sample.json) est **synthétique** (dérivée du sample public `gamma_market_sample.json` en flipant `closed=true` + `outcomePrices=["1.0","0.0"]` ou `["0.0","1.0"]`). À l'implémentation, l'implémenteur devrait **capturer une fixture réelle** d'un marché neg_risk résolu via :

```bash
curl -s "https://gamma-api.polymarket.com/markets?condition_ids=<cond_of_resolved_neg_risk>&closed=true" \
  > tests/fixtures/gamma_neg_risk_resolved_sample.json
```

À demander au skill Polymarket ou via exploration des archives Gamma. Si le schéma diffère substantiellement de la fixture synthétique (champs manquants, typage différent de `outcome_prices`), corriger les tests 15-19 en conséquence.

### 14.2 Formule PnL exact cas "invalid resolution"

Polymarket a historiquement eu des marchés résolus "invalid" où UMA (oracle) n'a pas pu trancher. Dans ce cas, la convention est le refund : `outcome_prices=["0.5","0.5"]` approximativement, et les tokens sont rachetés au prix mid.

**M13 v1 scope** : on détecte `max(prices) < 0.99` → skip (log `dry_run_resolution_winning_outcome_unknown` déjà en place M8 v1). La position reste ouverte. Le cas est assez rare (< 1% des marchés Polymarket) et la gestion "refund 50/50" est cross-cutting (affecte binaire et neg_risk identiquement). **Reporté à M14+** si le besoin émerge.

### 14.3 Race condition — SELL copié vs resolver en parallèle

Scénario : à 03h00 UTC, le watcher M8 v2 lance un cycle de résolution. Pendant ce temps, le bot copie un SELL sur une position encore ouverte mais en cours de résolution par le watcher.

Séquence problématique possible :
1. Watcher lit `existing = MyPosition(id=42, closed_at=None)`.
2. Executor copy-SELL : `upsert_virtual(SELL)` décrémente size → `closed_at = now` → commit.
3. Watcher calcule `realized_pnl` et appelle `close_virtual(id=42, ...)` → trouve la position déjà fermée (`closed_at != None`) → **sur-écrase le `closed_at` et le `realized_pnl`**.

**Conséquence** : double comptabilisation ou écrasement du PnL SELL par le PnL résolution.

**Mitigation proposée v1** : ajouter un garde-fou dans `close_virtual` ([repositories.py:727](../../src/polycopy/storage/repositories.py#L727)) :

```python
async def close_virtual(self, position_id, *, closed_at, realized_pnl):
    async with self._session_factory() as session:
        position = await session.get(MyPosition, position_id)
        if position is None:
            raise ValueError(f"MyPosition id={position_id} not found")
        if not position.simulated:
            raise ValueError(...)
        # NOUVEAU M13 : idempotence — ne pas ré-écraser une position déjà fermée.
        if position.closed_at is not None:
            log.info(
                "close_virtual_skipped_already_closed",
                position_id=position_id,
                existing_closed_at=position.closed_at,
                existing_realized_pnl=position.realized_pnl,
            )
            return
        position.closed_at = closed_at
        position.realized_pnl = realized_pnl
        await session.commit()
```

**Alternative plus stricte** : SELECT FOR UPDATE au moment de la lecture dans le watcher, mais SQLite ne supporte pas — trop de plomberie pour un cas edge qui arrive peut-être 1 fois par mois. La mitigation idempotente ci-dessus est suffisante.

**Documenter dans le PR M13** : ordre de précédence = SELL copié prime sur résolution watcher (parce qu'il arrive en premier chronologiquement). Le watcher skip si déjà fermée. C'est sain parce que le PnL SELL capture la valeur réelle du trade copié, tandis que la résolution watcher approxime via le payout final.

### 14.4 Cache TTL Gamma

Le watcher poll toutes les 30 min (`dry_run_resolution_poll_minutes`). À chaque cycle, un batch query `/markets?condition_ids=...` peut contenir 100+ condition_ids (512 positions en prod). Pas de cache TTL dédié pour ces requêtes aujourd'hui.

**v1 acceptable** — 1 requête Gamma batch toutes les 30 min = ~48 requêtes/jour. Largement sous le rate limit Gamma (~100 req/min). Pas d'optimisation nécessaire.

**Si scale futur** (> 1000 positions ouvertes simultanées) : batcher par 100 condition_ids et paginer. Pas scope M13.

### 14.5 Ordre de précédence des commits

L'ordre recommandé (§15 prompt d'implémentation) est :
1. Bug 5 (débloque tout, critique).
2. Bug 7 (petit, indépendant, peut shipper en parallèle).
3. M8 v2 (moyenne taille, dépend de la fixture + settings config).
4. Bug 6 + PnL latent (plus gros, dernier à valider avec screenshots).

Chaque commit ≤ 3 fichiers modifiés + 1 fichier de tests. Push immédiat après chaque commit pour pull côté `uni-debian`.

---

## 15. Prompt d'implémentation

Bloc à coller tel quel dans une nouvelle conversation Claude Code à l'implémentation M13.

````markdown
# Contexte

polycopy dashboard affiche toujours des KPI incomplets après 4 fix précédents (bugs 1-4 M13). Diagnostic complet dans [docs/specs/M13_dry_run_observability_spec.md](docs/specs/M13_dry_run_observability_spec.md). 5 sujets bundle : Bug 5 (PositionSizer side-aware, critique, débloque le cycle BUY→SELL), Bug 6 (3 cartes KPI /home), Bug 7 (Telegram recap volume), PnL latent (4ᵉ carte /home), M8 v2 (résolution neg_risk). Test 14 jours en cours sur `uni-debian` bloqué par l'absence de PnL réalisé — M13 débloque l'observabilité complète.

# Prérequis

- Lire `docs/specs/M13_dry_run_observability_spec.md` **en entier** (spécifiquement §5 algorithmes + §9 test plan).
- Lire [CLAUDE.md](CLAUDE.md) section "Conventions de code" et "Sécurité".
- La fixture [tests/fixtures/gamma_neg_risk_resolved_sample.json](tests/fixtures/gamma_neg_risk_resolved_sample.json) est synthétique — si possible, invoquer skill polymarket pour capturer une fixture réelle ; sinon conserver la synthétique.
- Ne PAS modifier les modules hors scope (§2.2 spec) : `SlippageChecker`, `RiskManager`, `TraderLifecycleFilter`, `EntryPriceFilter`, `MarketFilter`, orchestrators autres que `ExecutorOrchestrator`.

# Ordre de commits recommandé

1. `fix(strategy): make PositionSizer side-aware so SELLs can close open positions` (Bug 5, §5.1, 6 tests §9.1)
2. `fix(monitoring): count SIMULATED orders in Telegram daily recap when dry-run` (Bug 7, §5.3, 2 tests §9.3)
3. `feat(executor): support neg_risk market resolution in DryRunResolutionWatcher` (M8 v2, §5.5, 5 tests §9.5)
4. `feat(dashboard): add Exposition, Gain max, PnL latent and Win rate cards on /home` (Bug 6 + PnL latent, §5.2 + §5.4, 6 tests §9.2 + §9.4)

**Push sur main après chaque commit.** Je pull immédiatement sur debian.

# Validation avant commit 4

Commit 4 touche le template HTML — **montre-moi le diff de [home.html](src/polycopy/dashboard/templates/home.html) avant commit** pour valider le rendu des 4 nouvelles cartes (ordre, icônes, grid layout). Les commits 1-3 peuvent partir sans validation supplémentaire.

# Tests + quality gates

- Tests ciblés entre commits (cf. memory `feedback_test_scope`).
- Full `pytest` + `ruff check .` + `ruff format .` + `mypy src` à la fin.
- Les 2 tests flaky de `test_watcher_live_reload.py` (pré-existants) passent en isolation → OK s'ils échouent dans la full suite, noté dans une session précédente.

# Git workflow

- Tout sur `main`, push après chaque commit.
- Update CLAUDE.md §Conventions si un nouveau reason code ou event type change les invariants de sécurité.

# Plan à confirmer

Commence par me confirmer ton plan en 1 message bref (1 phrase par commit), puis enchaîne les 4 commits en suivant l'ordre ci-dessus. Tests verts avant chaque push.
````

---

## 16. Commit message proposé

```
docs(specs): add M13 — dry-run observability & neg_risk resolution

Spec bundle de 5 sujets pour débloquer le test 14 jours en cours :

- Bug 5 (critique) : PositionSizer side-aware — les SELL copiés peuvent
  enfin fermer les positions virtuelles (aujourd'hui bloqués par
  position_already_open indistinct BUY/SELL).
- Bug 6 (UX) : 3 nouvelles cartes KPI /home (Exposition, Gain max
  latent, Win rate global).
- Bug 7 (UX) : recap Telegram daily volume mode-aware (même pattern
  que Bug 2 côté dashboard, oublié côté monitoring).
- Feature PnL latent : 4ᵉ carte /home clarifiant la distinction
  cristallisé vs mark-to-market.
- Feature M8 v2 : résolution neg_risk activée (suppression du skip
  M8 v1 — les marchés neg_risk sont structurellement binaires, la
  logique de résolution existante s'applique sans modification).

Fixture synthétique gamma_neg_risk_resolved_sample.json fournie
(à remplacer par une capture réelle à l'implémentation si possible).

Implémentation en 4 commits séparés (cf. spec §15).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```
