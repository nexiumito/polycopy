# M2 — Strategy Engine

Spec d'implémentation du Module 2 de polycopy : consommer les `DetectedTrade` produits par M1, passer chaque trade dans un pipeline de filtres (marché, sizing, slippage, risk), et émettre une décision `OrderApproved` ou `OrderRejected(reason)` persistée pour audit. **Aucun ordre envoyé à M2** (Executor = M3).

Source de vérité fonctionnelle : `docs/architecture.md` section "Strategy". Conventions : `CLAUDE.md`. Schémas API : skill `/polymarket:polymarket` + https://docs.polymarket.com (Gamma OpenAPI : `https://docs.polymarket.com/api-spec/gamma-openapi.yaml`). Template structurel : `specs/M1-watcher-storage.md`.

---

## 0. Pré-requis environnement

### 0.1 Bootstrap (déjà fait)

`bash scripts/setup.sh` (idempotent, déjà existant). Aucun nouveau patch config requis. Tous les seuils M2 vivent déjà dans `src/polycopy/config.py` :

| Variable env | Champ Settings | Default | Lu par |
|---|---|---|---|
| `COPY_RATIO` | `copy_ratio` | 0.01 | PositionSizer |
| `MAX_POSITION_USD` | `max_position_usd` | 100 | PositionSizer |
| `MIN_MARKET_LIQUIDITY_USD` | `min_market_liquidity_usd` | 5000 | MarketFilter |
| `MIN_HOURS_TO_EXPIRY` | `min_hours_to_expiry` | 24 | MarketFilter |
| `MAX_SLIPPAGE_PCT` | `max_slippage_pct` | 2.0 | SlippageChecker |
| `KILL_SWITCH_DRAWDOWN_PCT` | `kill_switch_drawdown_pct` | 20 | RiskManager |

### 0.2 Nouvelle env var (optionnelle)

À ajouter à `config.py` **et** `.env.example` car le RiskManager M2 a besoin d'un capital de référence avant que l'Executor M3 ne sache lire le wallet on-chain :

| Variable env | Champ Settings | Default | Justification |
|---|---|---|---|
| `RISK_AVAILABLE_CAPITAL_USD_STUB` | `risk_available_capital_usd_stub` | 1000.0 | Stub M2 pour le calcul d'exposition ; remplacé par lecture wallet à M3. |

**Justification du default 1000 USD** : valeur conservative pour un test perso ; force le RiskManager à rejeter les trades qui dépasseraient `MAX_POSITION_USD * 5` cumulés, ce qui matche un scenario "petit capital" plausible pour M2.

### 0.3 Skill Polymarket (déjà installé)

Source de vérité pour Gamma + CLOB. Invocation : `/polymarket:polymarket`. Si réinstall :

```
/plugin marketplace add atompilot/polymarket-skill
/plugin install polymarket@atompilot-polymarket-skill
```

### 0.4 Patches M1 (déjà appliqués)

- §0.5 M1 (`polymarket_private_key`/`funder` optionnels) : **déjà appliqué**.
- Validator CSV `TARGET_WALLETS` (M1 §0.4) : **déjà appliqué**.
- Cleanup `{src/` (M1 §0.7) : **déjà appliqué**.

### 0.5 Critère de validation "environnement"

```bash
python -m polycopy --dry-run
```

Doit continuer à logger `polycopy_starting`, `db_initialized`, `watcher_started`, et après M2, `strategy_started`. Sortie code 0 sur SIGINT.

---

## 1. Objectif M2 (scope exact)

Consommer en quasi-temps-réel les `DetectedTrade` produits par M1 et émettre, pour chacun, une décision `OrderApproved` ou `OrderRejected(reason)` :

- **Persistée** dans une nouvelle table `strategy_decisions` (audit complet pour M3 et debug humain).
- **Loggée** via `structlog` (`order_approved` ou `order_rejected` avec bindings `condition_id`, `tx_hash`, `reason`).
- **Si APPROVED**, push sur une `asyncio.Queue` `approved_orders_queue` consommée par l'Executor à M3 (à M2, queue créée mais sans consumer → DropConsumer no-op).

**Livrable fonctionnel** : `python -m polycopy --dry-run` lance watcher + strategy en parallèle. Quand un wallet copié trade, le watcher persiste le `DetectedTrade` puis push sur la queue ; la strategy le pull, query Gamma + CLOB, applique le pipeline, persiste la décision, log.

**Hors livrable M2** : signature CLOB, envoi d'ordre, alerte Telegram, dashboard PnL, scoring. `settings.dry_run` n'a aucun effet sur la strategy à M2 (la strategy est read-only — aucun side-effect monétaire). Le mode dry-run reste un garde-fou pour M3.

---

## 2. Couplage watcher ↔ strategy

**Choix retenu : `asyncio.Queue` partagée**, instanciée dans `__main__._run()` et injectée dans `WatcherOrchestrator` ET `StrategyOrchestrator`.

```
WalletPoller.insert_if_new=True
        │
        ▼
detected_trades_queue.put_nowait(DetectedTradeDTO)
        │
        ▼
StrategyOrchestrator: trade = await detected_trades_queue.get()
        │
        ▼ pipeline
        ▼
approved_orders_queue.put_nowait(OrderApproved)   # consumer M3
StrategyDecisionRepository.insert(decision)
```

**Alternative écartée** : polling DB (flag `processed: bool` sur `DetectedTrade`).

**Justification** :

- **Latence** : queue = 0 ms vs polling = jusqu'à `STRATEGY_POLL_INTERVAL` ms. La cible architecture (≤ 15 s détection→exécution) impose le couplage direct.
- **Pas de migration de schéma** : ajouter une colonne sur `detected_trades` impose une migration Alembic (hors scope M1/M2).
- **Couplage faible préservé** : la queue est typée `asyncio.Queue[DetectedTradeDTO]`, le watcher ne connaît pas la strategy. L'orchestrateur principal (`__main__`) est le seul à connaître les deux.
- **Resync au boot** : si la strategy crash et redémarre, elle perd les trades en vol. Acceptable à M2 (logs structlog permettent le rejeu manuel). À M3 on réintroduira une reprise depuis `detected_trades` non joints à `strategy_decisions` si besoin.

**Backpressure** : `asyncio.Queue(maxsize=1000)` (large mais borné). Si saturée (strategy lente / Gamma down), `WalletPoller` log `strategy_queue_full` (warning) et drop le trade en queue. Le trade reste persisté en DB → rejouable à la main si nécessaire.

---

## 3. Modèles de données + repository + DTOs internes

### 3.1 `StrategyDecision` (peuplé à M2) — `src/polycopy/storage/models.py`

| Colonne | Type | Contrainte |
|---|---|---|
| `id` | `int` | PK autoincrement |
| `detected_trade_id` | `int` | FK logique vers `detected_trades.id`, **indexed** |
| `tx_hash` | `str(66)` | **indexed** (lookup direct) |
| `decision` | `str(8)` | `"APPROVED"` ou `"REJECTED"` |
| `reason` | `str(64) \| None` | rempli si `REJECTED` (ex: `"liquidity_too_low"`, `"slippage_exceeded"`) |
| `my_size` | `float \| None` | taille calculée par PositionSizer (None si rejet avant sizer) |
| `my_price` | `float \| None` | prix mid CLOB au moment de la décision (None si rejet avant slippage check) |
| `slippage_pct` | `float \| None` | slippage calculé (None si rejet avant slippage check) |
| `decided_at` | `datetime` | default `now(UTC)`, timezone-aware, **indexed** |
| `pipeline_state` | `JSON` | snapshot complet du `PipelineContext` (filtre par filtre) pour audit |

Ajouter dans `models.py` à côté de `DetectedTrade`. Pas de modif aux tables M1.

### 3.2 `StrategyDecisionRepository` — `src/polycopy/storage/repositories.py`

```python
class StrategyDecisionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    async def insert(self, decision: StrategyDecisionDTO) -> StrategyDecision: ...
    async def list_recent(self, limit: int = 100) -> list[StrategyDecision]: ...   # debug
    async def count_by_decision(self) -> dict[str, int]: ...                        # metrics
```

Style SQLAlchemy 2.0 : `select()`, async_sessionmaker. Pas de `update` (les décisions sont immuables — append-only).

### 3.3 DTOs Pydantic — `src/polycopy/strategy/dtos.py`

```python
class MarketMetadata(BaseModel):
    """Sous-ensemble Gamma /markets utile au pipeline."""
    model_config = ConfigDict(populate_by_name=True, extra="allow", frozen=True)

    market_id: str = Field(alias="id")
    condition_id: str = Field(alias="conditionId")
    question: str | None = None
    slug: str | None = None
    active: bool
    closed: bool
    archived: bool
    accepting_orders: bool = Field(alias="acceptingOrders")
    enable_order_book: bool = Field(alias="enableOrderBook")
    liquidity_clob: float | None = Field(default=None, alias="liquidityClob")
    end_date_iso: str | None = Field(default=None, alias="endDateIso")  # "YYYY-MM-DD" ou ISO datetime
    end_date: datetime | None = Field(default=None, alias="endDate")    # ISO 8601 avec Z
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    outcomes: list[str] = Field(default_factory=list)

    @field_validator("clob_token_ids", "outcomes", mode="before")
    @classmethod
    def _parse_json_string(cls, v: object) -> object:
        # Gamma renvoie '["123","456"]' (string JSON-stringifiée), à parser.
        if isinstance(v, str):
            return json.loads(v)
        return v


class StrategyDecisionDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    detected_trade_id: int
    tx_hash: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str | None = None
    my_size: float | None = None
    my_price: float | None = None
    slippage_pct: float | None = None
    pipeline_state: dict[str, Any]


class OrderApproved(BaseModel):
    """Event poussé sur approved_orders_queue (consommé par M3)."""
    model_config = ConfigDict(frozen=True)

    detected_trade_id: int
    tx_hash: str
    condition_id: str
    asset_id: str
    side: Literal["BUY", "SELL"]
    my_size: float
    my_price: float


@dataclass
class PipelineContext:
    """État partagé entre filtres dans un même run de pipeline."""
    trade: DetectedTradeDTO       # input M1 (re-récupéré depuis DB ou passé par queue)
    market: MarketMetadata | None = None
    midpoint: float | None = None
    my_size: float | None = None
    slippage_pct: float | None = None
    rejection_reason: str | None = None

    def to_audit_dict(self) -> dict[str, Any]:
        """Snapshot sérialisable pour `pipeline_state`."""
```

`StrategyDecisionRepository.insert` prend un `StrategyDecisionDTO`, mappe vers `StrategyDecision`. Idem pattern `DetectedTradeRepository`.

### 3.4 Bootstrap DB

`init_db.py` (M1) crée déjà toutes les tables via `Base.metadata.create_all`. Ajouter `StrategyDecision` à `models.py` suffit ; **pas de code à modifier dans `init_db.py`**.

Note : `# TODO M2+: introduire Alembic` reste en place — toujours pas requis à M2 (schéma append-only, pas de modif backwards-incompatible).

---

## 4. DTOs internes (déjà décrits §3.3)

Section regroupée avec §3 pour cohérence (un DTO par responsabilité, repo et model adjacents).

---

## 5. Gamma API client — `src/polycopy/strategy/gamma_client.py`

### 5.1 Endpoint (verrouillé via skill + Gamma OpenAPI + capture réelle)

- **Base URL** : `https://gamma-api.polymarket.com`
- **Route** : `GET /markets`
- **Auth** : aucune
- **Rate limit** : non documenté ; CLAUDE.md retient ~50 req/min par prudence (Gamma est plus lent que Data API). À M2, cache TTL 60s tue 99% des appels répétés.

### 5.2 Query params (noms et casse exacts)

| Param | Type | Défaut | À utiliser à M2 |
|---|---|---|---|
| `condition_ids` | `array[string]` (CSV en query) | — | **`condition_ids=<conditionId>`**, 1 condition_id par appel à M2 (batch possible plus tard) |
| `limit` | int | (non documenté, ~100) | non passé (1 résultat attendu) |
| `offset` | int | 0 | non passé |
| `closed` | bool | false | non passé (on accepte toutes les variantes, le filtre vit dans `MarketFilter`) |
| `active`, `archived` | bool | — | non passés (idem) |

Les autres params (`liquidity_num_min`, `tag_id`, `slug`, `clob_token_ids`, etc.) ne sont pas utilisés à M2.

### 5.3 Schéma de réponse (capture réelle dans `tests/fixtures/gamma_market_sample.json`)

Réponse = `array<Market>`. Sous-ensemble exploité par M2 (snake_case = nom Python, `→` = nom JSON exact) :

```yaml
Market:
  id: string                              # "553826"
  conditionId: string                     # "0x4a67..."
  questionID: string                      # "0x7faa..." (note: pas "questionId" — case spécifique)
  question: string | null                 # "Will the Edmonton Oilers win the 2026 NHL Stanley Cup?"
  slug: string | null
  active: boolean | null                  # true si tradable
  closed: boolean | null                  # true si marché clos (pre-résolution ou résolu)
  archived: boolean | null
  restricted: boolean | null              # true = restreint (ex: sports US)
  acceptingOrders: boolean | null         # true si l'orderbook accepte de nouveaux ordres
  enableOrderBook: boolean | null         # true si CLOB activé
  liquidity: string | null                # "87432.6499"
  liquidityNum: number | null             # 87432.6499 (USD total)
  liquidityClob: number | null            # 87432.6499 (USD côté CLOB)
  volumeNum: number | null                # 538654.50
  volume24hr: number | null               # 30379.42
  endDate: string (date-time) | null      # "2026-06-30T00:00:00Z"
  endDateIso: string | null               # "2026-06-30"
  startDate: string (date-time) | null
  closedTime: string | null               # rempli quand closed=true
  clobTokenIds: string | null             # '["269949...","123456..."]' (STRING JSON-stringifié)
  outcomes: string | null                 # '["Yes","No"]' (STRING JSON-stringifié)
  outcomePrices: string | null            # '["0.08","0.92"]' (STRING JSON-stringifié)
  bestBid: number | null                  # 0.07
  bestAsk: number | null                  # 0.09
  lastTradePrice: number | null
  spread: number | null
```

**Pièges importants** :

1. `clobTokenIds`, `outcomes`, `outcomePrices` sont des **strings JSON-stringifiées**, pas des arrays JSON. Le DTO doit `json.loads()` côté client (validator `mode="before"`).
2. `questionID` (et non `questionId`) — case spécifique côté Gamma.
3. `closedTime` n'est pas rempli si `closed=false`. Aucun champ `resolved` direct ; déduire via `closed=true && closedTime != None`.
4. Plusieurs champs "non documentés dans l'OpenAPI" arrivent en pratique (`approved`, `negRisk`, `negRiskMarketID`, `feeType`, etc.). `extra="allow"` les absorbe.

### 5.4 DTO Pydantic

Voir §3.3 (`MarketMetadata`).

### 5.5 Client

```python
class GammaApiClient:
    BASE_URL = "https://gamma-api.polymarket.com"
    DEFAULT_TIMEOUT = 10.0
    CACHE_TTL_SECONDS = 60

    def __init__(self, http_client: httpx.AsyncClient) -> None: ...

    async def get_market(self, condition_id: str) -> MarketMetadata | None:
        """Retourne le marché ou None si Gamma ne le trouve pas (array vide)."""
```

- Cache TTL 60s en mémoire : `dict[condition_id, tuple[datetime, MarketMetadata]]`. Lookup avant fetch ; si entry présente et `now - ts < 60s`, retourner. Sinon fetch, stocker, retourner.
- Cache invalidé paresseusement (pas de tâche de fond ; expiration vérifiée à chaque appel).
- Tenacity sur la méthode `_fetch` privée :
  ```python
  @retry(
      retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
      wait=wait_exponential(multiplier=1, min=1, max=30),
      stop=stop_after_attempt(5),
      before_sleep=before_sleep_log(_log, logging.WARNING),
      reraise=True,
  )
  ```
- Sur 429 : respect `Retry-After`, sinon backoff.
- Si la réponse est `[]` (condition_id inconnu de Gamma) → retour `None`. Le `MarketFilter` traite comme rejet `market_not_found`.

### 5.6 Garde-fou rate limit

Au boot du `StrategyOrchestrator`, log `gamma_cache_ready` avec `cache_ttl_seconds=60`. Pas de calcul de risque automatique à M2 (volume bas).

---

## 6. CLOB read client — `src/polycopy/strategy/clob_read_client.py`

### 6.1 Endpoint (verrouillé via doc + capture réelle)

- **Base URL** : `https://clob.polymarket.com`
- **Route** : `GET /midpoint`
- **Auth** : aucune
- **Rate limit** : non documenté ; CLAUDE.md retient ~100 req/min côté CLOB read.

### 6.2 Query params

| Param | Type | Défaut | À utiliser à M2 |
|---|---|---|---|
| `token_id` | string (ERC1155) | — | **obligatoire**, c'est le `asset` du `DetectedTrade` |

### 6.3 Schéma de réponse (capture réelle dans `tests/fixtures/clob_midpoint_sample.json`)

```json
{"mid": "0.08"}
```

**Piège** : la doc OpenAPI publie `{"mid_price": "..."}` — c'est faux. La vraie réponse en prod est `{"mid": "..."}`. Le DTO doit utiliser `mid`. La capture fixture est la source de vérité.

`mid` est une **string** (pas un float) — à parser via `float(value)`.

Erreurs : `400` token_id invalide, `404` "No orderbook exists for the requested token id" (marché sans liquidité — traité comme rejet `no_orderbook`).

### 6.4 DTO

Pas de DTO Pydantic pour cette réponse mono-champ. Méthode retourne directement `float`.

### 6.5 Choix d'implémentation : httpx direct vs `py-clob-client`

**Recommandation : httpx direct.** Justifications :

- Cohérence avec `DataApiClient` et `GammaApiClient` (même pattern tenacity, même gestion 429).
- Pas besoin du SDK pour 1 endpoint read sans auth ; instancier `ClobClient` charge tout le code de signature inutile à M2.
- Réponse mono-champ ; mapping httpx → float = 5 lignes.
- `py-clob-client.get_midpoint(token_id)` retourne probablement la même structure ; on n'évite rien en l'utilisant.

À M3, l'Executor utilisera `py-clob-client` pour la signature L1/L2 et le post d'ordre — c'est là que la dep prend son sens.

### 6.6 Client

```python
class ClobReadClient:
    BASE_URL = "https://clob.polymarket.com"
    DEFAULT_TIMEOUT = 5.0  # plus court que Gamma : prix temps réel

    def __init__(self, http_client: httpx.AsyncClient) -> None: ...

    async def get_midpoint(self, token_id: str) -> float | None:
        """Retourne le mid courant ou None si 404 (no_orderbook)."""
```

- Pas de cache (prix temps réel → toujours fetch).
- Tenacity même config que `GammaApiClient`.
- Sur `404` : retour `None` (pas une erreur, juste pas d'orderbook → `SlippageChecker` rejette `no_orderbook`).

---

## 7. Pipeline de filtres — `src/polycopy/strategy/pipeline.py`

Chaque filtre est une classe avec une méthode `async check(ctx: PipelineContext) -> FilterResult`. `FilterResult` est un Literal type :

```python
@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str | None = None  # rempli si passed=False
```

Le pipeline est une simple liste exécutée en séquence. Premier rejet → arrêt, retour `OrderRejected(reason)`. Tous OK → `OrderApproved`.

### 7.1 `MarketFilter`

Inputs : `ctx.trade.condition_id`, `gamma_client`, `settings`.

Étapes :

1. `market = await gamma_client.get_market(ctx.trade.condition_id)`. Si `None` → `FAIL("market_not_found")`.
2. `ctx.market = market`.
3. `if not market.active`: → `FAIL("market_inactive")`.
4. `if market.closed or market.archived`: → `FAIL("market_closed")`.
5. `if not market.accepting_orders or not market.enable_order_book`: → `FAIL("orderbook_disabled")`.
6. `if (market.liquidity_clob or 0) < settings.min_market_liquidity_usd`: → `FAIL("liquidity_too_low")`.
7. Calcul `hours_to_expiry` :
   - Si `market.end_date` (datetime aware) → `(end_date - now()) / 1h`.
   - Sinon, parser `end_date_iso` (`"YYYY-MM-DD"` → datetime UTC fin de journée).
   - Sinon → ignorer (certains marchés perpétuels). Log `expiry_unknown` debug.
8. `if hours_to_expiry < settings.min_hours_to_expiry`: → `FAIL("expiry_too_close")`.
9. → `PASS`.

### 7.2 `PositionSizer`

Inputs : `ctx.trade`, `ctx.market`, `settings`, `session_factory` (pour lire `MyPosition`).

Étapes :

1. Vérifier qu'on n'a pas déjà une position ouverte sur le même `condition_id` :
   - Query `select(MyPosition).where(condition_id=..., closed_at=None)` via le session_factory.
   - Si trouvé → `FAIL("position_already_open")`.
   - **Note M2** : `MyPosition` n'est jamais peuplé avant M3, donc cette query retourne toujours `None`. C'est OK ; on prépare la place.
2. `raw_size = ctx.trade.size * settings.copy_ratio`.
3. `cap_size = settings.max_position_usd / ctx.trade.price`.
4. `ctx.my_size = min(raw_size, cap_size)`.
5. `if ctx.my_size <= 0`: → `FAIL("size_zero")` (ne devrait pas arriver, garde-fou).
6. → `PASS`.

### 7.3 `SlippageChecker`

Inputs : `ctx.trade`, `clob_client`, `settings`.

Étapes :

1. `mid = await clob_client.get_midpoint(ctx.trade.asset_id)`. Si `None` → `FAIL("no_orderbook")`.
2. `ctx.midpoint = mid`.
3. `slippage = abs(mid - ctx.trade.price) / ctx.trade.price`. (price source toujours > 0 — Polymarket.)
4. `ctx.slippage_pct = slippage * 100`.
5. `if ctx.slippage_pct > settings.max_slippage_pct`: → `FAIL("slippage_exceeded")`.
6. `ctx.my_price = mid`. (le PositionSizer ne fixe pas le prix d'exécution — c'est le mid courant.)
7. → `PASS`.

### 7.4 `RiskManager`

Inputs : `ctx`, `settings`, `session_factory` (lecture `MyPosition` + `PnlSnapshot` pour drawdown).

Étapes :

1. `available_capital = settings.risk_available_capital_usd_stub` (M2 stub ; M3 lira le wallet).
2. `current_exposure = sum(p.size * p.avg_price for p in MyPosition.all where closed_at is None)`. À M2 → 0 (table vide).
3. `prospective_cost = ctx.my_size * ctx.my_price`.
4. `if (current_exposure + prospective_cost) > available_capital`: → `FAIL("capital_exceeded")`.
5. Drawdown : lire le dernier `PnlSnapshot.drawdown_pct`. À M2 → table vide → drawdown = 0%. Si `> settings.kill_switch_drawdown_pct`: → `FAIL("kill_switch_triggered")`.
6. → `PASS`.

### 7.5 Orchestration du pipeline

```python
PIPELINE: list[type[Filter]] = [MarketFilter, PositionSizer, SlippageChecker, RiskManager]

async def run_pipeline(
    trade: DetectedTradeDTO,
    *,
    gamma_client: GammaApiClient,
    clob_client: ClobReadClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> tuple[Literal["APPROVED", "REJECTED"], str | None, PipelineContext]:
    ctx = PipelineContext(trade=trade)
    for FilterCls in PIPELINE:
        f = FilterCls(...)  # injection des deps
        result = await f.check(ctx)
        if not result.passed:
            return "REJECTED", result.reason, ctx
    return "APPROVED", None, ctx
```

Pas d'abstraction `AbstractFilter` partagée à M2 (rule of three, cf. CLAUDE.md). Une fonction `run_pipeline` qui itère suffit.

---

## 8. Orchestrateur strategy — `src/polycopy/strategy/orchestrator.py`

```python
class StrategyOrchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        detected_trades_queue: asyncio.Queue[DetectedTradeDTO],
        approved_orders_queue: asyncio.Queue[OrderApproved],
    ) -> None: ...

    async def run_forever(self, stop_event: asyncio.Event) -> None: ...
```

`run_forever` :

1. Construit `httpx.AsyncClient` partagé, `GammaApiClient`, `ClobReadClient`, `StrategyDecisionRepository`.
2. Log `strategy_started` (binding : `pipeline_steps=[MarketFilter, PositionSizer, SlippageChecker, RiskManager]`).
3. Boucle `while not stop_event.is_set()` :
   - `trade = await asyncio.wait_for(detected_trades_queue.get(), timeout=1.0)` (poll interruptible).
   - Sur `TimeoutError` : continue (vérifie stop_event).
   - Sur `asyncio.CancelledError` : re-raise.
   - Sur succès : appelle `run_pipeline`, persiste la `StrategyDecision`, log `order_approved` ou `order_rejected`, push `OrderApproved` sur `approved_orders_queue` si APPROVED.
   - Sur exception non gérée du pipeline : log `pipeline_error` (error), persiste une `StrategyDecision` REJECTED reason=`pipeline_error`, ne crash pas.
4. Sur sortie : `await http_client.aclose()`, log `strategy_stopped`.

**Pas de signal handler ici** : le `stop_event` est partagé avec `WatcherOrchestrator`, géré par `__main__` (factorisation §9).

**Backpressure approved_orders_queue** : si `put_nowait` lève `QueueFull`, log `executor_queue_full` warning et continue. (M3 sera responsable de drainer.)

---

## 9. Intégration `__main__` — `src/polycopy/__main__.py`

Refactor de `_run()` :

1. Init logging, init engine + session_factory (inchangé).
2. `init_db(engine, session_factory, settings.target_wallets)` (inchangé).
3. Créer 2 queues :
   ```python
   detected_trades_queue: asyncio.Queue[DetectedTradeDTO] = asyncio.Queue(maxsize=1000)
   approved_orders_queue: asyncio.Queue[OrderApproved] = asyncio.Queue(maxsize=1000)
   ```
4. Créer `stop_event = asyncio.Event()` partagé.
5. Installer signal handlers SIGINT/SIGTERM globaux qui set `stop_event` (déplacer le code actuel de `WatcherOrchestrator._install_signal_handlers` vers un helper dans `__main__` ou un module `polycopy/signals.py` partagé).
6. Instancier `WatcherOrchestrator(session_factory, settings, detected_trades_queue, stop_event)` — refactor de la signature actuelle pour injecter queue + stop_event.
7. Instancier `StrategyOrchestrator(session_factory, settings, detected_trades_queue, approved_orders_queue)`.
8. Lancer les 2 dans un `asyncio.TaskGroup` :
   ```python
   async with asyncio.TaskGroup() as tg:
       tg.create_task(watcher.run_forever(stop_event))
       tg.create_task(strategy.run_forever(stop_event))
   ```
9. `finally: await engine.dispose()` (inchangé).

**Refactor associé sur `WalletPoller`** :

- Constructeur prend une `asyncio.Queue[DetectedTradeDTO] | None` optionnelle (None = comportement M1, push désactivé — pour rétrocompatibilité tests M1).
- Après `inserted = await repo.insert_if_new(dto)`, si `inserted is True` et `queue is not None`, `queue.put_nowait(dto)`. Sur `asyncio.QueueFull`, log `strategy_queue_full` warning, ne crash pas.

---

## 10. Tests

### 10.1 Arborescence

```
tests/
├── fixtures/
│   ├── activity_sample.json            # M1 (existe)
│   ├── gamma_market_sample.json        # M2 (capturé)
│   └── clob_midpoint_sample.json       # M2 (capturé)
├── unit/
│   ├── test_config.py                  # M1, à étendre pour risk_available_capital_usd_stub
│   ├── ... (tests M1 existants)
│   ├── test_strategy_dtos.py
│   ├── test_strategy_repository.py
│   ├── test_gamma_client.py
│   ├── test_clob_read_client.py
│   ├── test_strategy_pipeline.py
│   └── test_strategy_orchestrator.py
└── integration/
    ├── test_data_api_live.py           # M1 (existe)
    ├── test_gamma_live.py              # M2
    └── test_clob_midpoint_live.py      # M2
```

### 10.2 `conftest.py` (extension)

Ajouter fixtures :

- `sample_gamma_market` : lit `fixtures/gamma_market_sample.json`, retourne le 1er item.
- `sample_clob_midpoint` : `{"mid": "0.08"}` (constante ou lit la fixture).
- `strategy_decision_repo` : dérivé de `session_factory`.

Réutiliser `async_engine`, `session_factory`, `detected_trade_repo`, `target_trader_repo` existants.

### 10.3 `test_config.py` (extension)

Ajouter :

- `risk_available_capital_usd_stub` default = 1000.0.
- Override via env `RISK_AVAILABLE_CAPITAL_USD_STUB=2500` → settings = 2500.0.

### 10.4 `test_strategy_dtos.py`

- `MarketMetadata.model_validate(sample_gamma_market)` parse correctement.
- `clob_token_ids` = liste de strings (parsé depuis le JSON-string).
- `outcomes` = `["Yes", "No"]`.
- `end_date` parse en `datetime` aware UTC.
- `OrderApproved` est frozen.

### 10.5 `test_strategy_repository.py`

- `StrategyDecisionRepository.insert` persiste, retourne l'instance avec `id`.
- `list_recent` ordonné par `decided_at DESC`.
- `count_by_decision` retourne `{"APPROVED": N, "REJECTED": M}`.

### 10.6 `test_gamma_client.py` (respx)

- Happy path : 1 marché renvoyé par Gamma → DTO valide.
- `condition_ids` bien passé en query string.
- Cache : 2 appels successifs au même `condition_id` → 1 seule requête HTTP. Avancer le temps via `freezegun` ou monkeypatch `_now()` interne ; au-delà de 60s → 2e fetch.
- Réponse `[]` (condition_id inconnu) → retour `None`.
- 429 puis 200 → tenacity retry. Idem `TransportError`.
- Parsing fixture réelle : `MarketMetadata` validé sur le payload entier.

### 10.7 `test_clob_read_client.py` (respx)

- Happy path : `{"mid": "0.08"}` → retourne `0.08` (float).
- 404 → retourne `None`, pas d'exception.
- 429 puis 200 → retry.

### 10.8 `test_strategy_pipeline.py`

Un test par filtre (chaque chemin REJECT + le PASS) + un test bout-en-bout :

- `MarketFilter` : market_not_found, market_inactive, market_closed, orderbook_disabled, liquidity_too_low, expiry_too_close, PASS.
- `PositionSizer` : position_already_open (mock `MyPosition` non vide via insert direct), sizing nominal, capping `MAX_POSITION_USD`.
- `SlippageChecker` : no_orderbook, slippage_exceeded, PASS.
- `RiskManager` : capital_exceeded (override `risk_available_capital_usd_stub` à 1.0), PASS.
- Bout-en-bout : trade synthétique + Gamma mock + CLOB mock → `OrderApproved` retourné, `ctx.my_size`, `ctx.my_price`, `ctx.slippage_pct` tous remplis.

Tenacity sleep désactivé via fixture autouse (cf. `tests/unit/test_data_api_client.py` M1).

### 10.9 `test_strategy_orchestrator.py`

- Push 1 trade sur `detected_trades_queue` → orchestrator pull, exécute pipeline (mocks Gamma+CLOB), persiste `StrategyDecision`, push sur `approved_orders_queue`. Stop event → sortie propre.
- Pipeline exception interne → `StrategyDecision` REJECTED reason=`pipeline_error`, log error, ne crash pas l'orchestrator.
- Backpressure `approved_orders_queue` plein → log warning `executor_queue_full`, ne crash pas.

### 10.10 Tests live (`@pytest.mark.integration`)

- `test_gamma_live.py` : fetch un marché actif connu, vérifie qu'il a `liquidityClob > 0` et `clobTokenIds` non vide.
- `test_clob_midpoint_live.py` : fetch midpoint sur un token_id actif (depuis le fixture Gamma), vérifie `0 < mid < 1`.

### 10.11 Couverture

```bash
pytest --cov=src/polycopy/strategy --cov-report=term-missing
```

Seuil : **≥ 80 % sur `src/polycopy/strategy/`**. Couverture watcher/storage doit rester ≥ 80% (refactor queue ne doit rien casser).

---

## 11. Mises à jour de documentation (même PR)

### 11.1 `README.md`

Cocher M2 dans "État d'avancement" :

```markdown
- [x] M1 : Watcher + Storage
- [x] M2 : Strategy Engine
- [ ] M3 : Executor
- [ ] M4 : Monitoring
- [ ] M5 : Scoring
```

Ajouter à la table "Variables d'environnement" la nouvelle ligne `RISK_AVAILABLE_CAPITAL_USD_STUB`.

### 11.2 `docs/architecture.md`

Ajouter en tête de la section "Strategy" :

```markdown
> **Status M2** ✅ — implémenté au commit XYZ. Pipeline : MarketFilter → PositionSizer → SlippageChecker → RiskManager. Communication watcher↔strategy via `asyncio.Queue` (pas de polling DB). Voir `specs/M2-strategy-engine.md`.
```

Pas de réécriture de la section — la spec reste source de vérité fonctionnelle.

### 11.3 `CLAUDE.md`

Section "APIs Polymarket utilisées" : préciser usage M2 sous Gamma et CLOB read :

```markdown
- **Gamma API** : `https://gamma-api.polymarket.com` (public)
  - Métadonnées marchés (slug, conditionId, tokenIds, expiration).
  - **Utilisé à M2** par `MarketFilter` (liquidité, expiration, état actif).
  - Cache TTL 60s côté client pour respecter le rate limit.
- **CLOB API** : `https://clob.polymarket.com` (auth L1 + L2 pour trading)
  - **À M2 utilisé en read-only** (`GET /midpoint?token_id=...`, sans auth) par `SlippageChecker`.
  - Auth L1+L2 requise à partir de M3 (Executor) via `py-clob-client`.
```

Ajouter à la section "Sécurité — RÈGLES STRICTES" :

```markdown
- À M2 la strategy ne place **aucun ordre**. Elle est read-only (Gamma + CLOB midpoint, pas de signature, pas de POST). `settings.dry_run` n'a pas d'effet sur la strategy. Le garde-fou kicks in à M3 quand l'Executor lit `dry_run` avant d'envoyer un ordre.
```

### 11.4 Aucun nouveau guide setup

`docs/setup.md` reste valide — pas de nouvelle commande utilisateur, juste une nouvelle env var optionnelle (default sain).

---

## 12. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy/strategy --cov=src/polycopy/watcher --cov=src/polycopy/storage --cov-report=term-missing
pytest -m integration
python -m polycopy --dry-run            # ≥ 60s, doit logger strategy_started + (si trades) order_approved/rejected
```

---

## 13. Critères d'acceptation

- [ ] `python -m polycopy --dry-run` tourne **60 s** sans crash. Log : `polycopy_starting`, `db_initialized`, `watcher_started`, `strategy_started`. Si les wallets cibles tradent dans la fenêtre, ≥ 1 ligne `order_approved` ou `order_rejected` avec `reason`.
- [ ] Ctrl-C → shutdown propre : watcher + strategy se ferment via `stop_event`, `http_client.aclose()` x 2, `engine.dispose()`. Aucune task orpheline dans les logs.
- [ ] `ruff check .` : 0 erreur. `ruff format --check .` : 0 diff.
- [ ] `mypy src` (--strict) : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `src/polycopy/strategy/` (et watcher/storage restent ≥ 80% — pas de régression).
- [ ] Aucun appel CLOB qui place un ordre (vérif manuelle code review : pas de `client.post_order`, pas de `create_and_post_order`).
- [ ] Aucune signature L1/L2 effectuée — la strategy n'instancie jamais `py-clob-client.ClobClient` avec une `key` ou `creds`.
- [ ] `tests/fixtures/gamma_market_sample.json` et `tests/fixtures/clob_midpoint_sample.json` présents et utilisés par les tests unitaires.
- [ ] `docs/setup.md`, `README.md`, `docs/architecture.md`, `CLAUDE.md` mis à jour comme décrit §11.
- [ ] Commit unique : `feat(strategy): implement M2 filtering, sizing and risk pipeline`.

---

## 14. Hors scope M2 (NE PAS implémenter)

- Signature L1/L2 + envoi d'ordres CLOB → **M3**
- Calcul de capital disponible depuis le wallet on-chain → **M3** (M2 utilise `RISK_AVAILABLE_CAPITAL_USD_STUB`)
- Lecture/écriture de `MyPosition` (autre que SELECT pour `position_already_open`) → **M3**
- Persistance de `PnlSnapshot` → **M4**
- Alertes Telegram → **M4**
- Dashboard PnL, endpoint `/metrics` Prometheus → **M4**
- Scoring de traders, sélection automatique → **M5**
- Migration Alembic (M2 reste sur `Base.metadata.create_all`)
- WebSocket CLOB pour le mid-price (REST `/midpoint` suffit à la latence cible 15 s)
- Batch Gamma (1 condition_id = 1 appel à M2 ; batch utile à M5 pour le scoring)
- Cache Redis distribué (cache in-memory dict suffit pour 1 process)

---

## 15. Notes d'implémentation

**Ordre de travail suggéré** :

1. (Déjà fait par cette spec) capture `gamma_market_sample.json` + `clob_midpoint_sample.json` dans `tests/fixtures/`.
2. Ajouter `risk_available_capital_usd_stub` à `config.py` + `.env.example`. Étendre `test_config.py`.
3. Ajouter `StrategyDecision` à `models.py`. Ajouter `StrategyDecisionRepository` à `repositories.py`. Ajouter aux exports `storage/__init__.py`. Tests repo.
4. Créer `src/polycopy/strategy/{__init__.py, dtos.py}`. Tests DTOs (parsing fixture Gamma).
5. `gamma_client.py` + `test_gamma_client.py` (respx + cache + parsing fixture).
6. `clob_read_client.py` + `test_clob_read_client.py`.
7. `pipeline.py` : 1 filtre à la fois avec ses tests. Ordre : MarketFilter → PositionSizer → SlippageChecker → RiskManager.
8. `orchestrator.py` + `test_strategy_orchestrator.py`.
9. Refactor `WalletPoller` (queue optionnelle) — vérifier que tous les tests M1 passent toujours.
10. Refactor `__main__.py` : 2 queues, stop_event partagé, signal handlers déplacés.
11. Doc updates §11.
12. Smoke test `--dry-run` 60s.
13. `ruff`, `mypy`, `pytest` verts.
14. Commit unique.

**Principes** :

- **Pas d'abstraction prématurée** : pas d'interface `AbstractFilter`, pas d'interface `AbstractMarketDataSource`. 4 filtres concrets, 2 clients concrets.
- **Logs structurés partout** : événements clés (`strategy_started`, `order_approved`, `order_rejected`, `pipeline_error`, `gamma_cache_hit`/`gamma_cache_miss` debug, `executor_queue_full`, `strategy_queue_full`, `strategy_stopped`) avec bindings (`condition_id`, `tx_hash`, `decision`, `reason`, `my_size`, `slippage_pct`).
- **Pas de `print` jamais.**
- **Persister la décision AVANT le log** `order_approved/rejected` (audit > performance — si crash entre persist et log, on a la trace DB).
- **Tous les `condition_id`, `target_wallet`, `asset_id` en lowercase ou tels quels** : Gamma et CLOB renvoient les mêmes ids que Data API. Pas de normalisation supplémentaire.
- **`dataclasses` pour les types internes mutables** (`PipelineContext`), **Pydantic** pour tout ce qui traverse une frontière (API in/out, queue, DB).

**Pièges identifiés à éviter** :

1. La doc OpenAPI CLOB midpoint annonce `{"mid_price": "..."}` — c'est faux en prod. Utiliser `{"mid": "..."}` (vérifié via fixture capturée).
2. Gamma `clobTokenIds`, `outcomes`, `outcomePrices` sont des **strings JSON-stringifiées**, pas des arrays. Validator `mode="before"` qui `json.loads()`.
3. Gamma `questionID` (pas `questionId`) — case spécifique.
4. Pas de champ `resolved` direct sur Gamma ; déduire via `closed=true`.
5. `bestBid`/`bestAsk`/`lastTradePrice` sur Gamma sont mis à jour périodiquement, **ne pas s'en servir pour le slippage check**. Toujours `/midpoint` CLOB pour le prix temps réel.
6. `asyncio.Queue.put_nowait` vs `put` : on choisit `put_nowait` pour ne jamais bloquer le watcher si la strategy est saturée. Drop ≠ silencieux : log warning systématique.

---

## 16. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M2

Suis specs/M2-strategy-engine.md à la lettre. Avant tout code, action obligatoire : invoque /polymarket:polymarket pour reconfirmer les schémas Gamma /markets et CLOB /midpoint. Les fixtures tests/fixtures/gamma_market_sample.json et tests/fixtures/clob_midpoint_sample.json sont déjà capturées (cf. spec §3.3 et §6.3) — ne pas les recapturer sauf si la spec demande explicitement de raffraîchir.

Contraintes non négociables :
- Patches M1 (§0.5 polymarket fields optionnels, validator CSV TARGET_WALLETS, NoDecode) déjà appliqués — ne pas les redéfaire.
- Une seule nouvelle env var : RISK_AVAILABLE_CAPITAL_USD_STUB (default 1000.0). Documenter dans config.py + .env.example.
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print).
- Aucune signature L1/L2, aucun appel POST CLOB. La strategy est read-only.
- mypy --strict doit passer, ruff check propre, pytest vert avec coverage ≥ 80% sur strategy/ ET pas de régression sur watcher/storage.
- Refactor WalletPoller : ajouter param queue optionnel pour rétrocompat des tests M1.
- Refactor __main__ : 2 queues (detected_trades_queue, approved_orders_queue), stop_event partagé, signal handlers déplacés du WatcherOrchestrator vers __main__ (ou un module signals.py partagé).
- Mises à jour doc §11 dans le même commit.
- Commit final unique : feat(strategy): implement M2 filtering, sizing and risk pipeline

Demande-moi confirmation avant tout patch sensible (config.py, .env, suppression de fichier, refactor non listé ci-dessus).

Si une question reste ambiguë (ex: faut-il casser la signature publique de WalletPoller ou faire un constructeur alternatif ?), tranche avec une recommandation et signale-le moi avant d'implémenter, ne bloque pas silencieusement.
```
