# M8 — Dry-run réaliste (orderbook-based fill simulation + PnL virtuel)

Spec d'implémentation du **mode dry-run semi-réel**. Aujourd'hui (M3→M7), `DRY_RUN=true` log l'ordre approuvé (`MyOrder.status='SIMULATED'`, `simulated=True`) puis s'arrête net : aucun fill virtuel, aucune position virtuelle, le PnL reste un stub (`RISK_AVAILABLE_CAPITAL_USD_STUB=1000`). Un user qui veut "observer 2-3 jours ce que le bot aurait gagné ou perdu" n'a pas de signal tangible.

M8 répond : un dry-run qui **simule un fill FOK réaliste** à partir de la profondeur orderbook CLOB (read-only, pas de signature), **persiste la position virtuelle**, **valorise live** en mid-price, et **résout** le PnL quand le marché se résout. Le tout avec **zéro risque capital** : `DRY_RUN=false` reste l'unique trigger d'un vrai POST CLOB (triple-garde-fou M3).

Source de vérité fonctionnelle : `docs/architecture.md` §Module Executor + §Module Monitoring. Conventions : `CLAUDE.md`. Code existant : `src/polycopy/executor/pipeline.py` (chemin simulé déjà isolé), `src/polycopy/executor/wallet_state_reader.py`, `src/polycopy/monitoring/pnl_writer.py`. Spec de référence : `specs/M3-executor.md` + `specs/M4-monitoring.md`.

---

## 0. Pré-requis environnement

### 0.1 Bootstrap (déjà fait)

`bash scripts/setup.sh` (idempotent). M8 n'introduit **aucune dépendance Python nouvelle**. `httpx` déjà en place pour les reads CLOB, `SQLAlchemy 2.0` déjà pour la persistance, `jinja2` et autres n'importent pas. Pas de patch config structurel.

### 0.2 Skill Polymarket — endpoint `/book` à capturer

Action obligatoire avant code : capturer 1 fixture réelle de `GET https://clob.polymarket.com/book?token_id=<token_id>` pour un marché liquide (ex: un outcome du top market Gamma au moment de la capture). Cet endpoint est **read-only, non-auth** et documenté dans le skill `/polymarket:polymarket`. À capturer dans `tests/fixtures/clob_orderbook_sample.json`.

Schéma anticipé (confirmer à la capture) :

```json
{
  "market": "0x...conditionId...",
  "asset_id": "73938...",
  "timestamp": "1713452400",
  "hash": "0x...",
  "bids": [
    {"price": "0.08", "size": "150.00"},
    {"price": "0.07", "size": "320.00"}
  ],
  "asks": [
    {"price": "0.09", "size": "200.00"},
    {"price": "0.10", "size": "180.00"}
  ]
}
```

**Piège anticipé** : `price` et `size` sont des strings (précision arbitraire) — parser via `Decimal` pas `float` directement.

### 0.3 `.env` — nouvelles variables (toutes OPTIONNELLES)

| Variable env | Champ Settings | Default | Description |
|---|---|---|---|
| `DRY_RUN_REALISTIC_FILL` | `dry_run_realistic_fill` | `false` | Opt-in strict (§2.5). Si `false`, comportement M3 préservé (fill stub instantané). Si `true` + `DRY_RUN=true` → M8 simule via orderbook. Si `DRY_RUN=false` → ignoré (pas pertinent en mode live). |
| `DRY_RUN_VIRTUAL_CAPITAL_USD` | `dry_run_virtual_capital_usd` | `1000.0` | Capital initial virtuel pour le PnL dry-run. Remplace `RISK_AVAILABLE_CAPITAL_USD_STUB` uniquement dans les snapshots `is_dry_run=true`. `Field(ge=10.0, le=1_000_000.0)`. |
| `DRY_RUN_BOOK_CACHE_TTL_SECONDS` | `dry_run_book_cache_ttl_seconds` | `5` | Cache in-memory du `/book` par `asset_id` (§2.6 anti-thundering-herd). `Field(ge=1, le=60)`. |
| `DRY_RUN_RESOLUTION_POLL_MINUTES` | `dry_run_resolution_poll_minutes` | `30` | Cadence de vérification "marché résolu ?" via Gamma `endDate + closed`. Résout les positions virtuelles et matérialise le PnL. `Field(ge=5, le=1440)`. |
| `DRY_RUN_ALLOW_PARTIAL_BOOK` | `dry_run_allow_partial_book` | `false` | Si `true`, autorise un fill partiel si book insuffisant ; sinon REJECT (FOK strict). Default `false` = cohérent avec l'executor M3 live (FOK taker). |

À ajouter à `config.py` ET `.env.example` avec commentaires explicites "uniquement actif si `DRY_RUN=true`". Aucune n'est requise — backwards compat totale (user M7 → M8 main branch sans diff `.env` → comportement M3/M4/M7 identique).

### 0.4 Interdépendance avec les autres specs post-M5

- **M6 (dashboard 2026)** : la page `/pnl` livre à M6 un layout avec toggle réservé "réel / dry-run virtuel" non actif. M8 branche ce toggle en ajoutant un filtre `?mode=real|dry_run|both` côté `queries.get_pnl_chart_data`. **Interdépendance forte** : M8 **nécessite** le layout M6 (branchement sur un layout M4.5 Pico serait moche). Recommandation : **M6 avant M8**.
- **M7 (Telegram enrichi)** : le daily summary M7 inclut le `total_usdc` virtuel si `is_dry_run=true`. Hook conditionnel déjà prévu dans `DailySummaryContext` M7 (`total_usdc: float | None`). M8 implémente la valorisation qui alimente ce champ. Indépendance souple : M8 peut shipper après M7 sans rewrite templates.
- **M9 (CLI silencieux + README)** : la FAQ README M9 documente "comment interpréter un PnL dry-run ?". M8 doit shipper **avant** la capture d'écran M9. Recommandation : M8 avant M9.

**Ordre d'implémentation recommandé global** (brief user) : **M9 → M6 → M8 → M7**. M8 arrive 3ᵉ — le layout PnL M6 est en place, les captures M9 finales peuvent inclure un exemple dry-run virtuel.

### 0.5 Critère de validation "environnement"

```bash
DRY_RUN=true DRY_RUN_REALISTIC_FILL=true \
DRY_RUN_VIRTUAL_CAPITAL_USD=1000 \
python -m polycopy --dry-run
```

Doit logger :

- `executor_started` (inchangé M3).
- `dry_run_realistic_fill_enabled` (nouveau, warning-level au boot pour rappeler le mode).
- Quand un trade est approuvé → `order_realistic_fill_simulated` avec `asset_id`, `requested_size`, `avg_fill_price`, `depth_consumed_shares`, `depth_consumed_levels`.
- Ou `order_realistic_fill_rejected` avec `reason='insufficient_liquidity'` si book insuffisant.
- Toutes les `DRY_RUN_RESOLUTION_POLL_MINUTES` → `dry_run_resolution_cycle_started` + `dry_run_position_resolved` pour chaque marché résolu.
- Snapshots PnL dry-run avec `total_usdc = DRY_RUN_VIRTUAL_CAPITAL_USD + valeur_des_positions_virtuelles`.

Exit 0 sur SIGINT. Tests M3 passent inchangés (mode "stub" continue d'être le fallback si `DRY_RUN_REALISTIC_FILL=false`).

### 0.6 Sécurité — rappels stricts pour M8

**M8 ÉLARGIT la surface d'exécution virtuelle — il ne doit EN AUCUN CAS relâcher les garde-fous M3 de signature CLOB** :

- **Triple garde-fou M3 préservé intact** :
  1. Lazy init `ClobWriteClient` : **jamais instancié si `dry_run=true`** (M3 §2.2). M8 ne touche pas à cette invariante — le mode "realistic fill" utilise uniquement `ClobMetadataClient` (read-only + nouveau client `ClobOrderbookReader`, read-only).
  2. `RuntimeError` au démarrage si `DRY_RUN=false` ET clés absentes (M3 `ExecutorOrchestrator.__init__`). Inchangé.
  3. `assert dry_run is False` juste avant chaque `create_and_post_order` (M3 pipeline). Inchangé.
- **Nouveau quatrième garde-fou M8** : `assert dry_run is True` juste avant chaque `simulate_realistic_fill`. Un bug de refactor qui appellerait la simulation en live mode → raise explicite + log `executor_invariant_breach` CRITICAL + kill switch. Cohérent avec la logique de defense in depth M3/M4.
- **Aucune creds consommée par le chemin "realistic fill"** : pas de L1, pas de L2, pas de `py-clob-client`. Uniquement httpx GET sur endpoints publics (`/book`, `/midpoint`, Gamma `/markets`).
- **Aucune persistance de `POLYMARKET_PRIVATE_KEY` ni `POLYMARKET_FUNDER`** dans les logs ni les messages Telegram M8 (inchangé M3/M4/M7).
- **Endpoint `/book` = read-only** documenté par Polymarket. Aucun POST, aucun PATCH.
- **Virtual PnL n'affecte PAS le RiskManager M2** : le RiskManager lit `settings.risk_available_capital_usd_stub` (M2→M3). M8 n'override pas — les decisions "approved" restent identiques peu importe que le capital virtuel monte ou descende. **Rationale** : on ne veut pas que le dry-run prenne des décisions différentes du live pour une même configuration. Le virtual capital sert **uniquement** à la valorisation PnL, pas à l'approbation.
- **Kill switch M4 virtuel** : si le PnL virtuel draws down ≥ `KILL_SWITCH_DRAWDOWN_PCT` → M4 `PnlSnapshotWriter` déclenche le kill switch… **NON**. Rappel M4 §Sécurité : "kill switch **jamais en dry-run**". **M8 respecte cette invariante** : les snapshots avec `is_dry_run=true` ne déclenchent jamais `stop_event.set()`. Le user peut avoir un drawdown virtuel de -50 % sans que le bot s'arrête — c'est l'intention (observer librement).
- **Alerte Telegram virtuel drawdown** : OK de **notifier** le user ("drawdown virtuel -22 %") mais **sans** kill switch. Nouvelle alerte `dry_run_virtual_drawdown` level `INFO` (pas WARNING, pas CRITICAL) — le mot "virtuel" dans le message.

---

## 1. Objectif M8 (scope exact)

Faire passer le dry-run de "log + stop" à "observabilité complète du PnL qu'on aurait eu", **sans toucher au code live** (M3 identique bit-for-bit côté path `dry_run=false`).

Livrable fonctionnel :

- **Orderbook-based fill simulation** : à chaque `OrderApproved` en `dry_run=true` + `realistic_fill=true`, fetch `/book?token_id=<asset_id>`, calcule le prix moyen pondéré sur la profondeur ask (BUY) ou bid (SELL), persiste dans `my_orders` avec `simulated=True`, `realistic_fill=True` (nouvelle colonne), `status='SIMULATED'`, `price=<avg_fill_price>`, `size=<requested_size>` (ou `REJECTED` si book insuffisant).
- **Position virtuelle tracking** : un fill simulé M8 crée/update une ligne `my_positions` (réutilise la table existante) avec `simulated=True` (nouvelle colonne) — **ségrégée** des positions réelles.
- **Market resolution watcher** : une nouvelle boucle périodique (`DryRunResolutionWatcher`) query Gamma toutes les `DRY_RUN_RESOLUTION_POLL_MINUTES` pour détecter les marchés `closed=true`. Quand une position virtuelle est sur un marché résolu, calcule le PnL réalisé (`size * (winning_outcome ? 1.0 : 0.0) - size * avg_entry_price`), update `my_positions.closed_at`, persiste un `PnlSnapshot` `is_dry_run=true` avec la réalisation.
- **Live valuation** : un `WalletStateReader` virtuel (extension du M3 existant) somme `size × current_mid_price` pour toutes les positions virtuelles ouvertes. Le `PnlSnapshotWriter` M4 utilise ce reader quand `dry_run=true` → `total_usdc` virtuel vivant, `realized_pnl` + `unrealized_pnl` distincts.
- **Dashboard intégration (M6)** : le toggle `?mode=real|dry_run|both` filtre sur `is_dry_run`, affiche courbes séparées. Timeline milestones inclut les résolutions de marchés virtuels.
- **Rapport HTML enrichi** : `scripts/pnl_report.py --dry-run-mode --output html` isole les snapshots `is_dry_run=true` et produit un rapport dédié avec : équity curve virtuelle, drawdown max, sharpe approximé, heatmap par marché, win rate.

**Hors livrable M8** :

- **Fills partiels par défaut** : FOK strict cohérent avec le mode live (si book insuffisant → REJECT). Opt-in via `DRY_RUN_ALLOW_PARTIAL_BOOK=true`.
- **Slippage simulé non déterministe** : M8 utilise le book snapshotté à l'instant T (read at simulate time). Pas de simulation Monte Carlo du slippage futur.
- **Order types autres que FOK** : GTC / GTD / FAK pas simulés. M3 n'émet que FOK taker, cohérent.
- **Maker rebates simulés** : M3 ne fait pas de maker → M8 non plus.
- **Kill switch virtuel** : jamais. Cf. §0.6.
- **Recalcul rétroactif** du PnL virtuel si `DRY_RUN_VIRTUAL_CAPITAL_USD` change. La valeur est lue au start, pas à chaque snapshot.
- **Backtesting historique**. M5 a `scripts/score_backtest.py` pour scoring ; M8 est live-forward only (on simule au fil de l'eau, pas en rejouant).
- **Multi-asset simulation cross-margining** : chaque position virtuelle est indépendante.
- **Refactor M3 executor path live**. M8 ajoute un chemin **parallèle** au path `SIMULATED` existant, ne touche pas au live path.

---

## 2. Arbitrages techniques (8 points à trancher explicitement)

### 2.1 Source de profondeur — `/book` vs `/midpoint`

**Recommandation : `/book` exclusivement.**

- `/midpoint?token_id=<id>` (M2 SlippageChecker) retourne juste `{"mid": "0.08"}` → sans profondeur, impossible de simuler un fill FOK réaliste (on ne sait pas combien de shares sont dispos au mid).
- `/book?token_id=<id>` retourne 2 arrays `bids[] asks[]` triés par prix. Exactement ce qu'il faut pour consommer level-by-level jusqu'à atteindre `requested_size`.
- Pas documenté officiellement dans le skill à ce jour (confirmer à la capture fixture). Cohérent avec le pattern CLOB public (GET = read-only, pas de signature).

**Alternatives écartées** :

- **Construire le book depuis `/midpoint` + hypothèse spread fixe** : approximation grossière, sous-estime le slippage sur marchés illiquides.
- **WebSocket CLOB channel `market`** : streaming, nécessiterait d'abonner aux tokens_id à la volée, infrastructure nouvelle. Reportable M8.1 si la latence READ `/book` devient problématique.
- **Goldsky subgraph** : `positions-subgraph` n'a pas d'orderbook snapshot. Non applicable.

### 2.2 Algorithme de fill — prix moyen pondéré level-by-level

**Recommandation : walk sur le side pertinent (asks pour BUY, bids pour SELL), consomme jusqu'à `requested_size`, calcule `avg_price = sum(level.price × shares_consumed) / total_shares`. FOK strict : si book insuffisant → REJECT.**

Pseudocode :

```python
def simulate_fill(
    order: OrderApproved,
    book: Orderbook,
    *,
    allow_partial: bool,
) -> RealisticFillResult:
    """Simule un fill FOK taker à partir de l'orderbook.

    - BUY → consomme les asks (prix croissant).
    - SELL → consomme les bids (prix décroissant).
    - FOK : si profondeur insuffisante ET pas allow_partial → REJECT.
    """
    if order.side == "BUY":
        levels = sorted(book.asks, key=lambda lvl: lvl.price)
    else:
        levels = sorted(book.bids, key=lambda lvl: -lvl.price)

    remaining = Decimal(str(order.size))
    consumed_usd = Decimal("0")
    consumed_shares = Decimal("0")
    levels_touched = 0

    for lvl in levels:
        if remaining <= 0:
            break
        fill_size = min(remaining, lvl.size)
        consumed_usd += fill_size * lvl.price
        consumed_shares += fill_size
        remaining -= fill_size
        levels_touched += 1

    if remaining > 0:
        if not allow_partial:
            return RealisticFillResult(
                status="REJECTED",
                reason="insufficient_liquidity",
                requested_size=order.size,
                filled_size=0.0,
                avg_fill_price=None,
                depth_consumed_shares=float(consumed_shares),
                depth_consumed_levels=levels_touched,
                shortfall=float(remaining),
            )
        # fill partiel autorisé : on accepte ce qu'on a
    avg_price = float(consumed_usd / consumed_shares) if consumed_shares > 0 else None
    return RealisticFillResult(
        status="SIMULATED",
        reason=None,
        requested_size=order.size,
        filled_size=float(consumed_shares),
        avg_fill_price=avg_price,
        depth_consumed_shares=float(consumed_shares),
        depth_consumed_levels=levels_touched,
        shortfall=float(remaining),
    )
```

**Décimales** : utiliser `Decimal` pour la somme pondérée afin d'éviter les erreurs de float accumulées sur des books à 10 levels. Convertir en `float` uniquement pour la persistance DB + logs.

**Alternatives écartées** :

- **Fill au mid-price** : irréaliste (on ignore le spread, donc on sous-estime le coût). Conservé pour le mode "stub" = M3 default `DRY_RUN_REALISTIC_FILL=false`.
- **Fill au top-of-book** : ignore la profondeur, sous-estime le slippage sur grosses tailles.
- **Fill partiel par défaut** : s'écarte du comportement live (M3 envoie FOK). Opt-in via `DRY_RUN_ALLOW_PARTIAL_BOOK=true`.

### 2.3 Ségrégation positions virtuelles vs réelles

**Recommandation : nouvelle colonne `simulated: bool` sur `my_orders` (existant) + sur `my_positions` (nouveau), pas de nouvelle table.**

Pros :

- Réutilise les repositories, queries dashboard, Alembic stack existants.
- Requêtes filtrées via `WHERE simulated=... ` — trivial à indexer.
- Le `/pnl` dashboard peut montrer les deux en parallèle avec `?mode=real|dry_run|both`.
- Pas de code duplication (pipeline, persist, position upsert).

Cons :

- Un bug de query qui oublie le filter `simulated` peut mélanger virtuel et réel. Mitigation : helper `session.filter_real()` / `session.filter_simulated()` injecté en début de chaque query lieu critique + test `test_queries_segregation.py`.

**Alternatives écartées** :

- **Tables séparées `my_virtual_orders` + `my_virtual_positions`** : duplication models + repos + migrations. Rejeté.
- **`MyOrder.status = 'SIMULATED_REALISTIC'`** : extension de l'enum `status`. Plus compact mais perd l'info "simulated" vs "realistic_simulated" vs "real_filled". Moins clair.
- **Schema-level séparation (2 DBs)** : over-engineering.

### 2.4 Résolution de marché — Gamma polling `closed=true`

**Recommandation : `DryRunResolutionWatcher` périodique (`DRY_RUN_RESOLUTION_POLL_MINUTES=30`), query Gamma par batch de condition_ids depuis `my_positions WHERE simulated=true AND closed_at IS NULL`.**

Algorithme :

```python
async def run_once(self) -> None:
    open_positions = await self._positions_repo.list_open_virtual()
    if not open_positions:
        return
    condition_ids = list({p.condition_id for p in open_positions})
    markets = await self._gamma.get_markets_by_condition_ids(condition_ids)  # batch via ?condition_ids=
    resolved_markets = [m for m in markets if m.closed]
    for m in resolved_markets:
        winning_outcome = self._extract_winning_outcome(m)
        for pos in (p for p in open_positions if p.condition_id == m.condition_id):
            pnl = self._realized_pnl(pos, winning_outcome)
            await self._positions_repo.close_virtual(pos, closed_at=utc_now(), pnl_realized=pnl)
            await self._pnl_snapshot_repo.insert_dry_run_resolution_snapshot(pos, pnl)
            log.info("dry_run_position_resolved", asset_id=pos.asset_id, pnl=pnl, ...)
```

**`_extract_winning_outcome`** : Gamma expose `outcomes` (JSON array stringified) + `outcomePrices` (au moment de la résolution le prix = 1.0 pour l'outcome gagnant, 0.0 pour le perdant). Matching par `asset_id` ↔ `outcomeIndex`. Piège : certains marchés multi-outcome ou négatifs (neg_risk) demandent une logique plus complexe — pour v1, on se limite aux marchés binaires YES/NO (vérifier `len(outcomes) == 2`).

**Marchés neg_risk** : reportable M8.1. À v1 M8, si `neg_risk=True` sur la position → log `dry_run_resolution_neg_risk_unsupported` + skip (position reste open virtuellement). Documenté en §14.5.

**Alternatives écartées** :

- **Event subscription WebSocket** sur résolution : pas de channel dédié documenté.
- **Polling individual par market** : 1 call par position ouverte, verbeux. Batch `?condition_ids=<csv>` existe sur Gamma (M2 extension).
- **Résolution inférée depuis `my_orders` history** : fragile (on peut avoir des positions sans order récent).

### 2.5 Activation — flag env uniquement, pas de CLI flag

**Recommandation : `DRY_RUN_REALISTIC_FILL=true` env only. Pas de `--realistic-fill` CLI.**

Rationale :

- `DRY_RUN` est déjà overridable par `--dry-run` CLI. Ajouter un 2ᵉ flag CLI ajoute de la complexité.
- Le mode M8 est "long-running" (2-3 jours observés) → config `.env` plutôt qu'invocation CLI.
- Cohérent avec les autres features opt-in (`DASHBOARD_ENABLED`, `DISCOVERY_ENABLED`, `TELEGRAM_HEARTBEAT_ENABLED`).

**Alternatives écartées** :

- **Flag CLI `--realistic-fill`** : redondant.
- **Auto-activé en dry-run** (sans opt-in) : changement comportement breakable pour les users existants M3/M4.

### 2.6 Cache orderbook — TTL 5 s in-memory

**Recommandation : cache in-memory par `asset_id` avec TTL `DRY_RUN_BOOK_CACHE_TTL_SECONDS=5`.**

Rationale :

- Plusieurs `OrderApproved` sur le même marché dans la même seconde (ex: 2 wallets pinned tradent sur le même marché) → 1 seul GET `/book`.
- 5 s de stale acceptable : le book évolue sur l'échelle de la seconde mais le fill simulé ne cherche pas à être pixel-perfect.
- Cap de 500 entrées (LRU) pour éviter la fuite mémoire sur des runs longs. Implémentation : `functools.lru_cache` ne suffit pas (TTL), utiliser un dict + timestamp check.

```python
class OrderbookCache:
    def __init__(self, ttl_seconds: int, max_entries: int = 500):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: dict[str, tuple[datetime, Orderbook]] = {}

    async def get_or_fetch(self, asset_id: str, fetcher) -> Orderbook:
        now = utc_now()
        entry = self._store.get(asset_id)
        if entry and (now - entry[0]).total_seconds() < self._ttl:
            return entry[1]
        book = await fetcher(asset_id)
        self._store[asset_id] = (now, book)
        if len(self._store) > self._max:
            # LRU evict
            oldest = min(self._store.items(), key=lambda i: i[1][0])
            self._store.pop(oldest[0])
        return book
```

**Alternatives écartées** :

- **Pas de cache** : 1 call CLOB par ordre simulé. Acceptable au débit M5 mais gaspilleux si le user pousse `DRY_RUN_REALISTIC_FILL=true` + 20 pinned wallets actifs.
- **Cache distribué Redis** : overkill single-process.
- **Cache TTL 60 s** : trop stale, le prix peut bouger dramatiquement entre deux trades sur marchés news-driven.

### 2.7 Valuation live — `WalletStateReader` virtuel

**Recommandation : nouveau `VirtualWalletStateReader` qui réutilise le contrat `WalletStateReader` M3 (read-only, async) et remplace la lecture CLOB balances par une agrégation DB positions virtuelles + midpoint price.**

```python
class VirtualWalletStateReader:
    """Agrège les positions virtuelles open + valorisation mid-price CLOB."""

    def __init__(
        self,
        session_factory,
        clob_metadata: ClobMetadataClient,   # a get_midpoint(asset_id)
        settings: Settings,
    ) -> None: ...

    async def read_state(self) -> WalletState:
        positions = await self._positions_repo.list_open_virtual()
        unrealized = 0.0
        exposure = 0.0
        for pos in positions:
            mid = await self._clob_metadata.get_midpoint(pos.asset_id)   # cached M3 30s
            current_value = pos.size * mid
            unrealized += current_value - pos.size * pos.avg_price
            exposure += current_value
        realized = await self._positions_repo.sum_realized_pnl_virtual()
        total_usdc = self._settings.dry_run_virtual_capital_usd + realized + unrealized
        return WalletState(
            total_usdc=total_usdc,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            open_exposure_usd=exposure,
        )
```

Le `PnlSnapshotWriter` M4 a déjà une injection de `WalletStateReader`. À M8 :

```python
reader = (
    VirtualWalletStateReader(sf, metadata, settings)
    if settings.dry_run and settings.dry_run_realistic_fill
    else WalletStateReader(sf, metadata, clob_write, settings)
)
```

Le snapshot flag `is_dry_run=settings.dry_run` reste la source de vérité pour le dashboard M6. Pas de refactor `PnlSnapshotWriter`.

**Alternatives écartées** :

- **Re-implementer le PnL entièrement** : duplication M4.
- **Pas de valorisation live** : perd l'intérêt M8 (observer la courbe live).

### 2.8 Alerte Telegram — `dry_run_virtual_drawdown`

**Recommandation : nouvelle alerte INFO déclenchée par `PnlSnapshotWriter` si drawdown virtuel ≥ 50 % du seuil kill switch (sans déclencher le kill switch).**

```python
if snapshot.is_dry_run and snapshot.drawdown_pct >= 0.5 * settings.kill_switch_drawdown_pct:
    alerts_queue.put_nowait(Alert(
        level="INFO",
        event="dry_run_virtual_drawdown",
        body=f"Drawdown virtuel {snapshot.drawdown_pct:.1f}% — capital virtuel ${snapshot.total_usdc:.0f}",
        cooldown_key="dry_run_virtual_drawdown",
    ))
```

Level INFO (pas WARNING) → rappel subliminal que c'est **virtuel**, pas d'alarme panique. Cooldown M4 + digest M7 s'appliquent normalement.

**Jamais kill switch en dry-run** (M4 invariant).

---

## 3. Arborescence du module

Modifications minimales :

```
src/polycopy/executor/
├── __init__.py                       (inchangé)
├── clob_metadata_client.py           (étendu +1 méthode : get_orderbook)
├── clob_orderbook_reader.py          NOUVEAU : wrapper cache + fetch /book
├── clob_write_client.py              (inchangé)
├── dtos.py                           (+ RealisticFillResult, Orderbook, OrderbookLevel)
├── orchestrator.py                   (étendu : branche VirtualWalletStateReader conditionnellement)
├── pipeline.py                       (étendu : branche simulate_realistic_fill si flag + dry_run)
├── realistic_fill.py                 NOUVEAU : algorithme simulate_fill (pure function)
├── virtual_wallet_reader.py          NOUVEAU : VirtualWalletStateReader
├── dry_run_resolution_watcher.py     NOUVEAU : scheduler 30 min résolution marchés
└── wallet_state_reader.py            (inchangé)

src/polycopy/storage/
├── models.py                         (+ MyOrder.realistic_fill, MyPosition.simulated, MyPosition.realized_pnl)
├── repositories.py                   (+ MyPositionRepository.list_open_virtual, close_virtual, sum_realized_pnl_virtual)
└── dtos.py                           (+ WalletState: VirtualWalletState compat)

src/polycopy/monitoring/
└── pnl_writer.py                     (étendu : construit reader selon mode)

scripts/
└── pnl_report.py                     (étendu : --dry-run-mode flag + rapport enrichi)

alembic/versions/
└── 0004_m8_dry_run_realistic.py      NOUVEAU : migration (colonnes realistic_fill, simulated, realized_pnl)
```

**Pas de nouveau module top-level** : M8 étend `executor/` + `storage/` + `monitoring/` existants. Cohérent avec le contrat CLAUDE.md "pas d'abstraction prématurée".

---

## 4. API Polymarket — endpoints utilisés

### 4.1 Endpoint nouveau — CLOB `/book`

| Détail | Valeur |
|---|---|
| URL | `https://clob.polymarket.com/book?token_id=<asset_id>` |
| Method | GET |
| Auth | **Aucune** (read-only public) |
| Rate limit | Non documenté. Prudence : cache TTL 5s + max 60 req/min observé. |
| Fixture | `tests/fixtures/clob_orderbook_sample.json` à capturer avant code. |

### 4.2 Endpoints réutilisés M2/M3/M5 (aucune nouveauté)

| Endpoint | Rôle M8 |
|---|---|
| `GET /midpoint?token_id=<id>` (M2) | Valorisation live positions virtuelles via `VirtualWalletStateReader`. |
| `GET /markets?condition_ids=<csv>` (Gamma, M2/M5) | Batch check closed=true pour résolution. |
| `GET /neg-risk?condition_id=<id>` (M3) | Inchangé — lu à la simulation du fill pour `MyOrder.neg_risk` (audit). |

### 4.3 DTO `Orderbook` — `executor/dtos.py`

```python
class OrderbookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal
    size: Decimal


class Orderbook(BaseModel):
    """Snapshot orderbook CLOB, sides triés par prix décroissant (bids) / croissant (asks)."""
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    asset_id: str
    bids: list[OrderbookLevel]    # meilleur bid = bids[0]
    asks: list[OrderbookLevel]    # meilleur ask = asks[0]
    snapshot_at: datetime
    raw_hash: str | None = None
```

### 4.4 DTO `RealisticFillResult`

```python
class RealisticFillResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["SIMULATED", "REJECTED"]
    reason: str | None                   # "insufficient_liquidity" ou None
    requested_size: float
    filled_size: float                   # 0.0 si REJECTED FOK
    avg_fill_price: float | None         # None si REJECTED
    depth_consumed_shares: float
    depth_consumed_levels: int
    shortfall: float                     # requested - filled (0.0 si OK)
```

### 4.5 Rate limit audit

Pic théorique : 20 pinned + auto-découverts actifs × ~10 ordres/jour = 200 `/book` calls / 24h. Cache 5s réduit à ~180 (certains ordres sur même marché). **< 0.25 req/min moyenné, < 5 req/min peak.** Safe vs ~100 req/min documenté CLAUDE.md.

---

## 5. Storage — migration 0004 + extensions

### 5.1 `MyOrder` — extension

| Colonne | Type | Default | Migration |
|---|---|---|---|
| (existantes) | — | — | — |
| `realistic_fill` | `bool` | `False` | NOUVEAU. True ⟺ fill simulé via orderbook M8 (vs stub M3). |

### 5.2 `MyPosition` — extension

| Colonne | Type | Default | Migration |
|---|---|---|---|
| (existantes) | — | — | — |
| `simulated` | `bool` | `False` | NOUVEAU. True ⟺ position virtuelle M8, non correspondant à un fill réel CLOB. |
| `realized_pnl` | `float \| None` | `NULL` | NOUVEAU. Rempli à la résolution du marché (virtuel). NULL si position ouverte. |

**Index** : `ix_my_positions_simulated_open` partiel `WHERE simulated=1 AND closed_at IS NULL` pour accélérer `list_open_virtual`.

**Unicité** : la contrainte existante `UniqueConstraint("condition_id", "asset_id")` devient invalide si on a virtuel + réel sur le même marché. **À corriger** : étendre la contrainte à `("condition_id", "asset_id", "simulated")`. **Important** : en SQLite, la modification d'une `UNIQUE` nécessite `op.batch_alter_table` (recréation table) — cohérent avec le pattern M5 migration 0003.

### 5.3 `PnlSnapshot` — pas de colonne nouvelle

Table déjà déclare `is_dry_run: bool` depuis M4. M8 réutilise — les snapshots `is_dry_run=true` portent le PnL virtuel.

### 5.4 Migration Alembic — `0004_m8_dry_run_realistic.py`

Étapes (auto-generate puis audit manuel SQLite friendly) :

1. `op.batch_alter_table("my_orders")` : add column `realistic_fill BOOLEAN NOT NULL DEFAULT 0`.
2. `op.batch_alter_table("my_positions")` :
   - Add column `simulated BOOLEAN NOT NULL DEFAULT 0`.
   - Add column `realized_pnl FLOAT NULL`.
   - Drop `uq_my_positions_condition_asset`.
   - Create `uq_my_positions_condition_asset_simulated UNIQUE (condition_id, asset_id, simulated)`.
3. `op.create_index("ix_my_positions_simulated_open", "my_positions", ["simulated", "closed_at"])`.

Backfill : aucun. Les existantes ont `realistic_fill=0` + `simulated=0` + `realized_pnl=NULL` par default.

**Test obligatoire** `tests/unit/test_m8_alembic_migration.py` :

- Apply 0001+0002+0003+0004 → vérifie colonnes + index + contrainte unique triple.
- Test insertion : même `(condition_id, asset_id)` avec `simulated=0` et `simulated=1` → 2 rows OK.
- Test conflit : 2ᵉ insert `simulated=0` même `(condition_id, asset_id)` → `IntegrityError`.

---

## 6. DTOs + repositories

### 6.1 `MyPositionRepository` extensions

```python
class MyPositionRepository:
    # existants : upsert, list_open, close_if_empty...

    async def list_open_virtual(self) -> list[MyPosition]:
        """Positions virtuelles ouvertes (simulated=True, closed_at=NULL)."""
        stmt = select(MyPosition).where(
            MyPosition.simulated.is_(True),
            MyPosition.closed_at.is_(None),
        )
        ...

    async def close_virtual(
        self, position_id: int, *, closed_at: datetime, realized_pnl: float,
    ) -> None: ...

    async def sum_realized_pnl_virtual(self) -> float:
        """Somme des realized_pnl des positions virtuelles closed."""
        stmt = select(func.coalesce(func.sum(MyPosition.realized_pnl), 0.0)).where(
            MyPosition.simulated.is_(True),
            MyPosition.closed_at.is_not(None),
        )
        ...

    async def upsert_virtual(self, ...) -> MyPosition:
        """Upsert sur (condition_id, asset_id, simulated=True). Weighted avg price update."""
        ...
```

### 6.2 `MyOrderRepository` extensions

```python
class MyOrderRepository:
    # existants : insert, list_recent, ...

    async def insert_realistic_simulated(self, dto: RealisticSimulatedOrderDTO) -> MyOrder:
        """Insert avec simulated=True + realistic_fill=True."""
        ...

    async def list_virtual_orders(self, limit: int = 200) -> list[MyOrder]: ...

    async def sum_virtual_volume_usd(self, since: datetime) -> float: ...
```

---

## 7. Pipeline — `simulate_realistic_fill`

### 7.1 Branche d'entrée — `pipeline.py`

Le pipeline M3 existant :

```python
async def execute_order(order, ctx) -> MyOrder:
    _validate_dry_run_invariants(order, ctx)
    metadata = await ctx.metadata_client.get_market_metadata(order.condition_id)
    tick_size = await ctx.metadata_client.get_tick_size(order.asset_id)
    rounded = _round_price_to_tick(order.price, tick_size)
    if ctx.settings.dry_run:
        return await _persist_simulated_stub(order, metadata, tick_size, rounded, ctx)
    await _assert_capital_available(order, ctx)
    resp = await ctx.write_client.create_and_post_order(...)
    return await _persist_sent_order(resp, order, ...)
```

À M8 on ajoute une branche :

```python
if ctx.settings.dry_run:
    if ctx.settings.dry_run_realistic_fill:
        return await _persist_realistic_simulated(order, metadata, tick_size, rounded, ctx)
    return await _persist_simulated_stub(...)
```

`_persist_realistic_simulated` :

```python
async def _persist_realistic_simulated(order, metadata, tick_size, rounded, ctx) -> MyOrder:
    assert ctx.settings.dry_run is True  # 4ᵉ garde-fou M8
    book = await ctx.orderbook_reader.get_orderbook(order.asset_id)
    fill = simulate_fill(
        order, book,
        allow_partial=ctx.settings.dry_run_allow_partial_book,
    )
    if fill.status == "REJECTED":
        log.info("order_realistic_fill_rejected", reason=fill.reason, shortfall=fill.shortfall, ...)
        my_order = await ctx.order_repo.insert_realistic_simulated(
            _build_rejected_dto(order, metadata, tick_size, rounded, fill),
        )
        return my_order
    log.info("order_realistic_fill_simulated",
             asset_id=order.asset_id, avg_fill_price=fill.avg_fill_price,
             depth_consumed_shares=fill.depth_consumed_shares,
             depth_consumed_levels=fill.depth_consumed_levels)
    my_order = await ctx.order_repo.insert_realistic_simulated(
        _build_simulated_dto(order, metadata, tick_size, fill),
    )
    # upsert position virtuelle
    await ctx.position_repo.upsert_virtual(
        condition_id=order.condition_id,
        asset_id=order.asset_id,
        side=order.side,
        filled_size=fill.filled_size,
        fill_price=fill.avg_fill_price,
    )
    return my_order
```

### 7.2 `_round_price_to_tick` — inchangé

M3 arrondit au `tick_size` du marché. M8 hérite ce comportement : `price` stored est `rounded`, mais `avg_fill_price` est le vrai prix moyen pondéré (sans arrondi, pour audit). Nouvelle colonne `MyOrder.avg_fill_price: float | None` ? **Non** — on économise la colonne et on stocke `avg_fill_price` dans `price`. Le `requested_price` original est perdu mais accessible via log (structlog preserve).

**Alternative tranchée** : ajouter `MyOrder.requested_price` en colonne ? Reportable M8.1 si l'audit audit le demande.

### 7.3 `_build_rejected_dto` / `_build_simulated_dto`

```python
def _build_simulated_dto(order, metadata, tick_size, fill):
    return RealisticSimulatedOrderDTO(
        source_tx_hash=order.source_tx_hash,
        condition_id=order.condition_id,
        asset_id=order.asset_id,
        side=order.side,
        size=fill.filled_size,
        price=fill.avg_fill_price,
        tick_size=tick_size,
        neg_risk=metadata.neg_risk,
        order_type="FOK",
        status="SIMULATED",
        simulated=True,
        realistic_fill=True,
        taking_amount=None,
        making_amount=None,
        transaction_hashes=[],
        error_msg=None,
    )
```

### 7.4 Contexte exécution étendu

`ExecutorContext` (ou équivalent) gagne un champ `orderbook_reader: ClobOrderbookReader` et `position_repo: MyPositionRepository`. Instancié une seule fois par orchestrator, partagé entre tous les `execute_order`.

---

## 8. Résolution de marchés virtuels

### 8.1 `DryRunResolutionWatcher` — `src/polycopy/executor/dry_run_resolution_watcher.py`

```python
class DryRunResolutionWatcher:
    """Boucle périodique : détecte les marchés résolus et close les positions virtuelles."""

    def __init__(
        self,
        session_factory,
        gamma_client: GammaApiClient,
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert] | None = None,
    ) -> None: ...

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        if not (self._settings.dry_run and self._settings.dry_run_realistic_fill):
            return  # M8 désactivé, ne lance pas la boucle
        interval_s = self._settings.dry_run_resolution_poll_minutes * 60
        log.info("dry_run_resolution_started", interval_s=interval_s)
        while not stop_event.is_set():
            try:
                await self._run_once()
            except Exception:
                log.exception("dry_run_resolution_cycle_failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                return
            except TimeoutError:
                pass
        log.info("dry_run_resolution_stopped")

    async def _run_once(self) -> None: ...
```

Lancé dans le même TaskGroup que l'executor via `ExecutorOrchestrator.run_forever` (extension §8.2).

### 8.2 `ExecutorOrchestrator` — extension M8

```python
async def run_forever(self, stop_event):
    # ... init existant ...
    if self._settings.dry_run and self._settings.dry_run_realistic_fill:
        resolution_watcher = DryRunResolutionWatcher(
            self._session_factory, gamma_client, self._settings, self._alerts,
        )
    async with asyncio.TaskGroup() as tg:
        tg.create_task(self._consume_loop(stop_event))
        if self._settings.dry_run and self._settings.dry_run_realistic_fill:
            tg.create_task(resolution_watcher.run_forever(stop_event))
    log.info("executor_stopped")
```

### 8.3 Calcul PnL réalisé

Pour un marché binaire YES/NO :

- Outcome gagnant = prix final 1.0, perdant = 0.0.
- PnL réalisé = `size × (1.0 if winning_asset_id == pos.asset_id else 0.0) - size × avg_entry_price`.
- Side distinction : déjà intégré dans `MyPosition.size` (négatif si SELL? Non — M3 traite BUY et SELL comme des longs sur le token outcome. Un SELL YES = un BUY NO ailleurs, mais M3 garde la convention token_id). **À vérifier** : M3 stocke un `size` toujours positif (nombre de shares held). Si SELL → la position existait déjà (on réduit). En M8, simulate un `SELL` sur une position virtuelle inexistante → log `dry_run_sell_without_position` warning + skip.

**Piège §14.5** : la logique de close/open virtuel sur SELL demande un design explicite. **Recommandation v1** : M8 simule uniquement des BUY virtuels (matchent le cas "copy-trading directionnel"). Les SELL virtuels en dry-run sont **ignorés** (log warning). Documenté.

### 8.4 Marchés neg_risk — skip v1

Comme annoncé §2.4 : neg_risk = résolution complexe (multi-outcome non-binaire). v1 M8 skip + log `dry_run_resolution_neg_risk_unsupported`. La position reste ouverte virtuellement, visible dashboard, pas de realized_pnl généré. Documenté §14.5.

---

## 9. Tests

### 9.1 Arborescence

```
tests/
├── fixtures/
│   └── clob_orderbook_sample.json           NOUVEAU (capturé §0.2)
├── unit/
│   ├── test_realistic_fill.py               NOUVEAU (algorithme pure)
│   ├── test_clob_orderbook_reader.py        NOUVEAU (respx + cache TTL)
│   ├── test_pipeline_m8_branch.py           NOUVEAU (dry_run + realistic_fill → _persist_realistic_simulated)
│   ├── test_virtual_wallet_reader.py        NOUVEAU
│   ├── test_dry_run_resolution_watcher.py   NOUVEAU
│   ├── test_my_position_repo_virtual.py     NOUVEAU
│   ├── test_my_order_repo_realistic.py      NOUVEAU
│   ├── test_m8_alembic_migration.py         NOUVEAU
│   ├── test_pnl_writer_m8_mode.py           NOUVEAU (snapshot dry_run vs real)
│   ├── test_pnl_report_dry_run_mode.py      NOUVEAU
│   ├── test_queries_segregation.py          NOUVEAU (real vs virtual non mélangés)
│   └── test_executor_orchestrator_m8.py     NOUVEAU (lance resolution watcher conditionnellement)
└── integration/
    └── test_clob_book_live.py               NOUVEAU @pytest.mark.integration
```

### 9.2 `test_realistic_fill.py` — algorithme pur

- BUY size=100 sur book asks=[(0.08, 50), (0.09, 60)] → filled=100, avg_price=0.085 (50×0.08 + 50×0.09)/100.
- BUY size=200 sur book asks=[(0.08, 50), (0.09, 60)] total=110 → REJECTED `insufficient_liquidity` (allow_partial=False).
- BUY size=200 sur même book, allow_partial=True → filled=110, avg_price pondéré sur 110 shares.
- SELL size=50 sur book bids=[(0.07, 100)] → filled=50, avg_price=0.07.
- BUY size=0.001 sur book asks=[(0.08, 1.0)] → filled=0.001 (tolérance float via Decimal).
- Book vide (asks=[]) → REJECTED.
- Property test `hypothesis` : `filled_size ≤ requested_size`, `avg_price ≥ price_level_min` dans tous les cas simulés.

### 9.3 `test_clob_orderbook_reader.py` (respx)

- 200 happy path → parse book correctement (Decimal sur prix/size).
- Cache TTL 5s : 2 `get_orderbook(asset_id)` en < 5s → 1 seul respx call.
- Refresh après TTL : respx called 2 fois.
- 429 retry tenacity (héritage M3 pattern).
- 404 `/book` → raise `OrderbookNotFound` + logged.
- LRU eviction à 500+ entries.

### 9.4 `test_pipeline_m8_branch.py`

- `dry_run=true, realistic_fill=false` → M3 path `_persist_simulated_stub` inchangé (non-régression).
- `dry_run=true, realistic_fill=true` → fetch book + simulate_fill + persist avec `realistic_fill=True`.
- `dry_run=false` → M3 live path (ignore `realistic_fill` flag — jamais fetché `/book`).
- Garde-fou 4ᵉ M8 : call `_persist_realistic_simulated` avec `dry_run=false` → `AssertionError` (test breakglass).
- Book rejected → `MyOrder.status='REJECTED'`, `error_msg="insufficient_liquidity"`, `realistic_fill=True`, pas de position créée.
- Book accepted → `MyOrder.status='SIMULATED'`, `price=avg_fill_price`, position virtuelle upsert.

### 9.5 `test_virtual_wallet_reader.py`

- 0 positions virtuelles → `WalletState(total=virtual_capital, unrealized=0, realized=0)`.
- 2 positions virtuelles ouvertes + mock midpoint → unrealized calculé.
- 1 position virtuelle close avec realized_pnl → somme correcte dans total.
- Mock midpoint 404 → log warning + skip la position dans le calcul (pas de crash).

### 9.6 `test_dry_run_resolution_watcher.py`

- Pas de positions virtuelles → cycle no-op.
- 1 position + Gamma `closed=false` → pas de close.
- 1 position sur marché binaire `closed=true, winning_outcome=YES` + position sur YES → close avec realized_pnl positif.
- Même marché mais position sur NO → close avec realized_pnl négatif.
- 1 position neg_risk → log warning + skip (pas de close).
- Exception Gamma → log + continue (cycle suivant retry).
- stop_event set → sortie propre.

### 9.7 `test_my_position_repo_virtual.py`

- `upsert_virtual` BUY size=10 price=0.08 → nouvelle position (0, 10, 0.08).
- `upsert_virtual` BUY size=10 price=0.09 → position (20, 0.085 avg).
- `list_open_virtual` avec 3 positions (1 virt open, 1 virt closed, 1 real open) → retourne 1.
- `close_virtual(pos_id, realized_pnl=5.0)` → `closed_at` set, `realized_pnl=5.0`.
- `sum_realized_pnl_virtual` : 2 positions closed (+10, -3) → 7.0.
- Unicité triple `(condition_id, asset_id, simulated)` testée.

### 9.8 `test_m8_alembic_migration.py`

- Apply 0001..0004 sur DB vide → columns + index + unique constraint OK.
- Apply sur DB M7 existante (0001..0003) → upgrade 0004 n'écrase pas les orders existants (ils ont `realistic_fill=False` par défault).
- Rollback 0004 → reverse OK sur SQLite (batch_alter_table).

### 9.9 `test_pnl_writer_m8_mode.py`

- `dry_run=true, realistic_fill=true` → `PnlSnapshotWriter` utilise `VirtualWalletStateReader`.
- `dry_run=false` → utilise `WalletStateReader` réel.
- Snapshot inséré avec `is_dry_run=true` en M8 mode.
- Kill switch **NE déclenche PAS** en dry-run (M4 invariant préservé).
- Alerte `dry_run_virtual_drawdown` envoyée si drawdown ≥ 50 % × `kill_switch_drawdown_pct` et mode dry-run.

### 9.10 `test_pnl_report_dry_run_mode.py`

- `python scripts/pnl_report.py --dry-run-mode --output html` → rapport HTML avec `is_dry_run=true` only.
- Stats : équity curve, drawdown max, sharpe approx (σ daily returns), heatmap par market.
- Edge case : 0 snapshots dry-run → message "Aucune donnée dry-run".

### 9.11 `test_queries_segregation.py`

- Mélange virtuel + réel en DB : `queries.get_pnl_chart_data(mode="real")` retourne seulement is_dry_run=false.
- `mode="dry_run"` retourne is_dry_run=true.
- `mode="both"` retourne les deux (utilisé si dashboard toggle).
- `MyPositionRepository.list_open` (non-virtual) **ignore** les virtual → sanity check segregation.

### 9.12 `test_clob_book_live.py` (opt-in)

```python
@pytest.mark.integration
async def test_fetch_real_orderbook():
    # Choisir un token_id public liquide (capturer au test time)
    token_id = "..."
    async with httpx.AsyncClient() as http:
        reader = ClobOrderbookReader(http, ttl_seconds=5)
        book = await reader.get_orderbook(token_id)
    assert len(book.asks) > 0 or len(book.bids) > 0
```

### 9.13 Couverture

```bash
pytest --cov=src/polycopy/executor --cov-report=term-missing   # ≥ 80% (M3 + M8)
pytest --cov=src/polycopy/storage --cov-report=term-missing    # ≥ 80%
```

Seuils : 80 % sur `src/polycopy/executor/` (zone critique M3/M8). M1..M7 non-régression ≥ 80 %.

---

## 10. Mises à jour de documentation (même PR)

### 10.1 `README.md`

Table env vars — ajouter 5 lignes :

```markdown
| `DRY_RUN_REALISTIC_FILL` | Simule les fills sur la profondeur orderbook (opt-in, DRY_RUN=true only) | `false` | non |
| `DRY_RUN_VIRTUAL_CAPITAL_USD` | Capital initial virtuel pour le PnL dry-run | `1000.0` | non |
| `DRY_RUN_BOOK_CACHE_TTL_SECONDS` | TTL cache /book par asset_id | `5` | non |
| `DRY_RUN_RESOLUTION_POLL_MINUTES` | Cadence check résolution marchés virtuels | `30` | non |
| `DRY_RUN_ALLOW_PARTIAL_BOOK` | FOK strict si false, fill partiel si true | `false` | non |
```

Nouvelle section après "Going live" :

```markdown
## Dry-run "semi-réel" (optionnel, M8)

Par défaut, `DRY_RUN=true` log l'ordre et s'arrête. Avec M8, tu peux observer le PnL que tu aurais eu :

\`\`\`env
DRY_RUN=true
DRY_RUN_REALISTIC_FILL=true
DRY_RUN_VIRTUAL_CAPITAL_USD=1000
\`\`\`

Le bot simule alors chaque ordre FOK sur l'orderbook réel (GET /book, read-only), persiste la position virtuelle, valorise live via mid-price, et résout le PnL à la clôture du marché. Aucun capital engagé, aucune signature CLOB requise.

Dashboard : la page `/pnl` expose un toggle "réel / dry-run virtuel". Rapport HTML dédié :

\`\`\`bash
python scripts/pnl_report.py --dry-run-mode --output html
\`\`\`

Garde-fou : `DRY_RUN=false` reste le **seul** trigger d'un ordre réel. Le 4ᵉ garde-fou M8 assert explicitement.
```

Roadmap : cocher `[x] **M8** : Dry-run réaliste (fill orderbook, PnL virtuel live, résolution marchés)`.

### 10.2 `docs/architecture.md`

Étendre la section "Module : Executor" :

```markdown
> **Status M8** ✅ — dry-run réaliste. Si `DRY_RUN=true` ET `DRY_RUN_REALISTIC_FILL=true`, l'executor simule chaque FOK via `GET /book` (read-only public), calcule le prix moyen pondéré level-by-level, persiste l'ordre + la position virtuelle (`simulated=True`). Un `DryRunResolutionWatcher` tourne dans le TaskGroup executor pour détecter les marchés `closed=true` (Gamma polling 30 min) et matérialiser le realized_pnl. `VirtualWalletStateReader` somme positions virtuelles × midpoint pour alimenter `PnlSnapshotWriter` M4 avec `is_dry_run=true`. **Kill switch jamais déclenché en dry-run** (invariant M4 préservé). Voir `specs/M8-dry-run-realistic.md`.
```

### 10.3 `CLAUDE.md`

Section "Sécurité — RÈGLES STRICTES", étendre :

```markdown
- **Dry-run M8** : `DRY_RUN_REALISTIC_FILL=true` active la simulation orderbook. **Triple garde-fou M3 préservé intact** + 4ᵉ M8 : `assert dry_run is True` avant chaque `simulate_realistic_fill`. Aucune creds consommée (read-only public `/book`, `/midpoint`, Gamma). Positions virtuelles ségréguées via colonne `simulated=True` + contrainte unique triple. Kill switch **JAMAIS** en dry-run (invariant M4). Alerte `dry_run_virtual_drawdown` INFO only. SELL sur position virtuelle inexistante = warning + skip (v1 scope : BUY only). Marchés neg_risk = skip résolution v1 (reportable M8.1).
```

### 10.4 `docs/setup.md`

Ajouter **section 17** :

```markdown
## 17. Activer le dry-run réaliste (M8, optionnel)

Objectif : laisser le bot tourner 2-3 jours sans capital engagé tout en observant le PnL simulé.

\`\`\`env
DRY_RUN=true
DRY_RUN_REALISTIC_FILL=true
DRY_RUN_VIRTUAL_CAPITAL_USD=1000
DRY_RUN_RESOLUTION_POLL_MINUTES=30
\`\`\`

Relance le bot. Tu verras dans les logs :

- `dry_run_realistic_fill_enabled` au boot.
- `order_realistic_fill_simulated` à chaque copy-trade (avec `avg_fill_price`, `depth_consumed_levels`).
- `order_realistic_fill_rejected reason=insufficient_liquidity` si le book est trop fin pour la taille demandée (FOK strict, cohérent avec le live).
- `dry_run_position_resolved` toutes les 30 min quand un marché virtuel se résout.

Dashboard `/pnl` affiche un toggle "réel / virtuel / les deux".

Rapport dédié :

\`\`\`bash
python scripts/pnl_report.py --dry-run-mode --since 7 --output html
# → dry_run_pnl_report.html
\`\`\`

Contient : équity curve virtuelle, drawdown max, sharpe approximé, heatmap par marché, win rate des positions résolues.

Troubleshooting :

- `insufficient_liquidity` récurrent → normal sur marchés très peu liquides ; baisse `MAX_POSITION_USD` ou active `DRY_RUN_ALLOW_PARTIAL_BOOK=true` (s'écarte du comportement live).
- Positions virtuelles qui ne se résolvent pas → check `DRY_RUN_RESOLUTION_POLL_MINUTES` ; les marchés neg_risk sont skipped v1.
- PnL virtuel qui ne bouge pas → check `WalletStateReader` mid-price cache M3 30s ; les snapshots sont toutes les 5 min (`PNL_SNAPSHOT_INTERVAL_SECONDS`).
- **Passage au live** : mettre `DRY_RUN=false` + clés + `MAX_POSITION_USD=1`. Le flag `DRY_RUN_REALISTIC_FILL` est **ignoré** en live (cohérent, jamais de fill virtuel en prod).
```

---

## 11. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy/executor --cov=src/polycopy/storage --cov-report=term-missing   # ≥ 80 %
pytest -m integration                                                                      # opt-in CLOB book

alembic upgrade head                                                                       # 0001..0004 OK
alembic current                                                                            # head = 0004

# Smoke test M8
DRY_RUN=true DRY_RUN_REALISTIC_FILL=true \
DRY_RUN_VIRTUAL_CAPITAL_USD=1000 \
python -m polycopy --dry-run &
sleep 10
# Vérifier logs : executor_started, dry_run_realistic_fill_enabled, dry_run_resolution_started
kill %1 && wait                                                                             # exit 0

# Rapport PnL dry-run
python scripts/pnl_report.py --dry-run-mode --since 1 --output html
ls -la dry_run_pnl_report.html                                                              # fichier généré
```

---

## 12. Critères d'acceptation

- [ ] `DRY_RUN=true DRY_RUN_REALISTIC_FILL=true python -m polycopy --dry-run` tourne ≥ 60 s sans crash. Logs : `executor_started`, `dry_run_realistic_fill_enabled` (warning), `dry_run_resolution_started`.
- [ ] `DRY_RUN_REALISTIC_FILL=false` (default) → comportement M3 strict préservé (fill stub, pas de call `/book`, pas de position virtuelle créée). Tests M3 passent sans diff.
- [ ] `DRY_RUN=false` → `realistic_fill` flag ignoré, M3 live path inchangé. Test : `DRY_RUN=false DRY_RUN_REALISTIC_FILL=true python -m polycopy` démarre en live comme M3.
- [ ] Ordre BUY simulé M8 : `MyOrder.status='SIMULATED'`, `realistic_fill=True`, `simulated=True`, `price=avg_fill_price` calculé sur book réel. Position virtuelle upsert avec `simulated=True`.
- [ ] Ordre BUY book insuffisant (FOK strict) : `MyOrder.status='REJECTED'`, `error_msg="insufficient_liquidity"`, `realistic_fill=True`, pas de position créée.
- [ ] `DRY_RUN_ALLOW_PARTIAL_BOOK=true` → fill partiel accepté, `filled_size < requested_size`, log `order_realistic_fill_partial`.
- [ ] `DryRunResolutionWatcher` : marché binaire `closed=true` + position YES sur outcome gagnant → position close avec `realized_pnl > 0`. Marché neg_risk → skip + log warning.
- [ ] `VirtualWalletStateReader` : `total_usdc = virtual_capital + realized + unrealized`. Unrealized calculé via midpoint × size.
- [ ] Snapshot dry-run déclenche `dry_run_virtual_drawdown` INFO si drawdown ≥ 50 % × kill_switch_drawdown_pct. **Aucun kill switch en dry-run.**
- [ ] Migration 0004 applique colonnes + index + contrainte unique triple sans casser 0001..0003. Tests migration OK.
- [ ] Ségrégation : `MyPositionRepository.list_open` (non-virtual) **n'inclut pas** les positions virtuelles. `list_open_virtual` **ne retourne que** les virtuels.
- [ ] Dashboard `/pnl?mode=dry_run` montre uniquement snapshots `is_dry_run=true`. `?mode=real` uniquement `false`. `?mode=both` les deux, courbes séparées visuellement.
- [ ] `scripts/pnl_report.py --dry-run-mode --output html` génère un rapport distinct avec stats virtuelles (équity curve, drawdown max, sharpe approx, heatmap par marché, win rate).
- [ ] 4ᵉ garde-fou M8 : call manuel `_persist_realistic_simulated` avec `dry_run=false` → `AssertionError`. Testé.
- [ ] Aucune creds loggée (`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, L2 api_key/secret/passphrase, `TELEGRAM_BOT_TOKEN`, `GOLDSKY_API_KEY` hypothétique) dans les logs M8 ni dans le rapport HTML.
- [ ] Rate limit observé : cache book 5s + 30 min résolution → pic < 10 req/min, moyenne < 1 req/min.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src` (`--strict`) : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `src/polycopy/executor/` + `src/polycopy/storage/`. Non-régression M1..M7 ≥ 80 %.
- [ ] Docs §10 à jour (`README.md`, `docs/architecture.md`, `CLAUDE.md`, `docs/setup.md` §17) dans le **même** commit que le code.
- [ ] Commit final unique : `feat(executor,storage): M8 dry-run realistic fill + virtual PnL`.

---

## 13. Hors scope M8 (NE PAS implémenter)

- **SELL virtual sur position inexistante** : v1 skip + warning. Le design close/reduce virtuel est reportable M8.1.
- **Marchés neg_risk résolution** : v1 skip + warning. Logique multi-outcome reportable M8.1.
- **Slippage Monte Carlo** (modélisation stochastique des mouvements de prix entre détection et simulation). v1 utilise le snapshot book instantané.
- **Fills partiels exécutés progressivement** (simulate staggered execution). v1 est one-shot FOK.
- **Maker rebates simulés**. M3 live ne fait que taker, v1 M8 idem.
- **Order types autres que FOK** (GTC, GTD, FAK, post-only). M3 n'émet que FOK.
- **Backtesting historique** (rejouer 30 jours passés). Scope `scripts/score_backtest.py` (M5) ; M8 est live-forward uniquement.
- **Kill switch virtuel qui coupe le bot**. Invariant M4 : **jamais en dry-run**. M8 respecte.
- **Auto-tune `DRY_RUN_VIRTUAL_CAPITAL_USD`** selon le vrai wallet on-chain. User pose une valeur explicite.
- **Simulations multi-scénarios** (what-if avec slippage, fees, etc. variables). Hors scope.
- **Export des positions virtuelles en CSV** séparé. `scripts/pnl_report.py --dry-run-mode --output csv` couvre.
- **Dashboard `/dry-run` page dédiée**. M8 réutilise `/pnl` avec toggle.
- **Persistance des books `/book` snapshot en DB** pour replay. Cache in-memory suffit.
- **Refactor M3 executor path live**. Diff strictement additif.
- **WebSocket CLOB `market` channel** pour prix temps réel. Polling midpoint 30s (M3 existant) suffit.
- **RiskManager M2 lit `DRY_RUN_VIRTUAL_CAPITAL_USD`** : non. Le capital virtuel sert uniquement à la valorisation, pas aux décisions.

---

## 14. Notes d'implémentation + zones d'incertitude

### 14.1 Ordre de travail suggéré

1. **Capturer fixture `clob_orderbook_sample.json`** : `curl 'https://clob.polymarket.com/book?token_id=<token_liquide>' > tests/fixtures/clob_orderbook_sample.json`. Vérifier schéma, ajuster DTOs si divergence.
2. **Migration Alembic 0004** : auto-generate + audit SQLite `batch_alter_table`. Test migration.
3. **Modèles** : `MyOrder.realistic_fill`, `MyPosition.simulated`, `MyPosition.realized_pnl`. Mettre à jour contraintes uniques.
4. **Repositories** : `MyPositionRepository.{upsert_virtual, list_open_virtual, close_virtual, sum_realized_pnl_virtual}` + `MyOrderRepository.insert_realistic_simulated`.
5. **DTOs** : `Orderbook`, `OrderbookLevel`, `RealisticFillResult`, `RealisticSimulatedOrderDTO`.
6. **Algorithm pure** `realistic_fill.py` : `simulate_fill(order, book, allow_partial) -> RealisticFillResult`. Tests exhaustifs (§9.2).
7. **`ClobOrderbookReader`** : httpx + cache TTL + tenacity retry (pattern M3).
8. **Pipeline branche M8** : `_persist_realistic_simulated` avec 4ᵉ garde-fou `assert dry_run is True`. Tests de non-régression M3 path.
9. **`VirtualWalletStateReader`** : lit positions virtuelles + midpoint, calcule total.
10. **`PnlSnapshotWriter` extension** : branche reader selon mode.
11. **`DryRunResolutionWatcher`** : boucle 30 min, Gamma batch, résolution.
12. **`ExecutorOrchestrator`** : lance resolution_watcher conditionnellement.
13. **Dashboard** (M6 branché) : query params `?mode=` sur `/pnl`.
14. **`scripts/pnl_report.py`** : flag `--dry-run-mode`, rapport enrichi.
15. **Alerte `dry_run_virtual_drawdown`** : producteur dans `PnlSnapshotWriter`.
16. **Smoke test réel** : lancer 1 h avec 1 pinned wallet actif, observer `order_realistic_fill_simulated` + `PnlSnapshot` dry-run.
17. **Doc §10** dans le même commit.
18. **Commit unique** : `feat(executor,storage): M8 dry-run realistic fill + virtual PnL`.

### 14.2 Principes

- **Diff strictement additif** sur M3 live path : zéro ligne modifiée dans `_persist_sent_order`, `_assert_capital_available`, `ClobWriteClient.create_and_post_order`. M8 ajoute une branche, ne refactor pas.
- **Decimal pour les calculs orderbook**, float pour la persistance. Évite les erreurs d'arrondi sur des books à 20 levels.
- **`assert dry_run is True`** dans `_persist_realistic_simulated` — 4ᵉ garde-fou au-delà des 3 M3. Defense in depth.
- **Ségrégation par colonne `simulated=True/False`** — unique constraint triple clé, tests dédiés.
- **Kill switch invariant préservé** : `PnlSnapshotWriter` ne déclenche jamais `stop_event.set()` en dry-run (M4 code existant inchangé).
- **Session courtes, append-only pour les snapshots** — pattern M4 préservé.

### 14.3 Décisions auto-arbitrées

1. **FOK strict par défaut** : cohérent avec executor M3 live. `DRY_RUN_ALLOW_PARTIAL_BOOK=true` opt-in pour qui veut observer un comportement partiel.
2. **Cache TTL 5 s** : compromis fraîcheur / efficacité. Plus court = doublons. Plus long = stale.
3. **Résolution polling 30 min** : marchés résolus ne bougent pas à la seconde. Cycle 30 min suffisant.
4. **v1 BUY only** : le design SELL virtuel sur position inexistante est un rabbit hole. Skip + warning + doc.
5. **v1 skip neg_risk** : multi-outcome demande design dédié. Warning + doc.
6. **Colonnes booléennes `realistic_fill` + `simulated`** (vs enum `status`) : plus simple à filtrer en query, plus explicite.
7. **Alerte drawdown virtuel INFO** : mot "virtuel" explicite + niveau non-critique. Pas de WARNING confusant.
8. **Rapport HTML enrichi** via flag `--dry-run-mode` : réutilise `scripts/pnl_report.py` existant, extension minimale.
9. **Pas de table `dry_run_snapshots` séparée** : `pnl_snapshots.is_dry_run=true` suffit.
10. **Pas de refactor `RiskManager` M2** : le capital virtuel ne pilote pas les décisions — seulement la valorisation.
11. **Fetch book **après** `_round_price_to_tick` du M3** : l'algo fill simulate utilise le price requested (déjà arrondi), mais fill au prix level pondéré. Le `price` persisté est l'avg pondéré, pas le requested.
12. **`DRY_RUN_VIRTUAL_CAPITAL_USD=1000` default** : cohérent avec le stub M3 `RISK_AVAILABLE_CAPITAL_USD_STUB=1000`.
13. **`MyOrder.price` stocke `avg_fill_price`** : on perd le requested_price. Logs structlog preserve si audit. Reportable M8.1 si besoin.
14. **Pas d'auto-activation à l'upgrade** : un user M7 qui pull `main` ne voit rien changer sauf s'il pose `DRY_RUN_REALISTIC_FILL=true`.
15. **`DryRunResolutionWatcher` lancé depuis `ExecutorOrchestrator`** (pas un nouveau top-level) : cohérence "executor gère tout ce qui touche aux ordres".

### 14.4 Pièges anticipés

1. **Book string → Decimal** : `OrderbookLevel.price = Decimal(str(raw))` obligatoire. Pas `Decimal(float_value)` (imprécis).
2. **Side confusion** : BUY = consomme asks, SELL = consomme bids. Sign swap in loop order.
3. **Fill empty book** : asks = [] → REJECTED `empty_book` (distinct de `insufficient_liquidity`).
4. **Position upsert race** : 2 ordres simultanés sur même marché → `UniqueConstraint` triple. Transaction SQLAlchemy gère. Testé.
5. **Midpoint 404 en valuation** : log warning + skip position. Pas de crash `VirtualWalletStateReader`.
6. **Kill switch vs dry-run invariant** : si jamais quelqu'un refactor `PnlSnapshotWriter` sans préserver `if not is_dry_run: check_kill_switch(...)`, test `test_pnl_writer_m8_mode.py` bloque.
7. **Gamma `outcomes` stringified** (piège M2 connu) : parser `json.loads(market.outcomes)` pour identifier le winning outcome.
8. **Neg_risk detection** : `market.neg_risk` présent en Gamma metadata. Si absent → assumer False (cohérent).
9. **SELL sur position inexistante** : v1 skip + log warning. S'assurer qu'aucun crash (pipeline continue).
10. **Resolution cycle crash** : try/except enveloppant `_run_once` — un exception Gamma ne doit pas tuer la boucle.
11. **Cache book éviction** : LRU naive `min(by timestamp)` → coût `O(N)` par eviction. À 500 entries, acceptable. Optimisable en `OrderedDict` si besoin.
12. **Decimal → float conversion** : `float(Decimal("0.0123456789"))` peut arrondir. Acceptable pour DB mais log preserve Decimal stringifié si audit.
13. **Snapshot PnL pendant résolution** : un snapshot pris pendant le polling résolution peut manquer un realized_pnl juste updated. Acceptable (snapshots 5 min, pas high-frequency).
14. **Alembic 0004 drop/recreate UniqueConstraint** : SQLite doit utiliser `batch_alter_table` pour recréer la table. Test migration obligatoire.
15. **Migration data safety** : les lignes `my_positions` pré-M8 ont `simulated=False` par default → contrainte unique triple encore satisfaite.

### 14.5 Zones d'incertitude à lever AVANT implémentation

1. **Schéma `/book` exact** : la fixture `clob_orderbook_sample.json` est **obligatoire** avant code. Si le schéma diverge (ex: `price` renvoyé en nombre, pas string ; ou structure différente), ajuster DTOs. **STOP** si divergence majeure → signaler à l'utilisateur.

2. **Rate limit `/book`** : non documenté. Pic pessimiste : 20 wallets × 10 ordres/jour × cache miss 50 % = 100 calls/jour = ~0.07 req/min. Safe. **Mais** : sur marché news-driven avec 5 wallets qui tradent en même temps sur 3 marchés différents → pic 15 calls/min. Toujours safe vs ~100 req/min CLOB. Si 429 observé → ajouter backoff + doc.

3. **Résolution neg_risk** : l'identification de "winning outcome" sur un marché neg_risk (multi-outcome avec probabilité de "all no" payoff) demande une query séparée `/neg-risk-markets/<id>`. v1 M8 **skip**. À clarifier post-v1 si assez de positions neg_risk s'accumulent pour justifier.

4. **`MyOrder.price` = avg_fill_price ou requested_price ?** : v1 décide `avg_fill_price`. Le `requested_price` (source trade × `_round_price_to_tick`) est perdu en DB mais accessible via structlog. Si un user veut comparer → reportable `MyOrder.requested_price` M8.1.

5. **SELL virtual** : logique "close virtual position partielle / totale" vs "short synthétique sur outcome opposé". v1 **skip** avec warning. À décider si feedback.

6. **`VirtualWalletStateReader` fees/slippage** : le PnL virtuel n'inclut pas les frais de transaction (Polymarket prend un %age lors du settlement). Le realized_pnl est donc **optimiste**. Documenter. Modéliser les frais : reportable M8.1.

7. **Dashboard mode filter `?mode=both`** : 2 courbes sur même graph → lisibilité ? v1 : superposer avec opacité réduite + légendes. À valider visuellement, possiblement reportable M8.1 (courbes séparées stackées).

8. **Test integration live `/book`** : nécessite un token_id public actif. Capturer dynamiquement au test time via Gamma `top_markets` ? Ou hardcoder un token stable ? **Décision** : dynamique — le test fetch un top market Gamma avant d'appeler `/book`.

9. **`DRY_RUN_RESOLUTION_POLL_MINUTES=30` trop lent pour tests** : override `DRY_RUN_RESOLUTION_POLL_MINUTES=1` via env en dev. Documenté.

10. **Race condition `DryRunResolutionWatcher` + `_persist_realistic_simulated`** : 1 position créée pendant qu'un cycle de résolution tourne → cycle suivant la voit. Pas de race fatale. Testé.

11. **Alembic 0004 rollback safety** : drop + recreate `my_positions` unique constraint. Si un user a déjà des data M8 et roll back → perte de `simulated`/`realized_pnl`. Documenter "rollback non supporté si data M8 présente".

12. **Volume `snapshots dry-run` vs `snapshots real`** : mix en DB, dashboard doit segregate. Requis de ne pas mélanger en `queries.get_pnl_chart_data` — test §9.11 garde.

---

## 15. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M8

Suis specs/M8-dry-run-realistic.md à la lettre. Avant tout code, action obligatoire :

1. Invoque /polymarket:polymarket pour reconfirmer le schéma exact de GET /book?token_id=<id>. Si le skill ne couvre pas → vérifier via https://docs.polymarket.com (endpoint CLOB read-only) ou capture directe.

2. Capture 1 fixture réelle :
   curl 'https://clob.polymarket.com/book?token_id=<token_id_actif_liquide>' > tests/fixtures/clob_orderbook_sample.json
   Choix du token : via curl Gamma `?limit=1&order=liquidityNum&ascending=false&active=true&closed=false` puis parser `clobTokenIds` (JSON-stringified, M2 piège). Prendre le premier tokenId.

   Si le schéma diverge significativement des assumptions §4.3 (ex: `price` number au lieu de string, clés différentes), STOP et signale-moi avant de continuer.

Ensuite suis l'ordre §14.1.

Contraintes non négociables :

- DRY_RUN_REALISTIC_FILL=false par défaut (opt-in strict). Backwards compat M3/M4/M7 : user qui ne touche pas son .env → comportement identique M3 (fill stub instantané).
- M8 est un DIFF ADDITIF sur M3. Zéro ligne modifiée dans le path live (ClobWriteClient, create_and_post_order, _persist_sent_order, _assert_capital_available). Tests M3 passent sans diff.
- Triple garde-fou M3 préservé intact (lazy init write client, RuntimeError boot, assert dry_run is False avant post). 4ᵉ garde-fou M8 : assert dry_run is True avant chaque _persist_realistic_simulated. Test breakglass.
- Kill switch JAMAIS en dry-run (M4 invariant). M8 ajoute alerte `dry_run_virtual_drawdown` INFO level — pas WARNING, pas CRITICAL. Aucun stop_event.set() depuis le path dry-run.
- Aucune creds consommée par le chemin M8 : pas de L1, L2, py-clob-client. Uniquement httpx GET /book (read-only public), /midpoint (M2), Gamma /markets (M2). Aucune signature.
- Ségrégation data : MyOrder.realistic_fill + MyPosition.simulated + contrainte unique triple (condition_id, asset_id, simulated). Tests de séparation real/virtual.
- FOK strict par défaut (cohérent M3 live). DRY_RUN_ALLOW_PARTIAL_BOOK=true opt-in.
- v1 BUY virtual only. SELL sur position inexistante → skip + log warning (pas de crash). Documenté §14.5.
- v1 marchés neg_risk : skip résolution + warning. Position reste open virtuellement. Documenté §14.5.
- Cache book in-memory TTL 5s + cap 500 entries LRU. Testé.
- Decimal pour les calculs orderbook, float pour la persistance. Pas de `Decimal(float)`.
- DryRunResolutionWatcher lancé conditionnellement depuis ExecutorOrchestrator (pas un nouveau top-level module).
- VirtualWalletStateReader alimente PnlSnapshotWriter M4 sans refactor M4.
- Migration Alembic 0004 : batch_alter_table SQLite-friendly pour recréer unique constraint. Audit manuel obligatoire.
- Aucun secret loggé (grep automatisé test) : POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER, L2 creds, TELEGRAM_BOT_TOKEN, GOLDSKY_API_KEY.
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print).
- mypy --strict propre, ruff propre, pytest ≥ 80% coverage sur src/polycopy/executor/ + storage/. Non-régression M1..M7 ≥ 80%.
- Tests via respx (mock /book), pytest-asyncio, property test hypothesis sur simulate_fill.
- Doc updates §10 dans le même commit (README + architecture + CLAUDE + setup §17).
- Commit final unique : feat(executor,storage): M8 dry-run realistic fill + virtual PnL

Demande-moi confirmation avant tout patch sensible :
- alembic/versions/0004_*.py (audit manuel batch_alter_table + contrainte unique triple).
- config.py (5 env vars + validators).
- Changement path live executor (interdit — M8 est additif).
- Refactor PnlSnapshotWriter M4 (préférer injection conditionnelle reader).

Si une zone §14.5 se confirme problématique pendant l'implémentation (ex: /book schéma diverge, neg_risk résolution nécessaire, rate limit 429 observé, fees non négligeables), STOP et signale — ne tranche pas au pif.

Smoke test réel obligatoire avant merge : 1h avec DRY_RUN=true DRY_RUN_REALISTIC_FILL=true + 1 pinned wallet actif. Observer : order_realistic_fill_simulated avec avg_fill_price réel, PnlSnapshot is_dry_run=true avec total_usdc bougeant au fil du temps. Screenshot des logs + rapport HTML joint à la PR.
```
