# M3 — Executor

Spec d'implémentation du Module 3 de polycopy : consommer les `OrderApproved` produits par M2, signer et poster les ordres CLOB Polymarket via `py-clob-client`. **Mode `--dry-run` strict par défaut** : aucun POST réseau tant que `DRY_RUN=true`. Persistance complète (`my_orders`, `my_positions`) pour audit.

Source de vérité fonctionnelle : `docs/architecture.md` section "Executor". Conventions : `CLAUDE.md`. Schémas API : skill `/polymarket:polymarket` + https://docs.polymarket.com (`/api-reference/authentication.md`, `/api-reference/trade/post-a-new-order.md`, `/api-reference/market-data/get-tick-size.md`, `/api-reference/core/get-current-positions-for-a-user.md`, `/resources/error-codes.md`). Templates : `specs/M1-watcher-storage.md`, `specs/M2-strategy-engine.md`.

---

## 0. Pré-requis environnement

### 0.1 Bootstrap (déjà fait)

`bash scripts/setup.sh` (idempotent). Aucun nouveau patch config requis.

### 0.2 Skill Polymarket (déjà installé)

Source de vérité pour CLOB write + auth. Invocation : `/polymarket:polymarket`.

### 0.3 Variables `.env` — REQUISES si `DRY_RUN=false`

| Variable env | Champ Settings | Default | Requis quand |
|---|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | `polymarket_private_key` | `None` | `DRY_RUN=false` (sinon optionnel) |
| `POLYMARKET_FUNDER` | `polymarket_funder` | `None` | `DRY_RUN=false` (sinon optionnel) |
| `POLYMARKET_SIGNATURE_TYPE` | `polymarket_signature_type` | `1` | toujours (default OK pour Magic) |

**En `DRY_RUN=true` (défaut), aucune clé n'est requise.** L'Executor n'instancie jamais le SDK signataire. Tu peux développer et tester M3 entièrement sans avoir de clé Polymarket.

### 0.4 Aucun nouveau patch `config.py`

Les 3 champs sont déjà présents (M1 §0.5). Pas de modif.

### 0.5 Critère validation env

```bash
python -m polycopy --dry-run
```

Doit logger `executor_started` (avec `mode=dry_run`), `polycopy_starting` (`dry_run=True`), tourner 60s sans crash, exit 0 sur SIGINT.

```bash
python -m polycopy           # sans --dry-run et sans clés en .env
```

Doit **exit non-zero** avec le message `RuntimeError("Executor cannot start without Polymarket credentials when DRY_RUN=false")` avant d'entrer dans le `TaskGroup`.

---

## 1. Objectif M3 (scope exact)

Consommer en quasi-temps-réel les `OrderApproved` produits par M2 et émettre, pour chacun, un POST CLOB signé via `py-clob-client.ClobClient.create_and_post_order`. Persistance complète de l'ordre (`my_orders`) + mise à jour incrémentale de la position (`my_positions`) sur fill.

**Livrable fonctionnel dry-run** : `python -m polycopy --dry-run` lance Watcher + Strategy + Executor en parallèle. Quand un trade passe le pipeline M2, l'Executor reçoit l'`OrderApproved`, log `order_simulated` avec le payload qui aurait été signé, persiste `MyOrder(status="SIMULATED")`. Aucun appel réseau CLOB. Aucune signature.

**Livrable fonctionnel mode réel** (après bascule manuelle, hors scope auto) : même flow mais POST réel, persiste `MyOrder(status="SENT")` puis update vers `"FILLED"` / `"REJECTED"` / `"FAILED"` selon réponse CLOB. `MyPosition.upsert_on_fill` au filling.

**Hors livrable M3** : alertes Telegram, dashboard PnL, snapshots PnL périodiques, scoring de traders, lecture USDC balance via chain RPC, WebSocket user channel pour fills async.

---

## 2. Mode dry-run et garde-fous (CRITIQUE)

### 2.1 Lazy init `ClobClient`

`py-clob-client.ClobClient` (signataire) **n'est instancié que si** `settings.dry_run is False`. En `DRY_RUN=true`, l'Executor :

- **Ne charge pas** la clé privée (même si elle est dans `.env`).
- **N'effectue pas** la dérivation L1 → L2.
- **Ne signe rien**, n'effectue aucun appel réseau CLOB.

### 2.2 Garde-fou démarrage strict

`ExecutorOrchestrator.__init__` (ou `run_forever` au tout début) :

```python
if settings.dry_run is False:
    if settings.polymarket_private_key is None or settings.polymarket_funder is None:
        raise RuntimeError(
            "Executor cannot start without Polymarket credentials when DRY_RUN=false. "
            "Set POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER in .env, or use --dry-run."
        )
```

Lève **avant** d'entrer dans le `TaskGroup` (sinon TaskGroup catch et le wrap dans un `ExceptionGroup`, log moins clair).

### 2.3 Garde-fou par ordre (defense in depth)

Juste avant chaque appel `clob_client.create_and_post_order(...)`, re-vérifier `settings.dry_run is False`. Double check (si jamais le flag est flippé en cours de run par erreur).

### 2.4 Output dry-run

Pour chaque `OrderApproved` reçu :

- Construire `BuiltOrder` complet (token_id, price, size, side, tick_size, neg_risk, order_type, signature_type, funder).
- **Ne pas signer**, ne pas POST.
- Log structlog `order_simulated` avec tous les champs (sauf passphrase, secret — jamais loggés même en debug).
- Persister `MyOrder(status="SIMULATED", clob_order_id=None, simulated=True, sent_at=now())`.
- Pas de mise à jour `MyPosition` (puisque rien n'est rempli).

### 2.5 Output mode réel

- Construire `BuiltOrder`.
- POST via SDK : `result = await asyncio.to_thread(clob_client.create_and_post_order, order_args, options, order_type)` (le SDK est sync, on offload pour ne pas bloquer l'event loop).
- Mapper `result["status"]` vers enum DB :
  - `"matched"` → `MyOrder(status="FILLED")` + `MyPositionRepository.upsert_on_fill(...)`.
  - `"live"` → `MyOrder(status="SENT")` (resting sur l'orderbook, ne devrait pas arriver pour FOK mais on couvre).
  - `"delayed"` → `MyOrder(status="SENT")` (matching reporté, idem).
  - `success=False` → `MyOrder(status="REJECTED", error_msg=result["errorMsg"])`.
- Sur exception SDK (réseau, signature) : `MyOrder(status="FAILED", error_msg=str(exc))`, log `executor_error`, continue la boucle.

---

## 3. Modèles de données — extension `MyOrder` + `MyPosition`

### 3.1 Limites du schéma actuel

`MyOrder` actuel a : `id, source_trade_id, clob_order_id, side, size, price, status, sent_at, filled_at`. **Manque pour M3** : `source_tx_hash`, `condition_id`, `asset_id`, `tick_size`, `neg_risk`, `order_type`, `error_msg`, `simulated`, `taking_amount`, `making_amount`, `transaction_hashes`. Plus un statut enum strict.

`MyPosition` actuel a : `id, condition_id, asset_id, size, avg_price, opened_at, closed_at`. **Suffisant pour M3** mais `condition_id` et `asset_id` doivent passer `nullable=False`.

### 3.2 Nouveau schéma `MyOrder`

| Colonne | Type | Contrainte |
|---|---|---|
| `id` | `int` | PK autoincrement |
| `source_tx_hash` | `str(66)` | **indexed**, FK logique vers `detected_trades.tx_hash` |
| `clob_order_id` | `str(66) \| None` | **indexed**, null en SIMULATED ou FAILED |
| `condition_id` | `str(66)` | **indexed**, nullable=False |
| `asset_id` | `str` | nullable=False (token_id ERC1155) |
| `side` | `str(4)` | `"BUY"` ou `"SELL"` |
| `size` | `float` | shares demandés (input M3, pas le filled) |
| `price` | `float` | mid CLOB au moment de la décision M2 |
| `tick_size` | `float` | tick size récupéré juste avant POST |
| `neg_risk` | `bool` | depuis Gamma metadata |
| `order_type` | `str(4)` | `"FOK"` (default M3) ; `"GTC"` / `"FAK"` réservé futur |
| `status` | `str(16)` | enum strict : `SIMULATED \| SENT \| FILLED \| PARTIALLY_FILLED \| REJECTED \| FAILED` |
| `taking_amount` | `str \| None` | string fixed-math 6 decimals (depuis CLOB response) |
| `making_amount` | `str \| None` | idem |
| `transaction_hashes` | `JSON` | list des tx hashes on-chain (vide si pas filled) |
| `error_msg` | `str(256) \| None` | rempli si REJECTED ou FAILED |
| `simulated` | `bool` | true en dry-run, false en réel |
| `sent_at` | `datetime` | default `now(UTC)`, nullable=False |
| `filled_at` | `datetime \| None` | rempli si status=FILLED ou PARTIALLY_FILLED |

### 3.3 `MyPosition` (modifications mineures)

| Colonne | Type | Changement |
|---|---|---|
| `condition_id` | `str(66)` | nullable=False (était nullable=True) |
| `asset_id` | `str` | nullable=False (était nullable=True) |
| `size` | `float` | nullable=False, default 0.0 |
| `avg_price` | `float` | nullable=False, default 0.0 |
| `opened_at` | `datetime` | nullable=False (default now UTC) |
| `closed_at` | `datetime \| None` | inchangé (None tant que size > 0) |

Index unique sur `(condition_id, asset_id)` pour permettre l'upsert.

### 3.4 Stratégie de migration

**Décision : drop + recreate la DB locale** (`polycopy.db` est gitignored, c'est de la donnée dev sans valeur). Le user supprime `polycopy.db` au premier `python -m polycopy --dry-run` post-M3 ; `init_db.create_all` recrée tout. L'historique M1/M2 (detected_trades + strategy_decisions) est perdu, c'est acceptable pour un dev local.

**TODO M4** : introduire Alembic pour migrations en place. Note `# TODO M4: Alembic migration here` au-dessus des changements de schéma `MyOrder`/`MyPosition` dans `models.py`.

**Documenter dans `docs/setup.md`** : "Après git pull qui touche `src/polycopy/storage/models.py`, supprimer `polycopy.db` et relancer le bot — la DB se recrée automatiquement (les données dev sont perdues, c'est ok)."

### 3.5 Repositories — `src/polycopy/storage/repositories.py`

```python
class MyOrderRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    async def insert(self, dto: MyOrderDTO) -> MyOrder: ...
    async def update_status(
        self,
        order_id: int,
        status: Literal["SIMULATED", "SENT", "FILLED", "PARTIALLY_FILLED", "REJECTED", "FAILED"],
        *,
        clob_order_id: str | None = None,
        taking_amount: str | None = None,
        making_amount: str | None = None,
        transaction_hashes: list[str] | None = None,
        error_msg: str | None = None,
        filled_at: datetime | None = None,
    ) -> None: ...
    async def list_recent(self, limit: int = 100) -> list[MyOrder]: ...


class MyPositionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    async def upsert_on_fill(
        self,
        condition_id: str,
        asset_id: str,
        side: Literal["BUY", "SELL"],
        size_filled: float,
        fill_price: float,
    ) -> MyPosition: ...
    async def list_open(self) -> list[MyPosition]: ...
    async def get_open(self, condition_id: str) -> MyPosition | None: ...
```

`upsert_on_fill` logique :

- Si pas de position open sur `(condition_id, asset_id)` : insert avec `size=size_filled`, `avg_price=fill_price`.
- Si position open et `side=BUY` : `new_size = old_size + size_filled`, `new_avg = (old_size*old_avg + size_filled*fill_price) / new_size`. Update.
- Si `side=SELL` : `new_size = old_size - size_filled`. Si `new_size <= 0` : set `closed_at=now()`. avg_price inchangé.

Append-only sur `MyOrder` (jamais d'update du record original ; on update les champs status/filled_at via `update_status`).

---

## 4. DTOs internes — `src/polycopy/executor/dtos.py`

```python
class MyOrderDTO(BaseModel):
    """DTO pour MyOrderRepository.insert (status initial = SIMULATED ou SENT)."""
    model_config = ConfigDict(frozen=True)

    source_tx_hash: str
    condition_id: str
    asset_id: str
    side: Literal["BUY", "SELL"]
    size: float
    price: float
    tick_size: float
    neg_risk: bool
    order_type: Literal["FOK", "FAK", "GTC"]
    status: Literal["SIMULATED", "SENT"]
    simulated: bool
    clob_order_id: str | None = None


class BuiltOrder(BaseModel):
    """Snapshot d'un ordre prêt à signer (consommé par ClobWriteClient)."""
    model_config = ConfigDict(frozen=True)

    token_id: str
    side: Literal["BUY", "SELL"]
    size: float            # shares (FOK SELL) ou USD (FOK BUY) selon order_type/side, voir §6.4
    price: float           # rounded à tick_size
    tick_size: float
    neg_risk: bool
    order_type: Literal["FOK", "FAK", "GTC"]


class OrderResult(BaseModel):
    """Réponse CLOB normalisée."""
    model_config = ConfigDict(populate_by_name=True, extra="allow", frozen=True)

    success: bool
    clob_order_id: str | None = Field(default=None, alias="orderID")
    status: Literal["matched", "live", "delayed"] | None = None
    making_amount: str | None = Field(default=None, alias="makingAmount")
    taking_amount: str | None = Field(default=None, alias="takingAmount")
    transaction_hashes: list[str] = Field(default_factory=list, alias="transactionsHashes")
    trade_ids: list[str] = Field(default_factory=list, alias="tradeIDs")
    error_msg: str = Field(default="", alias="errorMsg")


class WalletState(BaseModel):
    """État du wallet pour le RiskManager (lu via Data API positions)."""
    model_config = ConfigDict(frozen=True)

    total_position_value_usd: float       # somme des `currentValue` Data API positions
    available_capital_usd: float          # à M3 = settings.risk_available_capital_usd_stub (USDC sur chain à M4+)
    open_positions_count: int
```

---

## 5. CLOB metadata client — `src/polycopy/executor/clob_metadata_client.py`

### 5.1 Endpoints (verrouillés via doc officielle)

- **Tick size** : `GET https://clob.polymarket.com/tick-size?token_id=<id>` — auth aucune.
- **Neg risk** : pas d'endpoint REST documenté isolé. Solution : récupérer depuis le `MarketMetadata` Gamma déjà fetché par M2 (ajouter `neg_risk` au DTO `MarketMetadata`), OU via le SDK `client.get_neg_risk(token_id)`.

**Décision : enrichir `MarketMetadata`** pour exposer `neg_risk` (champ `negRisk` Gamma, déjà absorbé par `extra="allow"`). Modif minime du DTO M2 :

```python
# src/polycopy/strategy/dtos.py
class MarketMetadata(BaseModel):
    ...
    neg_risk: bool = Field(default=False, alias="negRisk")
```

L'Executor récupère le marché via `GammaApiClient.get_market(condition_id)` (déjà câblé, déjà cached 60s). Pas de nouveau client neg_risk dédié. **Justifié** : éviter un 2e appel réseau quand l'info est déjà dans le cache Gamma.

### 5.2 Schéma tick-size

```yaml
TickSizeResponse:
  minimum_tick_size: number   # double, ex: 0.01
```

**Pièges** :
- Réponse `number` (float Python), pas string.
- Tick sizes valides connus : 0.01, 0.001 (selon marché).
- Le `price` envoyé doit être un multiple de `tick_size`. Sinon 400 `"price ... breaks minimum tick size rule"`.

### 5.3 Cache TTL

- Tick size : cache TTL 5 min (les tick sizes changent rarement par marché).
- Neg risk : pas de cache dédié (vient du cache Gamma 60s déjà en place).

### 5.4 Client

```python
class ClobMetadataClient:
    BASE_URL = "https://clob.polymarket.com"
    DEFAULT_TIMEOUT = 5.0
    CACHE_TTL = timedelta(minutes=5)

    def __init__(self, http_client: httpx.AsyncClient) -> None: ...

    async def get_tick_size(self, token_id: str) -> float: ...   # cached
```

Pattern identique à `GammaApiClient` (cache dict, `_now()` monkeypatchable, tenacity retry).

### 5.5 Justification : httpx direct, pas py-clob-client read-only

Cohérence avec `ClobReadClient` (M2). py-clob-client `get_tick_size` est sync — il faudrait `asyncio.to_thread`. httpx async direct est plus simple, plus testable (respx).

---

## 6. CLOB write client — `src/polycopy/executor/clob_write_client.py`

### 6.1 Endpoint

- **POST `https://clob.polymarket.com/order`**
- **Auth L2 (HMAC-SHA256)** via 5 headers (`POLY_API_KEY`, `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_PASSPHRASE`, `POLY_TIMESTAMP`).
- HTTP : 200 (success ou error logique), 400 (validation), 401 (auth), 429 (rate limit), 500/503 (transient).

### 6.2 Auth L1 → L2 derivation (au boot)

```python
# Pseudo-code (impl sync via py-clob-client)
temp_client = ClobClient(host="https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=137)
api_creds = temp_client.create_or_derive_api_creds()   # déterministe pour la même clé+nonce
# api_creds = ApiCreds(api_key="uuid", api_secret="base64", api_passphrase="string")
client = ClobClient(
    host=...,
    key=PRIVATE_KEY,
    chain_id=137,
    creds=api_creds,
    signature_type=settings.polymarket_signature_type,  # 0/1/2
    funder=settings.polymarket_funder,
)
```

**Déterministe** : `create_or_derive_api_creds()` retourne la même triplet à chaque appel pour la même clé+nonce (par défaut nonce=0). Pas besoin de cache DB. Re-derive au boot, OK.

**Sécurité** : `api_secret` et `api_passphrase` ne doivent **JAMAIS** être loggés, même partiellement. Aucun `log.info("creds_derived", api_key=..., secret=...)`. Au boot, log juste `executor_creds_ready` (sans aucun champ creds).

### 6.3 Méthode publique

```python
class ClobWriteClient:
    def __init__(self, settings: Settings) -> None:
        # Garde-fou : raise si settings.dry_run is True ou clés absentes.
        if settings.dry_run:
            raise RuntimeError("ClobWriteClient must not be instantiated in dry-run mode")
        if settings.polymarket_private_key is None or settings.polymarket_funder is None:
            raise RuntimeError("ClobWriteClient requires POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER")
        self._client = self._derive_client(settings)

    async def post_order(self, built: BuiltOrder) -> OrderResult: ...

    @staticmethod
    def _derive_client(settings: Settings) -> ClobClient: ...
```

`post_order` exécute le SDK via `asyncio.to_thread` (le SDK est sync) :

```python
def _build_order_args(built: BuiltOrder) -> OrderArgs: ...

async def post_order(self, built: BuiltOrder) -> OrderResult:
    args = self._build_order_args(built)
    options = {"tick_size": str(built.tick_size), "neg_risk": built.neg_risk}
    response = await asyncio.to_thread(
        self._client.create_and_post_order,
        args,
        options,
        OrderType[built.order_type],   # FOK | FAK | GTC
    )
    return OrderResult.model_validate(response)
```

### 6.4 Sémantique `OrderArgs.size` selon `order_type` × `side`

| order_type | side | `size` représente |
|---|---|---|
| GTC | BUY | shares à acheter (limit order) |
| GTC | SELL | shares à vendre |
| FOK | BUY | **USD à dépenser** (à confirmer empiriquement à M3) |
| FOK | SELL | shares à vendre |
| FAK | BUY | USD à dépenser |
| FAK | SELL | shares à vendre |

**Question ouverte (cf. §17)** : le SKILL.md du plugin dit "FOK/FAK BUY: amount = dollar amount to spend" mais l'`OrderArgs` Python a un champ `size`, pas `amount`. À tester empiriquement sur staging (`https://clob-staging.polymarket.com`) avant le 1er run réel. **À M3, default `order_type="FOK"`** ; pour `BUY`, passer `size = my_size_USD = my_shares * my_price` (USD à dépenser). Pour `SELL`, passer `size = my_shares`.

### 6.5 Round price à tick_size

Avant POST :

```python
def _round_to_tick(price: float, tick_size: float) -> float:
    return round(price / tick_size) * tick_size
```

Sinon erreur 400 `"breaks minimum tick size rule"`. Tick sizes courants : 0.01 et 0.001.

### 6.6 Retry tenacity

Catégoriser les erreurs CLOB (basé sur `error-codes.md`) :

| Catégorie | Status code | Pattern errorMsg | Action |
|---|---|---|---|
| **Transient** | 429, 425, 500, 503 | "Too Many Requests", "Trading is currently disabled", "market is not yet ready" | Retry exponentiel, 3 tentatives max |
| **Auth** | 401 | "Invalid api key", "Invalid L1 Request headers" | Log error, alerter (M4), `status=FAILED`, ne pas retry |
| **Validation** | 400 | "tick size rule", "not enough balance", "Size lower than minimum", "Duplicated", "address banned", "owner has to be the owner of the API KEY" | Log info, `status=REJECTED`, ne pas retry |

Tenacity sur `_post_via_sdk` interne :

```python
@retry(
    retry=retry_if_exception_type(_TransientClobError),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def _post_via_sdk(self, args, options, order_type) -> dict[str, Any]: ...
```

Wrapper qui inspecte la réponse SDK et raise `_TransientClobError` / `_AuthClobError` / `_ValidationClobError` selon le pattern. Ces exceptions sont catchées dans l'Executor pipeline pour mapper vers les status DB.

---

## 7. Wallet state reader — `src/polycopy/executor/wallet_state_reader.py`

### 7.1 Décision : option (a) Data API positions

Recommandation **option (a)** : lecture `GET https://data-api.polymarket.com/positions?user=<funder>`. Pas de dep `web3`, pas de RPC chain, pattern cohérent avec M1 (`DataApiClient`).

**Limitations acceptées à M3** :
- USDC balance non lu directement → `available_capital_usd = settings.risk_available_capital_usd_stub` (stub conservé).
- Exposure réelle = somme `currentValue` des positions ouvertes Data API → plus précis que le stub seul.

L'option (b) chain RPC USDC est repoussée (M5+ si besoin de précision wallet absolue).

### 7.2 Endpoint Data API positions

- URL : `https://data-api.polymarket.com/positions`
- Auth : aucune
- Param : `user=<address>` (lowercase, le funder)
- Pagination : `limit` (max 500), `offset` (max 10000)

### 7.3 Schéma de réponse

Array d'objets Position (verbatim doc) :

```yaml
Position:
  proxyWallet: string                  # = user param
  asset: string                        # token_id
  conditionId: string
  size: number                         # shares (positif si long, négatif si short ; à confirmer pour M3)
  avgPrice: number
  initialValue: number                 # USD payé à l'entrée
  currentValue: number                 # valeur USD courante (utile pour exposure)
  cashPnl: number
  percentPnl: number
  totalBought: number
  realizedPnl: number
  percentRealizedPnl: number
  curPrice: number                     # mid courant Polymarket
  redeemable: boolean
  mergeable: boolean
  title: string
  slug: string
  icon: string
  eventSlug: string
  outcome: string
  outcomeIndex: integer
  oppositeOutcome: string
  oppositeAsset: string
  endDate: string (date-time)
  negativeRisk: boolean
```

### 7.4 Client + DTO

```python
class WalletStateReader:
    BASE_URL = "https://data-api.polymarket.com"
    DEFAULT_TIMEOUT = 10.0
    CACHE_TTL = timedelta(seconds=30)        # plus court que Gamma : positions bougent

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None: ...

    async def get_state(self) -> WalletState: ...
```

`get_state` :

1. En `dry_run=true` : retourne `WalletState(total_position_value_usd=0.0, available_capital_usd=settings.risk_available_capital_usd_stub, open_positions_count=0)` sans appel réseau (pas de funder).
2. En `dry_run=false` : `GET /positions?user=<funder>`, somme `currentValue`, retourne `WalletState`.

Retry tenacity, pattern cohérent avec `DataApiClient`.

### 7.5 Note : ce reader ne modifie pas le RiskManager M2 à M3

Le RiskManager M2 lit `settings.risk_available_capital_usd_stub` directement. À M3 on **n'injecte pas** `WalletStateReader` dans le pipeline strategy (ce serait un re-design M2). Le `WalletStateReader` est utilisé par l'Executor lui-même comme garde-fou avant POST :

- Avant chaque `post_order` réel, fetch `wallet_state`.
- Si `total_position_value_usd + cost_of_this_order > available_capital_usd` : `MyOrder(status="REJECTED", error_msg="capital_exceeded_at_executor")`. Ne pas POST.

Defense in depth : le RiskManager M2 a déjà filtré avec le stub, mais entre l'approval M2 et le POST M3 d'autres ordres peuvent avoir consommé du capital. Re-check au dernier moment.

---

## 8. Pipeline executor — `src/polycopy/executor/pipeline.py`

```python
async def execute_order(
    approved: OrderApproved,
    *,
    settings: Settings,
    metadata_client: ClobMetadataClient,
    gamma_client: GammaApiClient,
    write_client: ClobWriteClient | None,    # None en dry-run
    wallet_state_reader: WalletStateReader,
    order_repo: MyOrderRepository,
    position_repo: MyPositionRepository,
) -> None: ...
```

Étapes :

1. **Fetch metadata** : `tick_size = await metadata_client.get_tick_size(approved.asset_id)` ; `market = await gamma_client.get_market(approved.condition_id)` (utilise le cache Gamma 60s) → `neg_risk = market.neg_risk if market else False`.
2. **Build order** : round `approved.my_price` à `tick_size`. Construire `BuiltOrder` (default `order_type="FOK"`).
3. **Branche dry-run** :
   - Insert `MyOrder(status="SIMULATED", simulated=True, ...)`.
   - Log `order_simulated` (binding : `tx_hash`, `condition_id`, `side`, `size`, `price`, `tick_size`, `neg_risk`).
4. **Branche réelle** :
   - **Garde-fou capital** : `wallet_state = await wallet_state_reader.get_state()`. Si capital exceeded → `MyOrder(status="REJECTED", error_msg="capital_exceeded_at_executor")`, log `order_rejected_at_executor`, return.
   - **Garde-fou §2.3** : assert `settings.dry_run is False` (sinon raise — bug).
   - Insert `MyOrder(status="SENT", simulated=False, ...)` (avant POST pour audit).
   - Try : `result = await write_client.post_order(built)`.
   - Mapper `result` :
     - `result.success and result.status == "matched"` → `update_status("FILLED", clob_order_id, taking_amount, making_amount, transaction_hashes, filled_at=now())` + `position_repo.upsert_on_fill(...)` + log `order_filled`.
     - `result.success` (status `"live"` ou `"delayed"`) → `update_status("SENT", clob_order_id)` + log `order_sent` warning (FOK ne devrait pas resting).
     - `not result.success` → `update_status("REJECTED", error_msg=result.error_msg)` + log `order_rejected`.
   - Catch `_AuthClobError` → `update_status("FAILED", error_msg=...)` + log `executor_auth_error` error. **Stop l'orchestrator** (re-raise ; problème de creds, inutile de continuer).
   - Catch `_ValidationClobError` → `update_status("REJECTED", error_msg=...)` + log `order_validation_rejected`.
   - Catch `Exception` → `update_status("FAILED", error_msg=str(exc))` + log `executor_error` (exception trace), continue la boucle.

---

## 9. Orchestrateur Executor — `src/polycopy/executor/orchestrator.py`

```python
class ExecutorOrchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        approved_orders_queue: asyncio.Queue[OrderApproved],
    ) -> None:
        # Garde-fou démarrage strict §2.2.
        if settings.dry_run is False:
            if settings.polymarket_private_key is None or settings.polymarket_funder is None:
                raise RuntimeError("Executor cannot start without Polymarket credentials when DRY_RUN=false")
        self._settings = settings
        self._session_factory = session_factory
        self._queue = approved_orders_queue

    async def run_forever(self, stop_event: asyncio.Event) -> None: ...
```

`run_forever` :

1. Construit `httpx.AsyncClient` partagé pour read.
2. Construit `GammaApiClient`, `ClobMetadataClient`, `WalletStateReader`, `MyOrderRepository`, `MyPositionRepository`.
3. Si `settings.dry_run is False` : `write_client = ClobWriteClient(settings)` (lazy init L1→L2 dérivation ; sync car SDK sync, OK car au boot). Sinon `write_client = None`.
4. Log `executor_started` avec `mode=("dry_run" or "real")`, `signature_type=...` (en mode réel uniquement).
5. Boucle `while not stop_event.is_set()` :
   - `approved = await asyncio.wait_for(queue.get(), timeout=1.0)`.
   - Sur `TimeoutError` : continue.
   - Sur `asyncio.CancelledError` : raise.
   - Try `execute_order(...)`.
   - Catch `_AuthClobError` au niveau orchestrator : log `executor_auth_fatal`, set `stop_event` (arrêt complet — la creds est cassée).
   - Catch `Exception` : log `executor_loop_error`, continue (déjà géré par `execute_order` mais double safety).
6. Sur sortie : `await http_client.aclose()`, log `executor_stopped`.

---

## 10. Intégration `__main__` — `src/polycopy/__main__.py`

Modifier `_run()` : après instanciation Watcher + Strategy, ajouter Executor :

```python
executor = ExecutorOrchestrator(
    session_factory,
    settings,
    approved_orders_queue=approved_orders_queue,   # déjà créée à M2
)
async with asyncio.TaskGroup() as tg:
    tg.create_task(watcher.run_forever(stop_event))
    tg.create_task(strategy.run_forever(stop_event))
    tg.create_task(executor.run_forever(stop_event))
```

L'instanciation `ExecutorOrchestrator(...)` lève **avant** l'entrée du TaskGroup si garde-fou §2.2 déclenche. Donc le `RuntimeError` propage clairement vers `main()` → catch `Exception` → `polycopy_crashed` log → `sys.exit(1)`.

Pas de nouvelle queue. Pas de nouvelle env var côté __main__.

---

## 11. Tests

### 11.1 Arborescence

```
tests/
├── fixtures/
│   ├── activity_sample.json              # M1
│   ├── gamma_market_sample.json          # M2
│   ├── clob_midpoint_sample.json         # M2
│   ├── clob_tick_size_sample.json        # M3 — {"minimum_tick_size": 0.01}
│   ├── clob_order_response_sample.json   # M3 — réponse mockée du POST /order (basée sur doc)
│   └── data_api_positions_sample.json    # M3 — array Position
├── unit/
│   ├── (tests M1+M2 existants)
│   ├── test_my_order_repository.py
│   ├── test_my_position_repository.py
│   ├── test_clob_metadata_client.py
│   ├── test_clob_write_client.py
│   ├── test_wallet_state_reader.py
│   ├── test_executor_pipeline.py
│   └── test_executor_orchestrator.py
└── integration/
    ├── (tests M1+M2 existants)
    └── test_clob_l1_l2_auth_live.py      # @pytest.mark.integration — auth derivation only, NO POST
```

### 11.2 Fixtures à créer (manuelles, pas de capture réseau pour les write endpoints)

- `clob_tick_size_sample.json` : `{"minimum_tick_size": 0.01}` (capturable réellement, public).
- `clob_order_response_sample.json` : payload manuel basé sur le schéma doc (`{"success": true, "orderID": "0xabc...", "status": "matched", "makingAmount": "100000000", "takingAmount": "200000000", "transactionsHashes": ["0x..."], "tradeIDs": ["..."], "errorMsg": ""}`). Pas capturable sans poster un vrai ordre.
- `data_api_positions_sample.json` : capturable réellement avec une adresse publique du leaderboard.

### 11.3 `conftest.py` (extension)

- `sample_tick_size`, `sample_order_response`, `sample_positions` (lectures fixtures).
- `my_order_repo`, `my_position_repo` dérivés de `session_factory`.
- `_dry_run_settings()` helper (Settings avec `dry_run=True`, pas de clés).
- `_real_settings()` helper (Settings avec `dry_run=False`, clés mockées style `"0x" + "1"*64`).

### 11.4 `test_my_order_repository.py`

- `insert` persiste un MyOrder avec status SIMULATED, simulated=True.
- `update_status` SIMULATED → SENT illegal (assert ou doc convention) ; SENT → FILLED OK avec champs cascade ; FILLED → ré-update raise (append-only des transitions terminales).
- `list_recent` ordre `sent_at DESC`.

### 11.5 `test_my_position_repository.py`

- `upsert_on_fill` : 1ère BUY → insert, size = fill_size, avg_price = fill_price.
- 2e BUY même condition_id → size cumulé, avg_price moyenne pondérée correcte.
- SELL partiel → size décrémenté, avg_price inchangé.
- SELL total (size → 0) → `closed_at` set.
- `list_open` filtre `closed_at IS NULL`.

### 11.6 `test_clob_metadata_client.py` (respx)

- Happy path tick-size → 0.01 float.
- Cache TTL 5 min (2 calls → 1 fetch).
- Retry sur 429.

### 11.7 `test_clob_write_client.py`

- **Mock total** de `py_clob_client.client.ClobClient` via `monkeypatch.setattr` ou `unittest.mock.patch`.
- Constructor en `dry_run=true` → `RuntimeError` levé.
- Constructor sans `polymarket_private_key` (en `dry_run=false`) → `RuntimeError`.
- Constructor en mode réel : mock `ClobClient.__init__` → assert appelé avec bons params (host, key, chain_id=137, signature_type, funder).
- `post_order(built)` :
  - Mock SDK retourne `{"success": True, "status": "matched", "orderID": "0x...", ...}` → `OrderResult` correctement parsé (alias).
  - SDK raise `Exception("rate limit")` → après retry tenacity, raise.
  - SDK retourne `{"success": False, "errorMsg": "tick size rule"}` → retour `OrderResult(success=False, error_msg="tick size rule")`.
- Round price à tick_size : 0.0805 avec tick=0.01 → 0.08.

### 11.8 `test_wallet_state_reader.py` (respx)

- En dry_run : pas de fetch réseau, retourne stub.
- En réel : mock GET /positions retourne array, somme `currentValue` correcte.
- Cache TTL 30s.

### 11.9 `test_executor_pipeline.py`

- Dry-run path : `OrderApproved` in → `MyOrder(status="SIMULATED", simulated=True)` persisté, log `order_simulated` (capter via `caplog` structlog).
- Real path success matched : mock SDK + Gamma + tick-size → `MyOrder(status="FILLED")` + `MyPosition` upserted.
- Real path validation reject : mock SDK retourne `success=False, errorMsg="not enough balance"` → `MyOrder(status="REJECTED", error_msg="not enough balance")`. Pas de position update.
- Real path auth error : mock SDK raise `_AuthClobError` → `MyOrder(status="FAILED")`, exception re-raise (test catch).
- Real path capital exceeded at executor : wallet_state mock → high exposure → `MyOrder(status="REJECTED", error_msg="capital_exceeded_at_executor")`. Pas de POST.

### 11.10 `test_executor_orchestrator.py`

- Dry-run : init OK sans clés. 1 OrderApproved push → simulated.
- `dry_run=false, key=None` : `RuntimeError` au constructor.
- `dry_run=false, key set` : init OK, mock write client, 1 OrderApproved → flow réel.
- `_AuthClobError` propage → stop_event set, log `executor_auth_fatal`.
- `Exception` autre → loop continue.
- Shutdown propre via stop_event.

### 11.11 Test live opt-in (`@pytest.mark.integration`)

`test_clob_l1_l2_auth_live.py` :

```python
@pytest.mark.integration
def test_l1_l2_auth_derivation_with_throwaway_key() -> None:
    """Vérifie que create_or_derive_api_creds() retourne bien la triplet attendue.
    Utilise une clé jetable générée localement, ZÉRO fonds. Aucun POST."""
    from eth_account import Account
    private_key = Account.create().key.hex()  # clé fresh, jetable
    client = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137)
    creds = client.create_or_derive_api_creds()
    assert creds.api_key
    assert creds.api_secret
    assert creds.api_passphrase
```

**PAS de `test_post_order_live`** — trop dangereux pour un test automatique. Bascule manuelle uniquement.

### 11.12 Couverture

```bash
pytest --cov=src/polycopy/executor --cov-report=term-missing
```

Seuil : **≥ 80% sur `src/polycopy/executor/`**. M1+M2 doivent rester ≥ 80% (pas de régression).

---

## 12. Mises à jour de documentation (même PR)

### 12.1 `README.md` — polish public + image

**En tête, avant le titre** :

```markdown
<p align="center">
  <img src="assets/Company_Logo_Polymarket.png" alt="Polymarket" width="400">
</p>

# polycopy
```

(Image centrée, width 400px pour ne pas écraser le texte ; ratio préservé. Logo Polymarket déjà présent dans `assets/`.)

**Reformatage des sections** :

- Cocher M3 dans "État d'avancement".
- Ajouter ligne `RISK_AVAILABLE_CAPITAL_USD_STUB` dans "Variables d'environnement" — déjà fait à M2 d'ailleurs, vérifier.
- Ajouter une section **"Quickstart"** avec un exemple de log JSON :
  ```
  {"event": "trade_detected", "wallet": "0x...", "tx_hash": "0x...", "side": "BUY", ...}
  {"event": "order_approved", "tx_hash": "0x...", "my_size": 36.85, "slippage_pct": 0.39}
  {"event": "order_simulated", "condition_id": "0x...", "size": 36.85, "price": 0.08, "neg_risk": false}
  ```
- Ajouter une section **"Going live"** (nouvelle, post-M3) :
  ```markdown
  ## Going live (passage du dry-run au mode réel)

  1. Ajouter dans `.env` :
     - `POLYMARKET_PRIVATE_KEY=0x<ta_clé_privée>` (jamais commit)
     - `POLYMARKET_FUNDER=0x<ton_proxy_wallet>` (depuis polymarket.com/settings)
     - `POLYMARKET_SIGNATURE_TYPE=2` (Gnosis Safe — défaut le plus courant)
  2. Mettre `MAX_POSITION_USD=1` pour le 1er run réel (limite de sécurité forte).
  3. Lancer sans `--dry-run` : `python -m polycopy`.
  4. Surveiller les logs `order_filled` / `order_rejected` ; vérifier les transactions sur polymarket.com.
  5. Quand satisfait, augmenter progressivement `MAX_POSITION_USD`.
  
  ⚠️ `DRY_RUN` est `true` par défaut. Aucun ordre n'est jamais envoyé sans bascule explicite.
  ```
- Garder la section "Avertissement" telle quelle (légal).

### 12.2 `docs/architecture.md`

Ajouter en tête de la section "Executor" :

```markdown
> **Status M3** ✅ — implémenté. Dry-run par défaut (aucun POST CLOB). Mode réel via `py-clob-client` avec L1→L2 auth dérivation au boot. Pipeline : metadata fetch → tick-size round → garde-fou capital → POST → persist + position upsert. Voir `specs/M3-executor.md` et `src/polycopy/executor/`.
```

### 12.3 `CLAUDE.md`

Ajouter à la section "Sécurité — RÈGLES STRICTES" (après l'item M2 existant) :

```markdown
- **Executor M3** : 4 garde-fous obligatoires :
  1. Lazy init `ClobClient` (pas instancié si `dry_run=true`).
  2. Garde-fou démarrage : `RuntimeError` si `dry_run=false` ET clés absentes (lève avant TaskGroup).
  3. Double check par ordre : assert `dry_run is False` juste avant chaque `create_and_post_order`.
  4. Garde-fou capital exécutor : re-fetch wallet state avant POST, reject si exposition + cost > capital.
- **Creds L2** (`api_key`, `api_secret`, `api_passphrase`) ne doivent JAMAIS être loggées même partiellement, même en debug.
- `signature_type` mismatch = transactions rejetées silencieusement par CLOB. Toujours documenter dans `.env.example` les 3 valeurs (0/1/2) et leur correspondance wallet.
```

### 12.4 `docs/setup.md`

Ajouter une section "Migration de schéma DB" :

```markdown
## Migration de schéma DB (post-M3)

Après un `git pull` qui modifie `src/polycopy/storage/models.py`, supprimer la DB locale :

\`\`\`bash
rm polycopy.db
python -m polycopy --dry-run   # init_db.create_all recrée tout
\`\`\`

Les données de dev (detected_trades, strategy_decisions) sont perdues. C'est attendu jusqu'à l'introduction d'Alembic à M4.
```

---

## 13. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy/executor --cov=src/polycopy/strategy --cov=src/polycopy/watcher --cov=src/polycopy/storage --cov-report=term-missing
pytest -m integration                                 # opt-in, taps Polymarket Data API + auth derivation (no POST)
python -m polycopy --dry-run                          # ≥ 60s sans erreur, log executor_started
python -m polycopy 2>&1 | head -5                     # sans .env clés → exit non-zero, message clair
```

---

## 14. Critères d'acceptation

- [ ] `python -m polycopy --dry-run` (sans clés Polymarket) tourne **60 s** sans crash. Log : `polycopy_starting`, `db_initialized`, `watcher_started`, `strategy_started`, `executor_started` (`mode=dry_run`). Si trades dans la fenêtre, ≥ 1 ligne `order_simulated`.
- [ ] `python -m polycopy` (sans `--dry-run` ET sans clés) → exit 1 avec `RuntimeError("Executor cannot start without Polymarket credentials when DRY_RUN=false")` clairement loggé.
- [ ] Aucun appel POST CLOB en mode dry-run (vérification via mock asserts dans `test_executor_pipeline.py`).
- [ ] Aucun log de `api_secret`, `api_passphrase`, `private_key` même partiel (recherche `grep -r "secret\|passphrase\|private_key" src/` ne retourne que les noms de variables, jamais des valeurs).
- [ ] `ruff check .` + `ruff format --check .` : 0 erreur.
- [ ] `mypy src` (--strict) : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `executor/`. M1+M2 ≥ 80% (pas de régression).
- [ ] `tests/fixtures/clob_tick_size_sample.json`, `clob_order_response_sample.json`, `data_api_positions_sample.json` présents.
- [ ] `README.md` polished avec image `assets/Company_Logo_Polymarket.png` en tête, sections "Quickstart" + "Going live" + "Avertissement", lisible pour public externe.
- [ ] `docs/setup.md` documente la migration de schéma post-M3 (`rm polycopy.db`).
- [ ] `docs/architecture.md` marque Executor `Status M3 ✅`.
- [ ] `CLAUDE.md` documente les 4 garde-fous Executor + interdit le log des creds L2.
- [ ] Commit unique : `feat(executor): implement M3 order signing and posting with dry-run safeguards`.

---

## 15. Hors scope M3 (NE PAS implémenter)

- Alertes Telegram → **M4**
- Dashboard PnL, endpoint `/metrics` Prometheus → **M4**
- Snapshots `PnlSnapshot` périodiques → **M4** (`PnlSnapshot` reste une table vide)
- Backtest framework → hors roadmap actuelle
- Scoring de traders, sélection automatique → **M5**
- Lecture USDC balance via chain RPC (option (b) §7) — gardé pour M5+ si besoin
- WebSocket user channel pour fills async — `create_and_post_order` retourne déjà le résultat de fill pour FOK
- Migrations Alembic (M3 = drop+recreate ; **M4** = Alembic)
- Multi-process / multi-VPS
- Auto-bascule dry-run → réel (toujours manuelle, par sécurité)
- Modification du RiskManager M2 pour utiliser le `WalletStateReader` (le RiskManager garde le stub à M3 ; le re-check capital se fait côté Executor uniquement)

---

## 16. Notes d'implémentation

**Ordre de travail suggéré** :

1. (Préalable, fait par cette spec) skill query + fetch doc officielle pour L1/L2, POST /order, tick-size, positions, error-codes.
2. Capture `tests/fixtures/clob_tick_size_sample.json` (réelle, 1 appel httpx) et `tests/fixtures/data_api_positions_sample.json` (réelle, 1 appel sur une adresse leaderboard publique). Composer manuellement `clob_order_response_sample.json` basé sur le schéma doc — pas de POST réel.
3. Étendre `MarketMetadata` avec `neg_risk` (modif minime M2). Re-run tests M2 pour vérifier non-régression.
4. Modifier `models.py` : `MyOrder` étendu, `MyPosition` colonnes nullable=False. Drop+recreate `polycopy.db` local.
5. Ajouter `MyOrderRepository`, `MyPositionRepository` à `repositories.py` + exports `storage/__init__.py`. Tests repos.
6. Créer `src/polycopy/executor/{__init__.py, dtos.py}`. Tests DTOs.
7. `clob_metadata_client.py` + tests respx.
8. `wallet_state_reader.py` + tests respx (dry-run + mode réel).
9. `clob_write_client.py` : commencer par les garde-fous (RuntimeError au constructor) + tests qui les couvrent. Puis mock complet du SDK + tests `post_order` happy/transient/auth/validation.
10. `pipeline.py` `execute_order` + tests pipeline (dry-run + real path complet).
11. `orchestrator.py` `ExecutorOrchestrator` + tests (init, queue consumption, shutdown).
12. Refactor `__main__.py` : brancher Executor dans le TaskGroup.
13. Doc updates §12 (README polish + image, architecture, CLAUDE, setup).
14. Smoke test `--dry-run` 60s. Vérifier `order_simulated` apparaît si les wallets tradent.
15. Test bascule réelle : **manuel uniquement, par le user**, avec `MAX_POSITION_USD=1`. Pas dans la PR.
16. Commit unique : `feat(executor): implement M3 order signing and posting with dry-run safeguards`.

**Principes** :

- **Pas d'abstraction prématurée** : `ClobWriteClient`, `ClobMetadataClient`, `WalletStateReader`, `ExecutorOrchestrator` sont 4 classes concrètes. Pas d'interface `AbstractExecutor` à M3.
- **Logs structurés partout** : `executor_started`, `executor_creds_ready`, `order_simulated`, `order_sent`, `order_filled`, `order_rejected`, `order_validation_rejected`, `order_rejected_at_executor`, `executor_auth_error`, `executor_auth_fatal`, `executor_error`, `executor_loop_error`, `executor_stopped`. Bindings : `tx_hash`, `condition_id`, `clob_order_id`, `status`, `error_msg`, `mode`.
- **Pas de `print` jamais.**
- **Persister AVANT de POST** : `MyOrder(status="SENT")` inséré avant l'appel SDK (audit > performance — si crash entre POST et persist, on perd la trace de l'ordre envoyé).
- **Garde-fous redondants** : 4 niveaux de protection contre POST accidentel en dry-run (lazy init + assert constructeur write client + assert per-order + RuntimeError startup).
- **Asynchrone propre** : `py-clob-client` est sync ; toujours wrap via `asyncio.to_thread` pour ne pas bloquer l'event loop. Le SDK Polymarket lui-même utilise `requests` (sync), pas `httpx`.

**Pièges identifiés à éviter (à documenter dans le code via commentaires courts si besoin)** :

1. **`signature_type` mal réglé** = transactions rejetées silencieusement (status `success=False, errorMsg=""` ou auth error). Documenter clairement les 3 valeurs (0=EOA, 1=POLY_PROXY=Magic, 2=GNOSIS_SAFE=la plupart des wallets) dans `.env.example`. Utilisateur Polymarket.com via Magic = 1. Utilisateur via wallet ext (MetaMask) connecté à polymarket.com = 2 (Gnosis Safe créé automatiquement).
2. **`tick_size` non respecté** = order rejected par le matching engine avec message `"price ... breaks minimum tick size rule"`. Toujours fetch + arrondir AVANT POST.
3. **`neg_risk` markets** utilisent un Exchange contract différent (`0xC5d563A36AE78145C45a50134d48A1215220f80a` vs `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`). Si non passé en `options.neg_risk=True` pour un marché neg risk, l'ordre est rejeté ou pire (envoyé au mauvais Exchange et perdu). Toujours fetch via Gamma `negRisk` champ.
4. **FOK BUY semantics** : `OrderArgs.size` représente probablement les USD à dépenser (selon le SKILL.md du plugin), pas les shares. Très facile à inverser et brûler du capital en envoyant 100 shares à $0.50 alors qu'on voulait dépenser $100. Verrouiller via tests + commenter dans `_build_order_args`.
5. **L1→L2 derivation déterministe** : ne pas re-créer de creds à chaque ordre, dériver une fois au boot. Évite de spammer `POST /auth/api-key` (rate limit potentiel).
6. **Logs de creds L2** : `ApiCreds(api_key, api_secret, api_passphrase)` — JAMAIS log même partiellement. Le seul log lié = `executor_creds_ready` sans aucun champ.
7. **Fixed-math 6 decimals** : les `makerAmount`/`takerAmount` retournés par CLOB sont des strings comme `"100000000"` (= 100 USDC). Toujours parser comme `Decimal(s) / Decimal(10**6)` pour conversion en float USDC, jamais `float(s)` direct (perte de précision).

---

## 17. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M3

Suis specs/M3-executor.md à la lettre. Avant tout code, action obligatoire : invoque /polymarket:polymarket pour reconfirmer les schémas L1/L2 auth, POST /order, /tick-size, /positions, et l'exception classes du SDK py-clob-client. Les fixtures gamma_market_sample.json et clob_midpoint_sample.json sont déjà capturées (M2). Capture en plus :
- tests/fixtures/clob_tick_size_sample.json (1 GET httpx réel sur https://clob.polymarket.com/tick-size?token_id=<un asset du fixture M1>)
- tests/fixtures/data_api_positions_sample.json (1 GET httpx réel sur https://data-api.polymarket.com/positions?user=<premier wallet de TARGET_WALLETS>)
Compose manuellement tests/fixtures/clob_order_response_sample.json basé sur le schéma doc (§11.2 spec) — pas de POST réel.

Contraintes non négociables :
- Patches M1+M2 (NoDecode, optional polymarket fields, validator CSV TARGET_WALLETS, queue refactor) déjà appliqués — ne rien redéfaire.
- 4 garde-fous Executor obligatoires (§2 spec) implémentés EN PREMIER, avec leurs tests, avant tout code de signature réelle :
  1. Lazy init ClobClient.
  2. RuntimeError startup si dry_run=false ET clés absentes.
  3. Double check assert dry_run is False per-order.
  4. Capital re-check via WalletStateReader avant chaque POST.
- AUCUN log de api_secret, api_passphrase, private_key (vérifié par grep dans les critères d'acceptation §14).
- Mock total de py-clob-client dans les tests unitaires (jamais d'instanciation réelle, jamais de POST réseau).
- Test live opt-in unique : dérivation L1/L2 avec une clé jetable générée localement (eth_account.Account.create), ZÉRO POST.
- Schema migration : drop+recreate `polycopy.db` documenté dans docs/setup.md. Pas d'Alembic à M3.
- Ajout minimal MarketMetadata.neg_risk dans src/polycopy/strategy/dtos.py (1 champ Field(alias="negRisk")). Tests M2 doivent toujours passer.
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print).
- mypy --strict propre, ruff check propre, pytest vert avec coverage ≥ 80% sur executor/ ET pas de régression sur watcher/storage/strategy.
- README polish public : logo `assets/Company_Logo_Polymarket.png` en tête (centré width=400), section Quickstart avec exemple de logs JSON, section "Going live" qui explique la bascule dry-run → réel avec recommandation MAX_POSITION_USD=1 pour le 1er run.
- Mises à jour doc §12 dans le même commit.
- Commit final unique : feat(executor): implement M3 order signing and posting with dry-run safeguards

Demande-moi confirmation avant tout patch sensible (config.py, .env, modif de schéma DB autre que ceux spécifiés §3.2/§3.3, suppression de fichier autre que polycopy.db local).

Si une question reste ambiguë (ex: FOK BUY size sémantique - shares vs USD à confirmer empiriquement, tests live de derivation L1 nécessitent install de eth_account/web3 ?), tranche avec une recommandation et signale-le moi avant d'implémenter, ne bloque pas silencieusement.
```

---

## 18. Questions ouvertes pour Elie

(Section bonus — la spec tranche par défaut, mais à confirmer par toi avant l'implémentation si tu veux changer le tir.)

1. **Sémantique `OrderArgs.size` pour FOK BUY** : la doc skill dit "amount = dollar amount to spend" mais `OrderArgs` Python a un champ `size`. **Recommandation spec** : passer `size = my_size_shares * my_price` (USD à dépenser) pour FOK BUY, `size = my_size_shares` pour FOK SELL. **À tester empiriquement sur un seul ordre réel à $1 quand tu basculeras live.** Si surprenant, M3 remettra à plat — le code aura un commentaire `# FIXME: confirm FOK BUY size semantics empirically`.

2. **Drop + recreate `polycopy.db`** : à M3, le schéma `MyOrder` change significativement (8 nouvelles colonnes, statut enum strict). **Recommandation spec** : `rm polycopy.db` documenté dans `docs/setup.md`. Tu perds l'historique dev local (acceptable). Alternative : Alembic dès M3 (alourdit le PR, écart vs roadmap M4 = Alembic). **Tu confirmes le drop+recreate ?** Si non, je rajoute Alembic à M3.

3. **Image README** : `assets/Company_Logo_Polymarket.png` (1200×574). **Recommandation spec** : centrée width 400px (`<p align="center"><img ... width="400"></p>`). Si tu préfères pleine largeur ou côté gauche `![logo](assets/...)` simple, dis-le.

4. **Test live dérivation auth** : utilise `eth_account.Account.create()` pour générer une clé jetable. **`eth_account` est-il déjà tiré comme dep transitive de `py-clob-client` ?** Je suppose oui, mais à vérifier. Si non, on l'ajoute à `[project.optional-dependencies] dev` (acceptable, c'est dev-only).

5. **`signature_type` default `1` (POLY_PROXY)** : ton M1 §0.3 (et `.env.example`) dit "Magic/email Polymarket". **Tu utilises bien Magic ou un wallet externe (MetaMask) ?** Si MetaMask connecté à polymarket.com, c'est `signature_type=2` (Gnosis Safe). À M3 on respecte ce que tu mets dans `.env`, mais le default peut être révisé si nécessaire.

6. **Bascule dry-run → réel** : à M3 on **n'automatise rien**. Tu dois manuellement :
   - Remplir `.env` (POLYMARKET_PRIVATE_KEY, FUNDER, SIGNATURE_TYPE).
   - Mettre `DRY_RUN=false` ou enlever `--dry-run`.
   - Mettre `MAX_POSITION_USD=1` pour le 1er run.
   - Lancer `python -m polycopy` et surveiller.
   
   Confirmes-tu cette logique 100% manuelle ? (Je n'ajouterai aucun mécanisme auto, c'est par sécurité.)
